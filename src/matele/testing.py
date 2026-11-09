"""A fake messenger for testing bots without the network.

	bot = FakeBot()
	await dp.feed(bot.message("/start"))
	assert bot.sent[-1].text == "Привет!"

	await dp.feed(bot.press(SetStatus(42, "paid"), message=bot.sent[-1].message))
	assert bot.answers[-1].text == "Готово"

	# in a group: pass chat_id
	await dp.feed(bot.message("/stats", user_id=1, chat_id=-100))
	assert bot.sent[-1].chat_id == -100

FakeBot implements the BotClient protocol and records every call. Events
can also be queued with `push()` for code that polls (`get_events`).
"""

from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass, field
from pathlib import Path

from matele.callbacks import CallbackData
from matele.errors import BotApiError
from matele.types import (
	GROUP,
	MAX,
	PRIVATE,
	Attachment,
	CallbackQuery,
	ChatMemberUpdate,
	ChatStatus,
	FileKind,
	FileSource,
	IncomingEvent,
	Keyboard,
	Message,
	User,
	recipient,
)


@dataclass
class Sent:
	chat_id: int | None  # set when sent to a chat (group)
	user_id: int | None  # set when sent to a person's private chat
	text: str | None  # the message text, or a file's caption
	keyboard: Keyboard | None
	message: Message
	document: tuple[str, bytes] | None = None  # (filename, content) of a sent file
	kind: str | None = None  # "image", "video", "audio", "file" for a sent file
	reply_to: str | None = None


@dataclass
class Edit:
	message_id: str
	text: str | None
	keyboard: Keyboard | None


@dataclass
class Answer:
	callback_id: str
	text: str | None
	alert: bool


