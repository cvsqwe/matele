"""A fake messenger for testing bots without the network.

	bot = FakeBot()
	await dp.feed(bot.message("/start"))
	assert bot.sent[-1].text == "Привет!"

	await dp.feed(bot.press(SetStatus(42, "paid"), message=bot.sent[-1].message))
	assert bot.answers[-1].text == "Готово"

FakeBot implements the BotClient protocol and records every call. Events
can also be queued with `push()` for code that polls (`get_events`).
"""

from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass, field

from matele.callbacks import CallbackData
from matele.errors import BotApiError
from matele.types import (
	MAX,
	Attachment,
	CallbackQuery,
	ChatStatus,
	IncomingEvent,
	Keyboard,
	Message,
	User,
)


@dataclass
class Sent:
	user_id: int
	text: str | None
	keyboard: Keyboard | None
	message: Message
	document: tuple[str, bytes] | None = None  # (filename, content)


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
	closed: bool = False

	def __post_init__(self) -> None:
		self._ids = itertools.count(1)
		self._queue: asyncio.Queue[IncomingEvent] = asyncio.Queue()
		self._files: dict[str, tuple[bytes, str]] = {}
		self._live: set[str] = set()  # message ids that exist (can be edited/deleted)

	# ------------------------------------------------------------ building events
	def user(self, user_id: int = 1, name: str | None = None, username: str | None = None) -> User:
		return User(id=user_id, platform=self.platform, name=name, username=username)

	def message(
		self, text: str | None = None, *, user_id: int = 1, attachments: list[Attachment] | None = None
	) -> Message:
		"""An incoming message from a person."""
		message_id = self._next_id()
		self._live.add(message_id)
		return Message(
			message_id=message_id,
			chat_id=user_id,
			from_user=self.user(user_id),
			text=text,
			bot=self,
			attachments=attachments or [],
		)

	def press(self, data: str | CallbackData, *, user_id: int = 1, message: Message | None = None) -> CallbackQuery:
		"""A person pressing a button (with this payload) on `message`."""
		raw = data.pack() if isinstance(data, CallbackData) else data
		return CallbackQuery(
			id=f"cb{self._next_id()}", data=raw, from_user=self.user(user_id), message=message, bot=self
		)

	def blocked(self, *, user_id: int = 1, reachable: bool = False) -> ChatStatus:
		return ChatStatus(from_user=self.user(user_id), reachable=reachable, bot=self)

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
		return User(id=self.bot_id, platform=self.platform, name=self.title, username=self.username)

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

	async def send_message(self, text: str, *, user_id: int, keyboard: Keyboard | None = None) -> Message:
		message = self._outgoing(user_id, text)
		self.sent.append(Sent(user_id, text, keyboard, message))
		return message

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
		self, content: bytes, filename: str, *, user_id: int, caption: str | None = None
	) -> Message:
		token = f"file{self._next_id()}"
		self._files[token] = (content, filename)
		message = self._outgoing(user_id, caption, [Attachment("file", token, filename)])
		self.sent.append(Sent(user_id, caption, None, message, (filename, content)))
		return message

	async def send_file_by_token(
		self, token: str, *, user_id: int, file_type: str = "file", caption: str | None = None
	) -> Message:
		content, filename = await self.download_file(token, file_type=file_type, filename="")
		message = self._outgoing(user_id, caption, [Attachment(file_type, token, filename)])
		self.sent.append(Sent(user_id, caption, None, message, (filename, content)))
		return message

	async def download_file(
		self, token: str, *, file_type: str, filename: str, url: str | None = None
	) -> tuple[bytes, str]:
		if token not in self._files:
			raise BotApiError(404, "file.not.found", f"no file {token}", platform=self.platform)
		content, stored_name = self._files[token]
		return content, filename or stored_name

	# ------------------------------------------------------------ internals
	def _next_id(self) -> str:
		return str(next(self._ids))

	def _outgoing(self, user_id: int, text: str | None, attachments: list[Attachment] | None = None) -> Message:
		message_id = self._next_id()
		self._live.add(message_id)
		me = User(id=self.bot_id, platform=self.platform, username=self.username)
		return Message(message_id, user_id, me, text, self, attachments or [])

	def _require(self, message_id: str) -> None:
		if message_id not in self._live:
			raise BotApiError(400, "message.not.found", f"no message {message_id}", platform=self.platform)
