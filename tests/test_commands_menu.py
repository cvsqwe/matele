from __future__ import annotations

import asyncio

import pytest

from matele import Context, Dispatcher, Router
from matele.callbacks import CallbackRegistry
from matele.max import MaxBot
from matele.telegram import TelegramBot
from matele.testing import FakeBot
from tests.max_fake import FakeMaxServer
from tests.telegram_fake import TOKEN, FakeTelegram


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


async def test_the_menu_is_published_once_the_messenger_is_reached(monkeypatch):
	dp = Dispatcher(registry=CallbackRegistry())

	@dp.command("start", description="Начать")
	async def start(ctx: Context): ...

	class Offline(FakeBot):
		"""No network until the first get_me() fails: then it comes back."""

		online = False

		async def get_me(self):
			if not self.online:
				self.online = True
				raise ConnectionError("no network at start")
			return await super().get_me()

		async def set_commands(self, commands):
			if not self.online:
				raise ConnectionError("no network at start")
			await super().set_commands(commands)

	async def no_sleep(delay: float) -> None:
		await asyncio.sleep(0)

	monkeypatch.setattr("matele.dispatch.polling._sleep", no_sleep)
	bot = Offline()
	task = asyncio.create_task(dp.start_polling(bot, timeout=1))
	for _ in range(20):
		await asyncio.sleep(0)
	task.cancel()
	with pytest.raises(asyncio.CancelledError):
		await task
	assert bot.commands == [("start", "Начать")]
