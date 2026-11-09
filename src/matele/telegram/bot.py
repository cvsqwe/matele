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
  • `my_chat_member` kicked/member in a private chat becomes ChatStatus.

Everything else Telegram can do is on `bot.api` (a TelegramApi).
Only private chats are routed for now.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from typing import Any, Protocol

from matele.errors import BotApiError, ConnectionGuard
from matele.telegram.api import DEFAULT_API_URL, InputFile, TelegramApi, api_error
from matele.types import (
	TELEGRAM,
	Attachment,
	Button,
	CallbackQuery,
	ChatStatus,
	IncomingEvent,
	Keyboard,
	Message,
	User,
)

logger = logging.getLogger(__name__)

CALLBACK_DATA_LIMIT = 64
CAPTION_LIMIT = 1024
ALIAS_PREFIX = "~"
ALIAS_RESAVE_AFTER = 24 * 3600  # refresh a stored alias's timestamp at most daily
ALIAS_TTL_DAYS = 60
ALIAS_PURGE_EVERY = 24 * 3600
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
	store, every alias is saved before the message carrying it is sent."""

	def __init__(self, store: AliasStore | None = None) -> None:
		self.store = store
		self._by_alias: dict[str, str] = {}
		self._saved_at: dict[str, float] = {}
		self._unsaved: dict[str, str] = {}
		self._purged_at: float | None = None

	def encode(self, data: str) -> str:
		if len(data.encode("utf-8")) <= CALLBACK_DATA_LIMIT and not data.startswith(ALIAS_PREFIX):
			return data
		alias = ALIAS_PREFIX + hashlib.sha1(data.encode("utf-8"), usedforsecurity=False).hexdigest()[:24]
		self._by_alias[alias] = data
		saved = self._saved_at.get(alias)
		if saved is None or time.monotonic() - saved > ALIAS_RESAVE_AFTER:
			self._unsaved[alias] = data
		return alias

	async def flush(self) -> None:
		"""Saves aliases encoded since the last flush (and now and then purges
		old ones). A storage failure is logged, never raised."""
		if self.store is None or not self._unsaved:
			return
		pending, self._unsaved = self._unsaved, {}
		try:
			for alias, data in pending.items():
				await self.store.save(alias, data)
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
		if found is None and self.store is not None:
			try:
				found = await self.store.get(data)
			except Exception as exc:
				logger.warning("Could not look up a Telegram callback alias: %s", exc)
			if found is not None:
				self._by_alias[data] = found
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
	):
		self.api = api or TelegramApi(token, api_url=api_url, proxy=proxy)
		self.aliases = aliases or CallbackAliases()
		self.platform = platform
		self.me: User | None = None
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

	# ------------------------------------------------------------ incoming
	async def get_events(self, timeout: int = 30) -> list[IncomingEvent]:
		if not self._webhook_cleared:
			await self.api.call("deleteWebhook")  # getUpdates refuses to work while a webhook is set
			self._webhook_cleared = True
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
				event = await self.parse_update(update)
			except Exception:
				logger.exception("Could not parse Telegram update: %r", update)
				continue
			if event is not None:
				events.append(event)
		return events

	async def parse_update(self, update: dict[str, Any]) -> IncomingEvent | None:
		"""One Telegram update -> a matele event (None for updates not routed)."""
		if "message" in update:
			raw = update["message"]
			if raw.get("chat", {}).get("type") != "private" or not raw.get("from"):
				return None
			return self._message(raw)
		if "my_chat_member" in update:
			raw = update["my_chat_member"]
			if raw.get("chat", {}).get("type") != "private" or not raw.get("from"):
				return None
			status = (raw.get("new_chat_member") or {}).get("status")
			if status not in ("kicked", "member"):
				return None
			return ChatStatus(from_user=self._user(raw["from"]), reachable=status == "member", bot=self)
		if "callback_query" in update:
			raw = update["callback_query"]
			data = await self.aliases.decode(raw.get("data")) or ""
			raw_message = raw.get("message")
			message = self._message(raw_message) if raw_message and raw_message.get("chat") else None
			return CallbackQuery(id=raw["id"], data=data, from_user=self._user(raw["from"]), message=message, bot=self)
		return None

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
		)

	# ------------------------------------------------------------ outgoing
	@staticmethod
	def split_id(message_id: str) -> tuple[int, int]:
		chat_id, _, mid = message_id.partition(":")
		if not mid:
			raise api_error(400, "bad_message_id", f"not a Telegram message id: {message_id!r}")
		return int(chat_id), int(mid)

	def _button(self, button: Button) -> dict[str, Any]:
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

	async def send_message(self, text: str, *, user_id: int, keyboard: Keyboard | None = None) -> Message:
		markup = self.markup(keyboard)
		await self.aliases.flush()
		raw = await self._call_html(
			"sendMessage",
			chat_id=user_id,
			text=text,
			link_preview_options={"is_disabled": True},
			reply_markup=markup,
		)
		return self._message(raw)

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
		self, method: str, params: dict[str, Any], *, caption: str | None, supports_caption: bool, user_id: int
	) -> Message:
		long_caption = None
		if caption and supports_caption and len(caption) <= CAPTION_LIMIT:
			raw = await self._call_html(method, caption=caption, **params)
		else:
			long_caption = caption
			raw = await self.api.call(method, **params)
		sent = self._message(raw)
		if long_caption:
			await self.send_message(long_caption, user_id=user_id)
		return sent

	async def send_document(
		self, content: bytes, filename: str, *, user_id: int, caption: str | None = None
	) -> Message:
		params = {"chat_id": user_id, "document": InputFile(content, filename)}
		return await self._send_with_caption(
			"sendDocument", params, caption=caption, supports_caption=True, user_id=user_id
		)

	async def send_file_by_token(
		self, token: str, *, user_id: int, file_type: str = "file", caption: str | None = None
	) -> Message:
		method, field, supports_caption = SEND_BY_TYPE.get(file_type, SEND_BY_TYPE["file"])
		return await self._send_with_caption(
			method,
			{"chat_id": user_id, field: token},
			caption=caption,
			supports_caption=supports_caption,
			user_id=user_id,
		)

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
