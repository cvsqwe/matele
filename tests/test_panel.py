from __future__ import annotations

import pytest

from matele import MAX, TELEGRAM, Button, Context, Dispatcher, Keyboard, State, action
from matele.callbacks import CallbackRegistry
from matele.dispatch.panel import PANEL_KEY
from matele.testing import FakeBot

reg = CallbackRegistry()
OPEN = action("open", registry=reg)
ASK = State("ask")
KB = Keyboard([[Button("Открыть", OPEN.pack())]])


@pytest.fixture
def dp() -> Dispatcher:
	dp = Dispatcher(registry=reg)

	@dp.command("menu")
	async def menu(ctx: Context):
		await ctx.show("Меню", KB)

	@dp.callback(OPEN)
	async def opened(ctx: Context, payload):
		ctx.state.set(ASK)
		await ctx.show("Как назвать?", toast="Открыто")

	@dp.message(state=ASK)
	async def named(ctx: Context):
		ctx.state.clear()
		await ctx.show(f"Готово: {ctx.text}", KB, keep_input=ctx.text == "keep")

	return dp


async def test_button_press_edits_the_menu_in_place(dp):
	bot = FakeBot()
	await dp.feed(bot.message("/menu"))
	menu = bot.sent[0].message
	await dp.feed(bot.press(OPEN, message=menu))
	assert len(bot.sent) == 1  # no new message
	assert bot.edits[-1].message_id == menu.message_id and bot.edits[-1].text == "Как назвать?"
	assert bot.answers[-1].text == "Открыто"


async def test_typed_answer_is_deleted_and_the_menu_right_above_is_edited(dp):
	bot = FakeBot(platform=TELEGRAM)  # Telegram numbers messages: "right above" can be told
	await dp.feed(bot.message("/menu"))
	menu = bot.sent[0].message
	await dp.feed(bot.press(OPEN, message=menu))
	answer = bot.message("Кот")
	await dp.feed(answer)
	assert answer.message_id in bot.deleted
	assert len(bot.sent) == 1 and bot.edits[-1].message_id == menu.message_id
	assert bot.edits[-1].text == "Готово: Кот"


async def test_menu_far_up_the_chat_is_replaced_at_the_bottom(dp):
	bot = FakeBot()
	await dp.feed(bot.message("/menu"))
	old = bot.sent[0].message
	await bot.send_message("какое-то уведомление", user_id=1)  # pushes the menu up
	await dp.feed(bot.message("/menu"))
	new = bot.sent[-1].message
	assert new.message_id != old.message_id and new.text == "Меню"
	assert old.message_id in bot.deleted  # the old menu is retired


async def test_keep_input_leaves_the_message(dp):
	bot = FakeBot()
	await dp.feed(bot.message("/menu"))
	await dp.feed(bot.press(OPEN, message=bot.sent[0].message))
	kept = bot.message("keep")
	await dp.feed(kept)
	assert kept.message_id not in bot.deleted
	assert bot.sent[-1].text == "Готово: keep"  # a new menu below the kept message


async def test_menu_id_survives_clearing_the_state(dp):
	bot = FakeBot()
	await dp.feed(bot.message("/menu"))
	await dp.feed(bot.press(OPEN, message=bot.sent[0].message))
	await dp.feed(bot.message("x"))  # the handler calls ctx.state.clear()
	stored = await dp.storage.get(dp.storage_key(bot.message("y")))
	assert stored is not None and stored.state is None and stored.meta[PANEL_KEY] == bot.sent[-1].message.message_id


async def test_on_max_a_typed_message_gets_a_fresh_menu(dp):
	bot = FakeBot(platform=MAX)  # MAX message ids can't be compared, as in the real transport
	await dp.feed(bot.message("/menu"))
	menu = bot.sent[0].message
	await dp.feed(bot.press(OPEN, message=menu))
	answer = bot.message("Кот")
	await dp.feed(answer)
	assert answer.message_id in bot.deleted
	assert len(bot.sent) == 2 and bot.sent[-1].text == "Готово: Кот"  # sent anew at the bottom
	assert menu.message_id in bot.deleted  # and the old menu retired


def test_fake_bot_numbers_messages_only_as_telegram():
	assert FakeBot(platform=TELEGRAM).message_sequence("12") == 12
	assert FakeBot(platform=MAX).message_sequence("12") is None


async def test_a_menu_that_cannot_be_deleted_loses_its_buttons(dp):
	bot = FakeBot()
	await dp.feed(bot.message("/menu"))
	old = bot.sent[0].message
	await bot.send_message("notice", user_id=1)

	async def refuse(message_id: str) -> None:
		from matele import BotApiError

		raise BotApiError(400, "x", "message can't be deleted")

	bot.delete_message = refuse  # type: ignore[method-assign]
	await dp.feed(bot.message("/menu"))
	assert bot.edits[-1].message_id == old.message_id and bot.edits[-1].text is None and bot.edits[-1].keyboard is None


def test_message_order_per_messenger():
	from matele.max import MaxBot
	from matele.telegram import TelegramBot
	from tests.max_fake import FakeMaxServer
	from tests.telegram_fake import TOKEN, FakeTelegram

	tg = TelegramBot(TOKEN, api=FakeTelegram().api())
	assert tg.message_sequence("7:10") == 10 and tg.message_sequence("garbage") is None
	assert not hasattr(MaxBot("t", api=FakeMaxServer().api()), "message_sequence")  # MAX ids are opaque
