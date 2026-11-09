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
	moved into the text instead;
  • text is sent as HTML.

Everything MAX can do beyond that is on `bot.api` (a MaxApi).
Only private dialogs are routed for now; group chats are ignored.
"""

from __future__ import annotations

import html
import logging
from urllib.parse import urlsplit

from matele.errors import ConnectionGuard
from matele.max import models as m
from matele.max.api import DEFAULT_BASE_URL, MaxApi
from matele.types import (
	MAX,
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

_CALLBACK_USERS_LIMIT = 1000
_DEFAULT_FILENAMES = {"image": "image.jpg", "video": "video.mp4", "audio": "audio", "file": "file"}


class MaxBot:
	title = "MAX"

	def __init__(self, token: str, *, api: MaxApi | None = None, base_url: str = DEFAULT_BASE_URL, platform: str = MAX):
		"""`platform` is the name events carry (and FSM keys use); keep the
		default unless you need it to match ids stored by older code."""
		self.api = api or MaxApi(token, base_url=base_url)
		self.platform = platform
		self.me: User | None = None
		self._marker: int | None = None
		# callback_id -> who pressed, so an alert can also go to them as a message
		self._callback_users: dict[str, int] = {}

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

	# ------------------------------------------------------------ incoming
	async def get_events(self, timeout: int = 30) -> list[IncomingEvent]:
		result = await self.api.get_updates(marker=self._marker, timeout=timeout, limit=100)
		if result.marker is not None:
			self._marker = result.marker
		events: list[IncomingEvent] = []
		for update in result.updates:
			try:
				event = self.parse_update(update)
			except Exception:
				logger.exception("Could not parse MAX update: %r", update.raw)
				continue
			if event is not None:
				events.append(event)
		return events

	def parse_update(self, update: m.Update) -> IncomingEvent | None:
		"""One MAX update -> a matele event (None for updates not routed)."""
		if isinstance(update, m.MessageCreatedUpdate):
			msg = update.message
			if msg.sender is None or msg.recipient.chat_type != "dialog":
				return None  # channel posts, system messages, group chats
			return self._message(msg)
		if isinstance(update, m.MessageCallbackUpdate):
			cb = update.callback
			self._remember_callback(cb.callback_id, cb.user.user_id)
			return CallbackQuery(
				id=cb.callback_id,
				data=cb.payload or "",
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
		logger.debug("Ignoring MAX update %s", update.update_type)
		return None

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
		)

	def _remember_callback(self, callback_id: str, user_id: int) -> None:
		if len(self._callback_users) >= _CALLBACK_USERS_LIMIT:
			self._callback_users.pop(next(iter(self._callback_users)))  # oldest first
		self._callback_users[callback_id] = user_id

	# ------------------------------------------------------------ outgoing
	async def send_message(self, text: str, *, user_id: int, keyboard: Keyboard | None = None) -> Message:
		body, keyboard = move_unlinkable_buttons(text, keyboard)
		attachments = [keyboard_attachment(keyboard)] if keyboard is not None else None
		sent = await self.api.send_message(user_id=user_id, text=body, format="html", attachments=attachments)
		return self._message(sent)

	async def edit_message(self, message_id: str, text: str | None, *, keyboard: Keyboard | None = None) -> None:
		new_text, keyboard = move_unlinkable_buttons(text, keyboard)
		# An empty list removes the old keyboard: an edit without one drops it.
		attachments: list[m.AttachmentRequest] = [keyboard_attachment(keyboard)] if keyboard is not None else []
		await self.api.edit_message(message_id, text=new_text, format="html", attachments=attachments)

	async def delete_message(self, message_id: str) -> None:
		await self.api.delete_message(message_id)

	async def answer_callback(self, callback_id: str, *, text: str | None = None, alert: bool = False) -> None:
		user_id = self._callback_users.pop(callback_id, None)
		await self.api.answer_callback(callback_id, notification=text or "")
		if alert and text and user_id is not None:
			await self.send_message(html.escape(text, quote=False), user_id=user_id)

	async def send_document(
		self, content: bytes, filename: str, *, user_id: int, caption: str | None = None
	) -> Message:
		attachment = await self.api.upload(content, type="file", filename=filename)
		sent = await self.api.send_message(
			user_id=user_id, text=caption, format="html" if caption else None, attachments=[attachment]
		)
		return self._message(sent)

	async def send_file_by_token(
		self, token: str, *, user_id: int, file_type: str = "file", caption: str | None = None
	) -> Message:
		attachment = _attachment_by_token(file_type, token)
		sent = await self.api.send_message(
			user_id=user_id, text=caption, format="html" if caption else None, attachments=[attachment]
		)
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


def _attachment_by_token(file_type: str, token: str) -> m.AttachmentRequest:
	if file_type == "image":
		return m.PhotoAttachmentRequest(payload=m.PhotoAttachmentRequestPayload(token=token))
	cls = {"video": m.VideoAttachmentRequest, "audio": m.AudioAttachmentRequest}.get(file_type, m.FileAttachmentRequest)
	return cls(payload=m.UploadedInfo(token=token))


def keyboard_attachment(keyboard: Keyboard) -> m.InlineKeyboardAttachmentRequest:
	rows: list[list[m.Button]] = [[_button(b) for b in row] for row in keyboard.rows]
	return m.InlineKeyboardAttachmentRequest(payload=m.InlineKeyboardAttachmentRequestPayload(buttons=rows))


def _button(button: Button) -> m.Button:
	if button.url is not None:
		return m.LinkButton(text=button.text, url=button.url)
	return m.CallbackButton(text=button.text, payload=button.data or "noop")


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
	lines = "".join(f"\n{html.escape(b.text)}: {html.escape(b.url or '')}" for b in moved)
	return (text or "") + "\n" + lines, Keyboard(rows) if rows else None
