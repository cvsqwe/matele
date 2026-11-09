from __future__ import annotations

import asyncio

import pytest

from matele import Context, Dispatcher, Router
from matele.callbacks import CallbackRegistry
from matele.max import MaxBot
from matele.telegram import TelegramBot
from matele.testing import FakeBot
from tests.max_fake import FakeMaxServer
from tests.test_telegram import TOKEN, FakeTelegram


@pytest.fixture
def dp() -> Dispatcher:
	return Dispatcher(registry=CallbackRegistry())


def test_only_described_commands_go_to_the_menu(dp):
	extra = Router("extra")

	@dp.command("start", description="Начать")
	async def start(ctx: Context): ...

	@dp.command("admin")  # hidden: no description
	async def admin(ctx: Context): ...

	@extra.command("help", "h", description="Помощь")  # the first name goes to the menu
	async def help_(ctx: Context): ...

	dp.include(extra)
	assert dp.menu_commands() == [("start", "Начать"), ("help", "Помощь")]


@pytest.mark.parametrize(
	("names", "description"),
	[
		(("Start-Now",), "x"),  # not a-z, 0-9, _
		(("a" * 33,), "x"),  # too long
		(("ok",), ""),  # empty description
		(("ok",), "x" * 257),
	],
)
def test_menu_names_and_descriptions_are_validated(dp, names, description):
	with pytest.raises(ValueError):
		dp.command(*names, description=description)


async def test_published_on_polling_start(dp):
	bot = FakeBot()

	@dp.command("start", description="Начать")
	async def start(ctx: Context): ...

	task = asyncio.create_task(dp.start_polling(bot, timeout=1))
	for _ in range(10):
		await asyncio.sleep(0)
	task.cancel()
	with pytest.raises(asyncio.CancelledError):
		await task
	assert bot.commands == [("start", "Начать")]


async def test_nothing_published_without_descriptions(dp):
	bot = FakeBot()

	@dp.command("start")
	async def start(ctx: Context): ...

	await dp.publish_commands(bot)
	assert bot.commands == []


async def test_a_failing_messenger_does_not_stop_the_others(dp, caplog):
	class Broken(FakeBot):
		async def set_commands(self, commands):
			raise RuntimeError("no rights")

	good, broken = FakeBot(), Broken()

	@dp.command("start", description="Начать")
	async def start(ctx: Context): ...

	await dp.publish_commands(broken, good)
	assert good.commands == [("start", "Начать")]
	assert "could not set the command menu" in caplog.text


async def test_wire_formats():
	server = FakeMaxServer()
	server.on("PATCH", "/me/commands", {"commands": []})
	await MaxBot("t", api=server.api()).set_commands([("start", "Начать")])
	assert server.calls[0].json == {"commands": [{"name": "start", "description": "Начать"}]}

	tg = FakeTelegram()
	await TelegramBot(TOKEN, api=tg.api()).set_commands([("start", "Начать")])
	assert tg.calls[0][0] == "setMyCommands"
	assert tg.calls[0][1] == {"commands": [{"command": "start", "description": "Начать"}]}
