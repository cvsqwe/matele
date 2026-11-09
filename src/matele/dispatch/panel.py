"""Single-message menus: every screen is shown in ONE message per person
(per chat), edited in place, instead of a new message per tap.

	@dp.command("menu")
	async def menu(ctx):
		await ctx.show("Главное меню", main_keyboard)

	@dp.callback(OpenOrders)
	async def orders(ctx, payload):
		await ctx.show("Ваши заказы", orders_keyboard, toast="Загружено")

Where a screen goes:
  • a button press edits the message the button is on;
  • a typed message (a command, an answer to a question, a file) is
	deleted after reading, so the chat doesn't fill up with input. The menu
	is then edited in place if it is right above that message; otherwise
	(it is far up the chat) a new one is sent at the bottom and the old one
	deleted, or, if the messenger refuses, left without buttons. The person
	always sees the answer right under what they typed.
	Whether "right above" can be told depends on the messenger: Telegram
	numbers messages, MAX doesn't, so on MAX a typed message always gets a
	fresh menu;
  • keep_input=True leaves the person's message in the chat (e.g. a long
	text that failed validation, so it can be copied and fixed).

The menu's message id is kept in the conversation state (`ctx.state.meta`),
so it survives `ctx.state.clear()` and, with a persistent storage, restarts.
Notifications that aren't screens should stay separate messages
(`ctx.reply`).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from matele.errors import BotApiError
from matele.types import BotClient, CallbackQuery, Keyboard, Message

if TYPE_CHECKING:
	from matele.dispatch.context import Context

logger = logging.getLogger(__name__)

PANEL_KEY = "matele.panel"


def sequence(bot: BotClient, message_id: str | None) -> int | None:
	"""The message's position in its chat, if the messenger tells it."""
	if not message_id:
		return None
	order = getattr(bot, "message_sequence", None)
	return order(message_id) if order is not None else None


def is_right_above(bot: BotClient, panel_id: str | None, message_id: str | None) -> bool:
	"""Is the menu directly above (or below) the person's message? Unknown
	counts as no."""
	panel, message = sequence(bot, panel_id), sequence(bot, message_id)
	return panel is not None and message is not None and message - panel <= 1


async def _retire(bot: BotClient, message_id: str) -> None:
	"""Removes an old menu; failing that, strips its now-stale buttons."""
	try:
		await bot.delete_message(message_id)
		return
	except BotApiError:
		pass
	try:
		await bot.edit_message(message_id, None, keyboard=None)
	except BotApiError:
		logger.debug("Could not retire the old menu %s", message_id)


async def show(
	ctx: Context,
	text: str,
	keyboard: Keyboard | None = None,
	*,
	toast: str | None = None,
	keep_input: bool = False,
) -> Message | None:
	"""Shows a screen in the person's menu message. Returns the message if a
	new one was sent (None when an existing one was edited)."""
	event, bot, meta = ctx.event, ctx.bot, ctx.state.meta
	if isinstance(event, CallbackQuery) and event.message is not None and event.message.message_id:
		try:
			await bot.edit_message(event.message.message_id, text, keyboard=keyboard)
			meta[PANEL_KEY] = event.message.message_id
			await ctx.answer(toast)
			return None
		except BotApiError:
			pass  # too old to edit: send a fresh menu below

	old = meta.get(PANEL_KEY)
	if isinstance(event, Message) and event.message_id:
		if not keep_input:
			try:
				await bot.delete_message(event.message_id)
			except BotApiError:
				logger.debug("Could not delete the input message %s", event.message_id)
		if old and not keep_input and is_right_above(bot, old, event.message_id):
			try:
				await bot.edit_message(old, text, keyboard=keyboard)
				return None
			except BotApiError:
				pass  # the menu is gone: send a new one

	sent = await ctx.reply(text, keyboard)
	meta[PANEL_KEY] = sent.message_id
	if old and old != sent.message_id:
		await _retire(bot, old)
	await ctx.answer(toast)
	return sent
