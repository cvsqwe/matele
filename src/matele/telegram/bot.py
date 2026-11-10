"""TelegramBot: the Telegram transport for the Dispatcher.

	bot = TelegramBot(token)
	dp.run_polling(bot)

What it papers over so handlers don't care which messenger they're on:
  • message ids: Telegram edits need (chat_id, message_id), so the opaque
	id is "chat_id:message_id";
  • callback_data is capped at 64 bytes: longer payloads are swapped for a
	deterministic "~<hash>" alias and resolved back on the way in. Aliases
	live in memory unless you pass an AliasStore, which lets buttons sent
	before a restart keep working; an unknown alias arrives as empty data,
	which the dispatcher treats like any outdated button;
  • "message is not modified" and already-answered callbacks are harmless
	and swallowed;
  • a caption longer than 1024 characters goes as a separate message;
  • a link button with a non-http(s)/tg link would make Telegram reject
	the whole message: it becomes an inert button instead;
  • text that isn't valid HTML is re-sent as plain text;
  • `my_chat_member` kicked/member in a private chat becomes ChatStatus;
	in a group it becomes a ChatMemberUpdate for the bot itself, and
	"joined"/"left" service messages become ChatMemberUpdates.

Everything else Telegram can do is on `bot.api` (a TelegramApi).
Private chats and groups are routed; channels are not.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from collections import OrderedDict
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from matele.errors import BotApiError, ConnectionGuard
from matele.telegram.api import DEFAULT_API_URL, InputFile, TelegramApi, api_error
from matele.types import (
	GROUP,
	PRIVATE,
	TELEGRAM,
	Attachment,
	Button,
	CallbackQuery,
	ChatMemberUpdate,
	ChatStatus,
	FileKind,
	FileSource,
	IncomingEvent,
	Keyboard,
	Message,
	User,
	default_filename,
	describe_update,
	recipient,
)
from matele.webapp import DEFAULT_MAX_AGE, WebAppData, verify_init_data

logger = logging.getLogger(__name__)

CALLBACK_DATA_LIMIT = 64
CAPTION_LIMIT = 1024
ALIAS_PREFIX = "~"
ALIAS_RESAVE_AFTER = 24 * 3600  # refresh a stored alias's timestamp at most daily
ALIAS_TTL_DAYS = 60
ALIAS_PURGE_EVERY = 24 * 3600
ALIAS_CACHE_SIZE = 10_000  # aliases kept in memory
ALLOWED_UPDATES = ["message", "callback_query", "my_chat_member"]

# attachment type -> (send method, file field, accepts a caption)
SEND_BY_TYPE = {
	"file": ("sendDocument", "document", True),
	"image": ("sendPhoto", "photo", True),
	"video": ("sendVideo", "video", True),
	"audio": ("sendAudio", "audio", True),
	"voice": ("sendVoice", "voice", True),
	"animation": ("sendAnimation", "animation", True),
	"video_note": ("sendVideoNote", "video_note", False),
}


_CHAT_TYPES = {"private": PRIVATE, "group": GROUP, "supergroup": GROUP}
_IN_CHAT = {"creator", "administrator", "member", "restricted"}
# What a person can send; a message with none of these is a service message.
_CONTENT = (
	"text",
	"caption",
	"photo",
	"document",
	"video",
	"audio",
	"voice",
	"video_note",
	"animation",
	"sticker",
	"contact",
	"location",
	"venue",
	"poll",
	"dice",
)


def _target(chat_id: int | None, user_id: int | None) -> int:
	"""On Telegram a private chat's id is the person's id: one address."""
	chat_id, user_id = recipient(chat_id, user_id)
	return chat_id if chat_id is not None else user_id  # type: ignore[return-value]


def _with_query(url: str, **params: str) -> str:
	parts = urlsplit(url)
	query = parse_qsl(parts.query, keep_blank_values=True) + list(params.items())
	return urlunsplit(parts._replace(query=urlencode(query)))


def _is_benign(exc: BotApiError, *needles: str) -> bool:
	text = exc.message.lower()
	return any(needle in text for needle in needles)


class AliasStore(Protocol):
	"""Persistent storage for callback aliases (e.g. a database table)."""

	async def save(self, alias: str, data: str) -> None: ...

	async def get(self, alias: str) -> str | None: ...

	async def purge(self, older_than_days: int) -> int: ...


class CallbackAliases:
	"""Over-long callback payloads -> short deterministic aliases. With a
	store, every alias is saved before the message carrying it is sent.

	Memory holds the `cache_size` most recently used aliases, so a bot with
	ever-changing long payloads doesn't grow forever. An alias pushed out
	is read back from the store; without a store its button stops working,
	like any button from before a restart."""

	def __init__(self, store: AliasStore | None = None, *, cache_size: int = ALIAS_CACHE_SIZE) -> None:
		self.store = store
		self.cache_size = max(cache_size, 1)
		self._by_alias: OrderedDict[str, str] = OrderedDict()
		self._saved_at: dict[str, float] = {}
		self._unsaved: dict[str, str] = {}
		self._purged_at: float | None = None

	def encode(self, data: str) -> str:
		if len(data.encode("utf-8")) <= CALLBACK_DATA_LIMIT and not data.startswith(ALIAS_PREFIX):
			return data
		alias = ALIAS_PREFIX + hashlib.sha1(data.encode("utf-8"), usedforsecurity=False).hexdigest()[:24]
		self._remember(alias, data)
		saved = self._saved_at.get(alias)
		if saved is None or time.monotonic() - saved > ALIAS_RESAVE_AFTER:
			self._unsaved[alias] = data
		return alias

	def _remember(self, alias: str, data: str) -> None:
		self._by_alias[alias] = data
		self._by_alias.move_to_end(alias)
		while len(self._by_alias) > self.cache_size:
			oldest, _ = self._by_alias.popitem(last=False)
			self._saved_at.pop(oldest, None)

	async def flush(self) -> None:
		"""Saves aliases encoded since the last flush (and now and then purges
		old ones). A storage failure is logged, never raised."""
		if self.store is None or not self._unsaved:
			return
		pending, self._unsaved = self._unsaved, {}
		try:
			for alias, data in pending.items():
				await self.store.save(alias, data)
				if alias in self._by_alias:
					self._saved_at[alias] = time.monotonic()
			now = time.monotonic()
			if self._purged_at is None or now - self._purged_at > ALIAS_PURGE_EVERY:
				self._purged_at = now
				await self.store.purge(ALIAS_TTL_DAYS)
		except Exception as exc:  # never block a message on bookkeeping
			logger.warning("Could not store Telegram callback aliases: %s", exc)

	async def decode(self, data: str | None) -> str | None:
		"""The original payload; None for an alias nobody knows."""
		if data is None or not data.startswith(ALIAS_PREFIX):
			return data or ""
		found = self._by_alias.get(data)
		if found is not None:
			self._by_alias.move_to_end(data)
		elif self.store is not None:
			try:
				found = await self.store.get(data)
			except Exception as exc:
				logger.warning("Could not look up a Telegram callback alias: %s", exc)
			if found is not None:
				self._remember(data, found)
		return found


class TelegramBot:
	title = "Telegram"

	def __init__(
		self,
		token: str,
		*,
		api: TelegramApi | None = None,
		api_url: str = DEFAULT_API_URL,
		proxy: str | None = None,
		aliases: CallbackAliases | None = None,
		platform: str = TELEGRAM,
		app_url: str | None = None,
		replace_webhook: bool = False,
	):
		"""`app_url`: the https address of the bot's mini app, for
		Button(app=...); the button's value is added as ?startapp=...
		`replace_webhook`: let polling remove a webhook set for this token.
		Off by default: the webhook may belong to a running deployment."""
		self.api = api or TelegramApi(token, api_url=api_url, proxy=proxy)
		self._token = token
		self.app_url = app_url
		self.aliases = aliases or CallbackAliases()
		self.platform = platform
		self.me: User | None = None
		self.replace_webhook = replace_webhook
		self._offset: int | None = None
		self._webhook_cleared = False

	@property
	def guard(self) -> ConnectionGuard:
		return self.api.guard

	async def close(self) -> None:
		await self.api.close()

	async def get_me(self) -> User:
		self.me = self._user(await self.api.call("getMe"))
		return self.me

	def deep_link(self, username: str, payload: str) -> str:
		return f"https://t.me/{username}?start={payload}" if payload else f"https://t.me/{username}"

	# ------------------------------------------------------------ webhooks
	secret_header = "X-Telegram-Bot-Api-Secret-Token"

	async def set_webhook(self, url: str, *, secret: str | None = None) -> None:
		"""Makes Telegram deliver updates to `url` (https on 443, 80, 88 or 8443)."""
		await self.api.call("setWebhook", url=url, secret_token=secret, allowed_updates=ALLOWED_UPDATES)
		self._webhook_cleared = False

	async def delete_webhook(self, url: str | None = None) -> None:
		await self.api.call("deleteWebhook")

	async def parse_webhook(self, data: dict[str, Any]) -> list[IncomingEvent]:
		"""A webhook request body (one Telegram update) -> matele events."""
		return await self.parse_update(data)

	# ------------------------------------------------------------ incoming
	async def get_events(self, timeout: int = 30) -> list[IncomingEvent]:
		if not self._webhook_cleared:
			await self._clear_webhook()
		updates = (
			await self.api.call(
				"getUpdates",
				offset=self._offset,
				timeout=timeout,
				allowed_updates=ALLOWED_UPDATES,
				request_timeout=timeout + 10,
			)
			or []
		)
		events: list[IncomingEvent] = []
		for update in updates:
			self._offset = update["update_id"] + 1
			try:
				events.extend(await self.parse_update(update))
			except Exception:
				logger.exception("Could not parse Telegram %s", describe_update(update))
		return events

	async def _clear_webhook(self) -> None:
		"""getUpdates refuses to work while a webhook is set. The webhook is
		only removed when asked to (replace_webhook=True): it may be the one
		a running deployment of the bot gets its updates through, and a local
		run with the same token must not silence it."""
		info = await self.api.call("getWebhookInfo")
		url = (info or {}).get("url") if isinstance(info, dict) else None
		if url:
			if not self.replace_webhook:
				raise api_error(
					409,
					"getUpdates",
					f"Telegram sends this bot's updates to the webhook {url}, so polling can't get them. "
					"Remove it with `await bot.delete_webhook()`, or pass TelegramBot(replace_webhook=True)",
				)
			logger.warning("Telegram: removing the webhook %s to start polling", url)
			await self.api.call("deleteWebhook")
		self._webhook_cleared = True

	async def parse_update(self, update: dict[str, Any]) -> list[IncomingEvent]:
		"""One Telegram update -> matele events (usually one; none for
		updates not routed; several when several people joined at once)."""
		if "message" in update:
			raw = update["message"]
			chat_type = _CHAT_TYPES.get(raw.get("chat", {}).get("type"))
			if chat_type is None or not raw.get("from"):
				return []  # channels, anonymous posts
			if raw.get("new_chat_members") or raw.get("left_chat_member"):
				return self._membership_messages(raw)
			if not any(key in raw for key in _CONTENT):
				return []  # service messages: pinned, title changed, migrated, ...
			return [self._message(raw)]
		if "my_chat_member" in update:
			return self._my_membership(update["my_chat_member"])
		if "callback_query" in update:
			raw = update["callback_query"]
			data = await self.aliases.decode(raw.get("data")) or ""
			raw_message = raw.get("message")
			message = self._message(raw_message) if raw_message and raw_message.get("chat") else None
			return [
				CallbackQuery(id=raw["id"], data=data, from_user=self._user(raw["from"]), message=message, bot=self)
			]
		return []

	def _is_me(self, user_id: int) -> bool:
		return self.me is not None and self.me.id == user_id

	def _membership_messages(self, raw: dict[str, Any]) -> list[IncomingEvent]:
		"""Service messages "X joined" / "X left". The bot's own joins and
		leaves are skipped here: they come as my_chat_member too."""
		chat_id, by = raw["chat"]["id"], self._user(raw["from"])
		changes = [(m, True) for m in raw.get("new_chat_members") or []]
		if raw.get("left_chat_member"):
			changes.append((raw["left_chat_member"], False))
		return [
			ChatMemberUpdate(chat_id=chat_id, member=self._user(m), joined=joined, is_me=False, from_user=by, bot=self)
			for m, joined in changes
			if not self._is_me(m["id"])
		]

	def _my_membership(self, raw: dict[str, Any]) -> list[IncomingEvent]:
		"""The bot's own status changed: blocked/unblocked in a private chat,
		added to/removed from a group."""
		chat_type = _CHAT_TYPES.get(raw.get("chat", {}).get("type"))
		if chat_type is None or not raw.get("from"):
			return []
		old = (raw.get("old_chat_member") or {}).get("status")
		new_member = raw.get("new_chat_member") or {}
		new = new_member.get("status")
		if chat_type == PRIVATE:
			if new not in ("kicked", "member"):
				return []
			return [ChatStatus(from_user=self._user(raw["from"]), reachable=new == "member", bot=self)]
		was_in, is_in = old in _IN_CHAT, new in _IN_CHAT
		if was_in == is_in:
			return []  # e.g. promoted to admin: not a join or a leave
		if self.me is not None:
			me = self.me
		elif new_member.get("user"):
			me = self._user(new_member["user"])
		else:
			me = User(id=0, platform=self.platform)
		return [
			ChatMemberUpdate(
				chat_id=raw["chat"]["id"],
				member=me,
				joined=is_in,
				is_me=True,
				from_user=self._user(raw["from"]),
				bot=self,
			)
		]

	def _user(self, data: dict[str, Any]) -> User:
		name = " ".join(p for p in (data.get("first_name"), data.get("last_name")) if p) or None
		return User(id=data["id"], platform=self.platform, name=name, username=data.get("username"))

	@staticmethod
	def _attachments(raw: dict[str, Any]) -> list[Attachment]:
		def att(att_type: str, media: dict[str, Any]) -> Attachment:
			return Attachment(type=att_type, token=media["file_id"], filename=media.get("file_name") or "")

		if raw.get("animation"):  # arrives with a duplicate "document": prefer the animation
			return [att("animation", raw["animation"])]
		if raw.get("document"):
			return [att("file", raw["document"])]
		if raw.get("photo"):
			return [att("image", raw["photo"][-1])]  # the last size is the largest
		for key in ("video", "audio", "voice", "video_note"):
			if raw.get(key):
				return [att(key, raw[key])]
		return []

	def _message(self, raw: dict[str, Any]) -> Message:
		chat_id = raw["chat"]["id"]
		sender = self._user(raw["from"]) if raw.get("from") else User(id=chat_id, platform=self.platform)
		text = raw.get("text") if raw.get("text") is not None else raw.get("caption")
		return Message(
			message_id=f"{chat_id}:{raw['message_id']}",
			chat_id=chat_id,
			from_user=sender,
			text=text,
			bot=self,
			attachments=self._attachments(raw),
			chat_type=_CHAT_TYPES.get(raw["chat"].get("type"), GROUP),
		)

	# ------------------------------------------------------------ outgoing
	def message_sequence(self, message_id: str) -> int | None:
		"""Telegram numbers messages within a chat: menus use it to tell
		whether one message is right above another."""
		try:
			return self.split_id(message_id)[1]
		except (BotApiError, ValueError):
			return None

	@staticmethod
	def split_id(message_id: str) -> tuple[int, int]:
		chat_id, _, mid = message_id.partition(":")
		if not mid:
			raise api_error(400, "bad_message_id", f"not a Telegram message id: {message_id!r}")
		return int(chat_id), int(mid)

	def _button(self, button: Button) -> dict[str, Any]:
		if button.app is not None:
			if not self.app_url:
				raise ValueError("mini app buttons need TelegramBot(app_url=...)")
			url = _with_query(self.app_url, startapp=button.app) if button.app else self.app_url
			return {"text": button.text, "web_app": {"url": url}}
		if button.url is not None:
			if button.url.startswith(("http://", "https://", "tg://")):
				return {"text": button.text, "url": button.url}
			return {"text": button.text, "callback_data": "noop"}
		return {"text": button.text, "callback_data": self.aliases.encode(button.data or "noop")}

	def markup(self, keyboard: Keyboard | None) -> dict[str, Any] | None:
		if keyboard is None:
			return None
		return {"inline_keyboard": [[self._button(b) for b in row] for row in keyboard.rows]}

	async def _call_html(self, method: str, **params: Any) -> Any:
		"""Calls with parse_mode=HTML; text that doesn't parse goes as plain text."""
		try:
			return await self.api.call(method, parse_mode="HTML", **params)
		except BotApiError as exc:
			if not _is_benign(exc, "can't parse entities"):
				raise
			return await self.api.call(method, **params)

	async def send_message(
		self,
		text: str,
		*,
		chat_id: int | None = None,
		user_id: int | None = None,
		keyboard: Keyboard | None = None,
		reply_to: str | None = None,
	) -> Message:
		target = _target(chat_id, user_id)
		markup = self.markup(keyboard)
		await self.aliases.flush()
		raw = await self._call_html(
			"sendMessage",
			chat_id=target,
			text=text,
			link_preview_options={"is_disabled": True},
			reply_markup=markup,
			reply_parameters=self._reply_parameters(reply_to),
		)
		return self._message(raw)

	def _reply_parameters(self, reply_to: str | None) -> dict[str, Any] | None:
		if not reply_to:
			return None
		_, mid = self.split_id(reply_to)
		return {"message_id": mid, "allow_sending_without_reply": True}

	async def edit_message(self, message_id: str, text: str | None, *, keyboard: Keyboard | None = None) -> None:
		chat_id, mid = self.split_id(message_id)
		markup = self.markup(keyboard)
		await self.aliases.flush()
		try:
			if text is None:
				await self.api.call(
					"editMessageReplyMarkup",
					chat_id=chat_id,
					message_id=mid,
					reply_markup=markup or {"inline_keyboard": []},
				)
				return
			await self._call_html(
				"editMessageText",
				chat_id=chat_id,
				message_id=mid,
				text=text,
				link_preview_options={"is_disabled": True},
				reply_markup=markup,
			)
		except BotApiError as exc:
			if not _is_benign(exc, "message is not modified"):
				raise

	async def delete_message(self, message_id: str) -> None:
		chat_id, mid = self.split_id(message_id)
		await self.api.call("deleteMessage", chat_id=chat_id, message_id=mid)

	async def answer_callback(self, callback_id: str, *, text: str | None = None, alert: bool = False) -> None:
		try:
			await self.api.call(
				"answerCallbackQuery", callback_query_id=callback_id, text=text or None, show_alert=bool(alert and text)
			)
		except BotApiError as exc:
			if not _is_benign(exc, "query is too old", "query id is invalid", "already"):
				raise

	# ------------------------------------------------------------ files
	async def _send_with_caption(
		self, method: str, params: dict[str, Any], *, caption: str | None, supports_caption: bool
	) -> Message:
		long_caption = None
		if caption and supports_caption and len(caption) <= CAPTION_LIMIT:
			raw = await self._call_html(method, caption=caption, **params)
		else:
			long_caption = caption
			raw = await self.api.call(method, **params)
		sent = self._message(raw)
		if long_caption:
			await self.send_message(long_caption, chat_id=params["chat_id"])
		return sent

	async def send_document(
		self,
		content: bytes,
		filename: str,
		*,
		chat_id: int | None = None,
		user_id: int | None = None,
		caption: str | None = None,
	) -> Message:
		return await self.send_file(
			content, kind="file", filename=filename, chat_id=chat_id, user_id=user_id, caption=caption
		)

	async def send_file(
		self,
		source: FileSource,
		*,
		kind: FileKind = "file",
		filename: str | None = None,
		chat_id: int | None = None,
		user_id: int | None = None,
		caption: str | None = None,
		keyboard: Keyboard | None = None,
		reply_to: str | None = None,
	) -> Message:
		method, field, supports_caption = SEND_BY_TYPE[kind]
		params: dict[str, Any] = {
			"chat_id": _target(chat_id, user_id),
			field: InputFile(source, filename or default_filename(source, kind)),
			"reply_markup": self.markup(keyboard),
			"reply_parameters": self._reply_parameters(reply_to),
		}
		await self.aliases.flush()
		return await self._send_with_caption(method, params, caption=caption, supports_caption=supports_caption)

	def verify_init_data(self, init_data: str, *, max_age: float | None = DEFAULT_MAX_AGE) -> WebAppData:
		"""Checks initData from this bot's mini app (see matele.webapp)."""
		return verify_init_data(init_data, self._token, platform=self.platform, max_age=max_age)

	async def send_typing(self, chat_id: int) -> None:
		await self.api.call("sendChatAction", chat_id=chat_id, action="typing")

	async def set_commands(self, commands: list[tuple[str, str]]) -> None:
		await self.api.call("setMyCommands", commands=[{"command": n, "description": d} for n, d in commands])

	async def send_file_by_token(
		self,
		token: str,
		*,
		chat_id: int | None = None,
		user_id: int | None = None,
		file_type: str = "file",
		caption: str | None = None,
	) -> Message:
		method, field, supports_caption = SEND_BY_TYPE.get(file_type, SEND_BY_TYPE["file"])
		params = {"chat_id": _target(chat_id, user_id), field: token}
		return await self._send_with_caption(method, params, caption=caption, supports_caption=supports_caption)

	async def download_file(
		self, token: str, *, file_type: str, filename: str, url: str | None = None
	) -> tuple[bytes, str]:
		info = await self.api.call("getFile", file_id=token)
		file_path = (info or {}).get("file_path")
		if not file_path:
			raise api_error(400, "getFile", "file is not available for download")
		content = await self.api.download(file_path)
		name = filename or os.path.basename(file_path)
		if "." not in name:
			name += os.path.splitext(file_path)[1]
		return content, name
