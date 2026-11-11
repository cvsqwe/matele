"""MaxBot: the MAX transport for the Dispatcher.

	bot = MaxBot(token)
	dp.run_polling(bot)

It turns MAX updates into matele events and matele calls into MAX API
calls, papering over what differs from other messengers:
  • the deep-link payload arrives as a separate `bot_started` update; it
	becomes a "/start <payload>" message, like everywhere else;
  • `bot_stopped` / `bot_started` become ChatStatus events;
  • MAX has no modal alert on button answers (only a toast), so an alert
	is also sent as a regular message;
  • MAX rejects a whole message if a link button points somewhere it
	doesn't consider public (localhost, a bare IP…); such buttons are
	moved into the text instead. An edit that keeps the text as it is
	(text=None) has no text to move them into: they become inert buttons
	there, like on Telegram, and a warning is logged;
  • text is sent as HTML.

Everything MAX can do beyond that is on `bot.api` (a MaxApi).
Private dialogs and group chats are routed; channels are not.
"""

from __future__ import annotations

import html
import logging
from collections import OrderedDict
from typing import Any
from urllib.parse import urlsplit

from matele.callbacks import sign, unsign
from matele.errors import BotApiError, BotForbiddenError, ConnectionGuard
from matele.max import models as m
from matele.max.api import DEFAULT_BASE_URL, MaxApi
from matele.types import (
	GROUP,
	MAX,
	PRIVATE,
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

_CALLBACK_USERS_LIMIT = 1000
_KNOWN_MEDIA_LIMIT = 1000  # messages whose attachments are remembered for edits
_CHAT_TYPES = {"dialog": PRIVATE, "chat": GROUP}
_DEFAULT_FILENAMES = {"image": "image.jpg", "video": "video.mp4", "audio": "audio", "file": "file"}


class MaxBot:
	title = "MAX"

	def __init__(
		self,
		token: str,
		*,
		api: MaxApi | None = None,
		base_url: str = DEFAULT_BASE_URL,
		platform: str = MAX,
		callback_secret: str | None = None,
	):
		"""`platform` is the name events carry (and FSM keys use); keep the
		default unless you need it to match ids stored by older code.
		`callback_secret`: signs button data, so only buttons this bot made
		are accepted (see matele.callbacks). Keep it the same across
		restarts: buttons signed with another secret stop working."""
		self._callback_secret = callback_secret
		self.api = api or MaxApi(token, base_url=base_url)
		self._token = token
		self.platform = platform
		self.me: User | None = None
		self._marker: int | None = None
		self._subscriptions_checked = False
		# callback_id -> who pressed, so an alert can also go to them as a message
		self._callback_users: dict[str, int] = {}
		# message id -> its attachments other than the keyboard, which an edit
		# has to send again (MAX drops what an edit doesn't list)
		self._media: OrderedDict[str, list[m.AttachmentRequest]] = OrderedDict()

	@property
	def guard(self) -> ConnectionGuard:
		return self.api.guard

	async def close(self) -> None:
		await self.api.close()

	async def get_me(self) -> User:
		info = await self.api.get_me()
		self.me = User(id=info.user_id, platform=self.platform, name=info.first_name, username=info.username)
		return self.me

	def deep_link(self, username: str, payload: str) -> str:
		return f"https://max.ru/{username}?start={payload}" if payload else f"https://max.ru/{username}"

	# ------------------------------------------------------------ webhooks
	secret_header = "X-Max-Bot-Api-Secret"

	async def set_webhook(self, url: str, *, secret: str | None = None) -> None:
		"""Subscribes `url` to this bot's updates (MAX requires https on port 443)."""
		await self.api.subscribe(url, secret=secret)

	async def delete_webhook(self, url: str) -> None:
		await self.api.unsubscribe(url)

	async def parse_webhook(self, data: dict[str, Any]) -> list[IncomingEvent]:
		"""A webhook request body (one MAX update) -> matele events."""
		return self.parse_update(m.Update.from_dict(data))

	# ------------------------------------------------------------ incoming
	async def get_events(self, timeout: int = 30) -> list[IncomingEvent]:
		if not self._subscriptions_checked:
			await self._warn_about_webhooks()
		result = await self.api.get_updates(marker=self._marker, timeout=timeout, limit=100)
		if result.marker is not None:
			self._marker = result.marker
		events: list[IncomingEvent] = []
		for update in result.updates:
			try:
				events.extend(self.parse_update(update))
			except Exception:
				logger.exception("Could not parse MAX %s", describe_update(update.raw))
		return events

	async def _warn_about_webhooks(self) -> None:
		"""While a webhook is subscribed, MAX sends updates there and long
		polling gets nothing. Not removed automatically: another deployment
		of the bot may be using it. Checked once: it is only a hint."""
		self._subscriptions_checked = True
		try:
			subscriptions = await self.api.get_subscriptions()
		except Exception as exc:
			logger.debug("Could not check MAX webhook subscriptions: %s", exc)
			return
		for sub in subscriptions:
			logger.warning(
				"MAX sends this bot's updates to the webhook %s, so polling won't get them. "
				"Remove it with `await bot.delete_webhook(url)` or use matele.webhook.Webhook.",
				sub.url,
			)

	def parse_update(self, update: m.Update) -> list[IncomingEvent]:
		"""One MAX update -> matele events (none for updates not routed)."""
		event = self._parse(update)
		if isinstance(update, m.BotStartedUpdate) and event is not None:
			# Starting the bot again also means it can write to the person
			# again (Telegram says so with my_chat_member; MAX doesn't).
			return [ChatStatus(from_user=event.from_user, reachable=True, bot=self), event]
		return [event] if event is not None else []

	def _parse(self, update: m.Update) -> IncomingEvent | None:
		if isinstance(update, m.MessageCreatedUpdate):
			msg = update.message
			if msg.sender is None or msg.recipient.chat_type not in _CHAT_TYPES:
				return None  # channel posts, system messages
			return self._message(msg)
		if isinstance(update, m.MessageCallbackUpdate):
			cb = update.callback
			self._remember_callback(cb.callback_id, cb.user.user_id)
			if update.message is not None:  # the message the button is on: likely edited next
				self._remember_media(update.message)
			return CallbackQuery(
				id=cb.callback_id,
				data=self._checked(cb.payload or "", cb.user.user_id),
				from_user=self._user(cb.user),
				message=self._message(update.message) if update.message is not None else None,
				bot=self,
			)
		if isinstance(update, m.BotStartedUpdate):
			user = self._user(update.user)
			return Message(
				message_id="",  # not a real message: nothing to edit or delete
				chat_id=update.chat_id,
				from_user=user,
				text=f"/start {update.payload}" if update.payload else "/start",
				bot=self,
			)
		if isinstance(update, m.BotStoppedUpdate):
			return ChatStatus(from_user=self._user(update.user), reachable=False, bot=self)
		if isinstance(update, m.BotAddedToChatUpdate | m.BotRemovedFromChatUpdate):
			if update.is_channel:
				return None
			me = self.me or User(id=0, platform=self.platform)
			return ChatMemberUpdate(
				chat_id=update.chat_id,
				member=me,
				joined=isinstance(update, m.BotAddedToChatUpdate),
				is_me=True,
				from_user=self._user(update.user),
				bot=self,
			)
		if isinstance(update, m.UserAddedToChatUpdate | m.UserRemovedFromChatUpdate):
			if update.is_channel:
				return None
			member = self._user(update.user)
			by = update.inviter_id if isinstance(update, m.UserAddedToChatUpdate) else update.admin_id
			return ChatMemberUpdate(
				chat_id=update.chat_id,
				member=member,
				joined=isinstance(update, m.UserAddedToChatUpdate),
				is_me=self.me is not None and member.id == self.me.id,
				from_user=User(id=by, platform=self.platform) if by else member,
				bot=self,
			)
		logger.debug("Ignoring MAX update %s", update.update_type)
		return None

	def _checked(self, data: str, user_id: int) -> str:
		"""Button data without its signature; "" if the signature is wrong."""
		if not self._callback_secret or not data:
			return data
		checked = unsign(data, self._callback_secret)
		if checked is None:
			logger.debug("MAX: button data without a valid signature from user %s", user_id)
		return checked or ""

	def _user(self, user: m.User) -> User:
		name = " ".join(p for p in (user.first_name, user.last_name) if p) or None
		return User(id=user.user_id, platform=self.platform, name=name, username=user.username)

	def _message(self, msg: m.Message) -> Message:
		chat_id = msg.recipient.chat_id if msg.recipient.chat_id is not None else msg.recipient.user_id
		sender = self._user(msg.sender) if msg.sender is not None else User(id=chat_id or 0, platform=self.platform)
		return Message(
			message_id=msg.body.mid,
			chat_id=chat_id or 0,
			from_user=sender,
			text=msg.body.text,
			bot=self,
			attachments=[a for a in map(_attachment, msg.body.attachments or []) if a is not None],
			chat_type=_CHAT_TYPES.get(msg.recipient.chat_type, GROUP),
		)

	def _remember_callback(self, callback_id: str, user_id: int) -> None:
		if len(self._callback_users) >= _CALLBACK_USERS_LIMIT:
			self._callback_users.pop(next(iter(self._callback_users)))  # oldest first
		self._callback_users[callback_id] = user_id

	def _remember_media(self, msg: m.Message) -> None:
		mid = msg.body.mid
		if not mid:
			return
		self._media[mid] = media_requests(msg.body.attachments)
		self._media.move_to_end(mid)
		while len(self._media) > _KNOWN_MEDIA_LIMIT:
			self._media.popitem(last=False)

	async def _media_of(self, message_id: str) -> list[m.AttachmentRequest]:
		"""The attachments (but the keyboard) an edit must keep; asks MAX for
		a message this bot hasn't seen since it started."""
		known = self._media.get(message_id)
		if known is None:
			self._remember_media(await self.api.get_message(message_id))
			known = self._media.get(message_id, [])
		return list(known)

	# ------------------------------------------------------------ outgoing
	async def send_message(
		self,
		text: str,
		*,
		chat_id: int | None = None,
		user_id: int | None = None,
		keyboard: Keyboard | None = None,
		reply_to: str | None = None,
	) -> Message:
		chat_id, user_id = recipient(chat_id, user_id)
		body, keyboard = move_unlinkable_buttons(text, keyboard)
		attachments = [keyboard_attachment(keyboard, self.me, self._callback_secret)] if keyboard is not None else None
		sent = await self.api.send_message(
			chat_id=chat_id,
			user_id=user_id,
			text=body,
			format="html",
			attachments=attachments,
			link=_reply_link(reply_to),
		)
		self._remember_media(sent)
		return self._message(sent)

	async def edit_message(self, message_id: str, text: str | None, *, keyboard: Keyboard | None = None) -> None:
		if text is None:  # the text stays: links MAX won't take can't go into it
			new_text, keyboard = None, inert_unlinkable_buttons(keyboard)
		else:
			new_text, keyboard = move_unlinkable_buttons(text, keyboard)
		# MAX replaces all attachments with the ones listed: the media goes
		# again, and the keyboard only if there is one (an edit without drops it).
		attachments = await self._media_of(message_id)
		if keyboard is not None:
			attachments.append(keyboard_attachment(keyboard, self.me, self._callback_secret))
		await self.api.edit_message(message_id, text=new_text, format="html", attachments=attachments)

	async def delete_message(self, message_id: str) -> None:
		await self.api.delete_message(message_id)
		self._media.pop(message_id, None)

	async def answer_callback(self, callback_id: str, *, text: str | None = None, alert: bool = False) -> None:
		user_id = self._callback_users.pop(callback_id, None)
		try:
			# Without a text nothing but the answer itself is sent: an empty
			# notification could show an empty toast.
			await self.api.answer_callback(callback_id, notification=text or None)
		except BotApiError as exc:
			if text or exc.status_code != 400:
				raise
			# Should MAX want a field in the answer after all, an empty
			# notification stops the spinner just the same.
			await self.api.answer_callback(callback_id, notification="")
		if alert and text and user_id is not None:
			# Privately, also when the button is in a group: an alert is
			# meant for the person who pressed, not for the whole chat.
			try:
				await self.send_message(html.escape(text, quote=False), user_id=user_id)
			except BotForbiddenError:
				logger.debug("Alert not delivered: user %s never started the bot", user_id)

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
		chat_id, user_id = recipient(chat_id, user_id)
		attachment = await self.api.upload(source, type=kind, filename=filename or default_filename(source, kind))
		return await self._send_attachment(attachment, chat_id, user_id, caption, keyboard, reply_to)

	def verify_init_data(self, init_data: str, *, max_age: float | None = DEFAULT_MAX_AGE) -> WebAppData:
		"""Checks initData from this bot's mini app (see matele.webapp)."""
		return verify_init_data(init_data, self._token, platform=self.platform, max_age=max_age)

	async def send_typing(self, chat_id: int) -> None:
		await self.api.send_action(chat_id, "typing_on")

	async def set_commands(self, commands: list[tuple[str, str]]) -> None:
		if len(commands) > MENU_LIMIT:
			logger.warning("MAX shows at most %s commands in the menu: the rest are left out", MENU_LIMIT)
		await self.api.set_my_commands(m.BotCommand(name=n, description=d) for n, d in commands[:MENU_LIMIT])

	async def send_file_by_token(
		self,
		token: str,
		*,
		chat_id: int | None = None,
		user_id: int | None = None,
		file_type: str = "file",
		caption: str | None = None,
	) -> Message:
		chat_id, user_id = recipient(chat_id, user_id)
		return await self._send_attachment(_attachment_by_token(file_type, token), chat_id, user_id, caption)

	async def _send_attachment(
		self,
		attachment: m.AttachmentRequest,
		chat_id: int | None,
		user_id: int | None,
		caption: str | None,
		keyboard: Keyboard | None = None,
		reply_to: str | None = None,
	) -> Message:
		caption, keyboard = move_unlinkable_buttons(caption, keyboard)
		attachments = [attachment]
		if keyboard is not None:
			attachments.append(keyboard_attachment(keyboard, self.me, self._callback_secret))
		sent = await self.api.send_message(
			chat_id=chat_id,
			user_id=user_id,
			text=caption,
			format="html" if caption else None,
			attachments=attachments,
			link=_reply_link(reply_to),
		)
		self._remember_media(sent)
		return self._message(sent)

	async def download_file(
		self, token: str, *, file_type: str, filename: str, url: str | None = None
	) -> tuple[bytes, str]:
		if not url:
			raise ValueError("MAX attachments are downloaded by URL; this one has none")
		content = await self.api.download(url)
		return content, filename or _DEFAULT_FILENAMES.get(file_type, "file")


# ---------------------------------------------------------------------
# Wire helpers
# ---------------------------------------------------------------------
def _attachment(att: m.Attachment) -> Attachment | None:
	"""Files a person sent; keyboards, stickers, contacts… are skipped."""
	payload = getattr(att, "payload", None)
	token = getattr(payload, "token", None)
	if not token or att.type not in ("image", "video", "audio", "file"):
		return None
	filename = getattr(att, "filename", None) or _DEFAULT_FILENAMES[att.type]
	return Attachment(type=att.type, token=token, filename=filename, url=getattr(payload, "url", None))


def media_requests(attachments: list[m.Attachment] | None) -> list[m.AttachmentRequest]:
	"""A received message's attachments as they would be sent again, without
	the keyboard (an edit sets that itself). Kinds that can't be sent again
	are left out, with a warning."""
	requests: list[m.AttachmentRequest] = []
	for att in attachments or []:
		request = _request_for(att)
		if request is not None:
			requests.append(request)
		elif att.type != "inline_keyboard":
			logger.warning("MAX: a %s attachment can't be kept when the message is edited", att.type)
	return requests


def _request_for(att: m.Attachment) -> m.AttachmentRequest | None:
	if isinstance(att, m.PhotoAttachment) and att.payload.token:
		return m.PhotoAttachmentRequest(payload=m.PhotoAttachmentRequestPayload(token=att.payload.token))
	if isinstance(att, m.VideoAttachment) and att.payload.token:
		return m.VideoAttachmentRequest(payload=m.UploadedInfo(token=att.payload.token))
	if isinstance(att, m.AudioAttachment) and att.payload.token:
		return m.AudioAttachmentRequest(payload=m.UploadedInfo(token=att.payload.token))
	if isinstance(att, m.FileAttachment) and att.payload.token:
		return m.FileAttachmentRequest(payload=m.UploadedInfo(token=att.payload.token))
	if isinstance(att, m.StickerAttachment) and att.payload.code:
		return m.StickerAttachmentRequest(payload=m.StickerAttachmentRequestPayload(code=att.payload.code))
	if isinstance(att, m.LocationAttachment):
		return m.LocationAttachmentRequest(latitude=att.latitude, longitude=att.longitude)
	if isinstance(att, m.ShareAttachment) and (att.payload.url or att.payload.token):
		return m.ShareAttachmentRequest(payload=m.ShareAttachmentPayload(url=att.payload.url, token=att.payload.token))
	return None


def _reply_link(reply_to: str | None) -> m.NewMessageLink | None:
	return m.NewMessageLink(type="reply", mid=reply_to) if reply_to else None


def _attachment_by_token(file_type: str, token: str) -> m.AttachmentRequest:
	if file_type == "image":
		return m.PhotoAttachmentRequest(payload=m.PhotoAttachmentRequestPayload(token=token))
	cls = {"video": m.VideoAttachmentRequest, "audio": m.AudioAttachmentRequest}.get(file_type, m.FileAttachmentRequest)
	return cls(payload=m.UploadedInfo(token=token))


def keyboard_attachment(
	keyboard: Keyboard, bot: User | None = None, secret: str | None = None
) -> m.InlineKeyboardAttachmentRequest:
	"""`bot`: the bot itself, needed for mini app buttons (they point at the
	bot whose mini app to open). `secret`: signs the buttons' data."""
	rows: list[list[m.Button]] = [[_button(b, bot, secret) for b in row] for row in keyboard.rows]
	return m.InlineKeyboardAttachmentRequest(payload=m.InlineKeyboardAttachmentRequestPayload(buttons=rows))


CALLBACK_PAYLOAD_LIMIT = 1024  # MAX's limit on a button's payload
MENU_LIMIT = 32  # commands MAX shows in the menu


def _button(button: Button, bot: User | None = None, secret: str | None = None) -> m.Button:
	if button.app is not None:
		if bot is None or not bot.username:
			raise ValueError("mini app buttons need the bot's own name: call get_me() first (polling and webhooks do)")
		# The app's address is set in the bot's settings on business.max.ru.
		return m.OpenAppButton(text=button.text, web_app=bot.username, contact_id=bot.id, payload=button.app or None)
	if button.url is not None:
		return m.LinkButton(text=button.text, url=button.url)
	payload = button.data or "noop"
	if secret:
		payload = sign(payload, secret)
	if len(payload) > CALLBACK_PAYLOAD_LIMIT:
		raise ValueError(
			f"button {button.text!r}: MAX allows {CALLBACK_PAYLOAD_LIMIT} characters of data, got {len(payload)}"
		)
	return m.CallbackButton(text=button.text, payload=payload)


def is_linkable(url: str) -> bool:
	"""Would MAX accept this as a link button? Only public http(s) hosts."""
	parts = urlsplit(url)
	host = (parts.hostname or "").lower()
	return (
		parts.scheme in ("http", "https")
		and "." in host
		and host != "0.0.0.0"
		and not host.startswith("127.")
		and not host.endswith(".localhost")
	)


def move_unlinkable_buttons(text: str | None, keyboard: Keyboard | None) -> tuple[str | None, Keyboard | None]:
	"""Link buttons MAX would reject become "label: url" lines under the text."""
	if keyboard is None:
		return text, None
	moved: list[Button] = []
	rows: list[list[Button]] = []
	for row in keyboard.rows:
		kept = []
		for button in row:
			if button.url is not None and not is_linkable(button.url):
				moved.append(button)
			else:
				kept.append(button)
		if kept:
			rows.append(kept)
	if not moved:
		return text, keyboard
	lines = "\n".join(f"{html.escape(b.text)}: {html.escape(b.url or '')}" for b in moved)
	return (f"{text}\n\n{lines}" if text else lines), Keyboard(rows) if rows else None


def inert_unlinkable_buttons(keyboard: Keyboard | None) -> Keyboard | None:
	"""Link buttons MAX would reject become buttons that do nothing."""
	if keyboard is None:
		return None
	rows: list[list[Button]] = []
	for row in keyboard.rows:
		kept: list[Button] = []
		for button in row:
			if button.url is not None and not is_linkable(button.url):
				logger.warning("MAX won't take a link button to %s: it is left without its link", button.url)
				button = Button(button.text, data="noop")
			kept.append(button)
		rows.append(kept)
	return Keyboard(rows)
