"""Context: the one object every handler, filter and middleware receives.

Subclass it to give handlers typed access to whatever your middleware
loads, and pass the subclass to the Dispatcher:

	class Ctx(Context):
		account: Account | None = None

	dp = Dispatcher(context_class=Ctx)

	@dp.middleware
	async def load_account(ctx: Ctx, call_next):
		ctx.account = await accounts.find(ctx.platform, ctx.user.id)
		await call_next()
"""

from __future__ import annotations

import logging
import re
from typing import Any

from matele.callbacks import CallbackData
from matele.errors import BotApiError
from matele.fsm import FSMContext
from matele.types import (
	PRIVATE,
	BotClient,
	CallbackQuery,
	ChatMemberUpdate,
	ChatStatus,
	IncomingEvent,
	Keyboard,
	Message,
	User,
)

logger = logging.getLogger(__name__)


class Context:
	def __init__(self, event: IncomingEvent, state: FSMContext):
		self.event = event
		self.state = state
		# Free-form storage for middleware and filters.
		self.data: dict[str, Any] = {}
		# Set by the dispatcher before filters run:
		self.payload: CallbackData | None = None  # parsed button payload (None if it didn't parse)
		self.command: str | None = None  # "start" for "/start abc"
		self.args: str | None = None  # "abc" for "/start abc"
		self.match: re.Match[str] | None = None  # set by the Regex filter
		self.answered = False

	# ------------------------------------------------------------ the event
	@property
	def bot(self) -> BotClient:
		return self.event.bot

	@property
	def user(self) -> User:
		return self.event.from_user

	@property
	def platform(self) -> str:
		return self.event.from_user.platform

	@property
	def callback(self) -> CallbackQuery | None:
		return self.event if isinstance(self.event, CallbackQuery) else None

	@property
	def is_callback(self) -> bool:
		return isinstance(self.event, CallbackQuery)

	@property
	def message(self) -> Message | None:
		"""The incoming message, or the message a pressed button is on."""
		if isinstance(self.event, Message):
			return self.event
		if isinstance(self.event, CallbackQuery):
			return self.event.message
		return None

	@property
	def chat_status(self) -> ChatStatus | None:
		return self.event if isinstance(self.event, ChatStatus) else None

	@property
	def member_update(self) -> ChatMemberUpdate | None:
		return self.event if isinstance(self.event, ChatMemberUpdate) else None

	@property
	def chat_type(self) -> str:
		"""matele.PRIVATE or matele.GROUP: where the event happened."""
		if isinstance(self.event, ChatMemberUpdate):
			return self.event.chat_type
		message = self.message
		return message.chat_type if message is not None else PRIVATE

	@property
	def chat_id(self) -> int | None:
		"""The group the event happened in; None in a private chat."""
		if isinstance(self.event, ChatMemberUpdate):
			return self.event.chat_id
		message = self.message
		return message.chat_id if message is not None and message.chat_type != PRIVATE else None

	def _target(self) -> dict[str, int]:
		chat_id = self.chat_id
		return {"chat_id": chat_id} if chat_id is not None else {"user_id": self.user.id}

	@property
	def text(self) -> str:
		"""Text of an incoming message ("" for anything else)."""
		return (self.event.text or "") if isinstance(self.event, Message) else ""

	# ------------------------------------------------------------ output
	async def reply(self, text: str, keyboard: Keyboard | None = None) -> Message:
		"""Sends a new message where the event happened: to the group, or to
		the person's private chat."""
		return await self.bot.send_message(text, keyboard=keyboard, **self._target())

	async def reply_document(self, content: bytes, filename: str, caption: str | None = None) -> Message:
		return await self.bot.send_document(content, filename, caption=caption, **self._target())

	async def send_private(self, text: str, keyboard: Keyboard | None = None) -> Message:
		"""Sends to the person's private chat with the bot, even from a group
		(fails if they never started the bot)."""
		return await self.bot.send_message(text, user_id=self.user.id, keyboard=keyboard)

	async def edit(self, text: str | None, keyboard: Keyboard | None = None) -> None:
		"""Edits the message the pressed button is on."""
		target = self.event.message if isinstance(self.event, CallbackQuery) else None
		if target is None or not target.message_id:
			raise RuntimeError("edit() needs a pressed button on a message the bot can edit")
		await self.bot.edit_message(target.message_id, text, keyboard=keyboard)

	async def delete(self) -> None:
		"""Deletes the incoming message, or the message a pressed button is on."""
		target = self.message
		if target is None or not target.message_id:
			raise RuntimeError("delete() needs a message")
		await self.bot.delete_message(target.message_id)

	async def answer(self, text: str | None = None, *, alert: bool = False) -> None:
		"""Answers a button press: stops the spinner, optionally with a toast
		(or an alert). Only the first call counts; the dispatcher answers
		silently after the handler if nobody did. No-op for other events."""
		if not isinstance(self.event, CallbackQuery) or self.answered:
			return
		self.answered = True
		try:
			await self.bot.answer_callback(self.event.id, text=text, alert=alert)
		except BotApiError as exc:  # e.g. Telegram's "query is too old"
			logger.debug("answer_callback failed: %s", exc)
