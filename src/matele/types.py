"""Messenger-agnostic types. Each transport builds these from its own wire
format; `bot` on a message or callback is the client that received it, so
a reply always goes back through the same messenger."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

MAX = "max"
TELEGRAM = "telegram"


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
	"""The person blocked (reachable=False) or restarted the bot."""

	from_user: User
	reachable: bool
	bot: BotClient = field(repr=False)


IncomingEvent = Event | ChatStatus


class BotClient(Protocol):
	"""What every transport offers the framework. Addressed by the
	recipient's user id: private chats only for now."""

	platform: str
	title: str

	async def close(self) -> None: ...

	async def get_me(self) -> User: ...

	def deep_link(self, username: str, payload: str) -> str: ...

	async def get_events(self, timeout: int = 30) -> list[IncomingEvent]: ...

	async def send_message(self, text: str, *, user_id: int, keyboard: Keyboard | None = None) -> Message: ...

	async def edit_message(self, message_id: str, text: str | None, *, keyboard: Keyboard | None = None) -> None: ...

	async def delete_message(self, message_id: str) -> None: ...

	async def answer_callback(self, callback_id: str, *, text: str | None = None, alert: bool = False) -> None: ...

	async def send_document(
		self, content: bytes, filename: str, *, user_id: int, caption: str | None = None
	) -> Message: ...

	async def send_file_by_token(
		self, token: str, *, user_id: int, file_type: str = "file", caption: str | None = None
	) -> Message: ...

	async def download_file(
		self, token: str, *, file_type: str, filename: str, url: str | None = None
	) -> tuple[bytes, str]: ...
