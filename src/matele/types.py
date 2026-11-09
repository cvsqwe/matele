"""Messenger-agnostic types. Each transport builds these from its own wire
format; `bot` on a message or callback is the client that received it, so
a reply always goes back through the same messenger."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

MAX = "max"
TELEGRAM = "telegram"

# Normalised chat types (each transport maps its own onto these).
PRIVATE = "private"
GROUP = "group"


@dataclass
class Button:
	text: str
	data: str | None = None  # callback payload
	url: str | None = None  # link button


@dataclass
class Keyboard:
	rows: list[list[Button]]


@dataclass
class User:
	id: int
	platform: str
	name: str | None = None
	username: str | None = None


@dataclass
class Attachment:
	"""An incoming file, normalised. `token` is whatever id lets the same
	transport re-send it (MAX upload token / Telegram file_id); `type` is
	the transport's own kind (file, image, video, audio, voice, …)."""

	type: str
	token: str
	filename: str
	url: str | None = None


@dataclass
class Message:
	# Opaque per-transport id, only ever passed back to the same transport.
	message_id: str
	chat_id: int
	from_user: User
	text: str | None
	bot: BotClient = field(repr=False)
	attachments: list[Attachment] = field(default_factory=list)
	chat_type: str = PRIVATE  # PRIVATE or GROUP

	@property
	def is_private(self) -> bool:
		return self.chat_type == PRIVATE


@dataclass
class CallbackQuery:
	id: str
	data: str
	from_user: User
	message: Message | None
	bot: BotClient = field(repr=False)


Event = Message | CallbackQuery


@dataclass
class ChatStatus:
	"""The person blocked (reachable=False) or restarted the bot in their
	private chat."""

	from_user: User
	reachable: bool
	bot: BotClient = field(repr=False)


@dataclass
class ChatMemberUpdate:
	"""Someone joined or left a group. `member` is who joined/left
	(`is_me`: the bot itself was added or removed); `from_user` is who did
	it, if known (the member themselves when they joined/left on their own)."""

	chat_id: int
	member: User
	joined: bool
	is_me: bool
	from_user: User
	bot: BotClient = field(repr=False)
	chat_type: str = GROUP


IncomingEvent = Event | ChatStatus | ChatMemberUpdate


class BotClient(Protocol):
	"""What every transport offers the framework.

	Outgoing calls take a recipient as `chat_id=` (a chat the bot is in,
	e.g. a group) or `user_id=` (a person's private chat with the bot);
	exactly one of them."""

	platform: str
	title: str
	me: User | None  # the bot itself, known after get_me()

	async def close(self) -> None: ...

	async def get_me(self) -> User: ...

	def deep_link(self, username: str, payload: str) -> str: ...

	async def get_events(self, timeout: int = 30) -> list[IncomingEvent]: ...

	async def send_message(
		self,
		text: str,
		*,
		chat_id: int | None = None,
		user_id: int | None = None,
		keyboard: Keyboard | None = None,
	) -> Message: ...

	async def edit_message(self, message_id: str, text: str | None, *, keyboard: Keyboard | None = None) -> None: ...

	async def delete_message(self, message_id: str) -> None: ...

	async def answer_callback(self, callback_id: str, *, text: str | None = None, alert: bool = False) -> None: ...

	async def send_document(
		self,
		content: bytes,
		filename: str,
		*,
		chat_id: int | None = None,
		user_id: int | None = None,
		caption: str | None = None,
	) -> Message: ...

	async def send_file_by_token(
		self,
		token: str,
		*,
		chat_id: int | None = None,
		user_id: int | None = None,
		file_type: str = "file",
		caption: str | None = None,
	) -> Message: ...

	async def download_file(
		self, token: str, *, file_type: str, filename: str, url: str | None = None
	) -> tuple[bytes, str]: ...


def recipient(chat_id: int | None, user_id: int | None) -> tuple[int | None, int | None]:
	"""Checks that exactly one recipient is given (transports call it)."""
	if (chat_id is None) == (user_id is None):
		raise ValueError("pass exactly one of chat_id=, user_id=")
	return chat_id, user_id