@dataclass
class FakeBot:
	platform: str = MAX
	title: str = "Fake"
	username: str = "fake_bot"
	bot_id: int = 999
	sent: list[Sent] = field(default_factory=list)
	edits: list[Edit] = field(default_factory=list)
	deleted: list[str] = field(default_factory=list)
	answers: list[Answer] = field(default_factory=list)
	typing: list[int] = field(default_factory=list)  # chat ids "typing…" was shown in
	commands: list[tuple[str, str]] = field(default_factory=list)  # the command menu
	closed: bool = False

	def __post_init__(self) -> None:
		self.me: User | None = User(id=self.bot_id, platform=self.platform, name=self.title, username=self.username)
		self._ids = itertools.count(1)
		self._callback_ids = itertools.count(1)  # presses don't take message numbers
		self._queue: asyncio.Queue[IncomingEvent] = asyncio.Queue()
		self._files: dict[str, tuple[bytes, str]] = {}
		self._live: set[str] = set()  # message ids that exist (can be edited/deleted)

	# ------------------------------------------------------------ building events
	def user(self, user_id: int = 1, name: str | None = None, username: str | None = None) -> User:
		return User(id=user_id, platform=self.platform, name=name, username=username)

	def message(
		self,
		text: str | None = None,
		*,
		user_id: int = 1,
		chat_id: int | None = None,
		attachments: list[Attachment] | None = None,
	) -> Message:
		"""An incoming message from a person: in their private chat, or in
		the group `chat_id`."""
		message_id = self._next_id()
		self._live.add(message_id)
		return Message(
			message_id=message_id,
			chat_id=chat_id if chat_id is not None else user_id,
			from_user=self.user(user_id),
			text=text,
			bot=self,
			attachments=attachments or [],
			chat_type=GROUP if chat_id is not None else PRIVATE,
		)

	def press(self, data: str | CallbackData, *, user_id: int = 1, message: Message | None = None) -> CallbackQuery:
		"""A person pressing a button (with this payload) on `message`."""
		raw = data.pack() if isinstance(data, CallbackData) else data
		return CallbackQuery(
			id=f"cb{next(self._callback_ids)}", data=raw, from_user=self.user(user_id), message=message, bot=self
		)

	def blocked(self, *, user_id: int = 1, reachable: bool = False) -> ChatStatus:
		return ChatStatus(from_user=self.user(user_id), reachable=reachable, bot=self)

	def member(self, chat_id: int, *, user_id: int = 1, joined: bool = True, by: int | None = None) -> ChatMemberUpdate:
		"""Someone (or the bot itself, user_id=bot.bot_id) joined or left a group."""
		member = self.me if user_id == self.bot_id and self.me is not None else self.user(user_id)
		return ChatMemberUpdate(
			chat_id=chat_id,
			member=member,
			joined=joined,
			is_me=user_id == self.bot_id,
			from_user=self.user(by) if by is not None else member,
			bot=self,
		)

	def push(self, *events: IncomingEvent) -> None:
		for event in events:
			self._queue.put_nowait(event)

	@property
	def texts(self) -> list[str | None]:
		return [s.text for s in self.sent]

	# ------------------------------------------------------------ BotClient
	async def close(self) -> None:
		self.closed = True

	async def get_me(self) -> User:
		assert self.me is not None
		return self.me

	def deep_link(self, username: str, payload: str) -> str:
		return f"https://fake.example/{username}?start={payload}" if payload else f"https://fake.example/{username}"

	async def get_events(self, timeout: int = 30) -> list[IncomingEvent]:
		try:
			first = await asyncio.wait_for(self._queue.get(), timeout)
		except TimeoutError:
			return []
		events = [first]
		while not self._queue.empty():
			events.append(self._queue.get_nowait())
		return events

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
		message = self._outgoing(chat_id, user_id, text)
		self.sent.append(Sent(chat_id, user_id, text, keyboard, message, reply_to=reply_to))
		return message

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
		if isinstance(source, bytes):
			content, name = source, filename or "file"
		elif isinstance(source, Path):
			content, name = source.read_bytes(), filename or source.name
		else:
			content, name = source.read(), filename or "file"
		token = f"file{self._next_id()}"
		self._files[token] = (content, name)
		message = self._outgoing(chat_id, user_id, caption, [Attachment(kind, token, name)])
		self.sent.append(Sent(chat_id, user_id, caption, keyboard, message, (name, content), kind, reply_to))
		return message

	async def send_typing(self, chat_id: int) -> None:
		self.typing.append(chat_id)

	async def set_commands(self, commands: list[tuple[str, str]]) -> None:
		self.commands = list(commands)

	async def edit_message(self, message_id: str, text: str | None, *, keyboard: Keyboard | None = None) -> None:
		self._require(message_id)
		self.edits.append(Edit(message_id, text, keyboard))

	async def delete_message(self, message_id: str) -> None:
		self._require(message_id)
		self._live.discard(message_id)
		self.deleted.append(message_id)

	async def answer_callback(self, callback_id: str, *, text: str | None = None, alert: bool = False) -> None:
		self.answers.append(Answer(callback_id, text, alert))

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
		content, filename = await self.download_file(token, file_type=file_type, filename="")
		message = self._outgoing(chat_id, user_id, caption, [Attachment(file_type, token, filename)])
		self.sent.append(Sent(chat_id, user_id, caption, None, message, (filename, content), file_type))
		return message

	async def download_file(
		self, token: str, *, file_type: str, filename: str, url: str | None = None
	) -> tuple[bytes, str]:
		if token not in self._files:
			raise BotApiError(404, "file.not.found", f"no file {token}", platform=self.platform)
		content, stored_name = self._files[token]
		return content, filename or stored_name

	def message_sequence(self, message_id: str) -> int | None:
		"""FakeBot numbers all messages in one sequence, like Telegram does per chat."""
		return int(message_id) if message_id.isdigit() else None

	# ------------------------------------------------------------ internals
	def _next_id(self) -> str:
		return str(next(self._ids))

	def _outgoing(
		self, chat_id: int | None, user_id: int | None, text: str | None, attachments: list[Attachment] | None = None
	) -> Message:
		message_id = self._next_id()
		self._live.add(message_id)
		assert self.me is not None
		return Message(
			message_id=message_id,
			chat_id=chat_id if chat_id is not None else user_id or 0,
			from_user=self.me,
			text=text,
			bot=self,
			attachments=attachments or [],
			chat_type=GROUP if chat_id is not None else PRIVATE,
		)

	def _require(self, message_id: str) -> None:
		if message_id not in self._live:
			raise BotApiError(400, "message.not.found", f"no message {message_id}", platform=self.platform)
