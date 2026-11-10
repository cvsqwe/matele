"""A token the messenger refuses stops polling at once; outages don't."""

from __future__ import annotations

import asyncio

import pytest

from matele import Context, Dispatcher, InvalidTokenError
from matele.callbacks import CallbackRegistry
from matele.max import MaxBot
from matele.telegram import TelegramBot
from matele.testing import FakeBot
from tests.max_fake import FakeMaxServer
from tests.test_telegram import TOKEN, FakeTelegram


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
	async def no_sleep(delay: float) -> None:
		await asyncio.sleep(0)

	monkeypatch.setattr("matele.dispatch.polling._sleep", no_sleep)
	monkeypatch.setattr("matele.telegram.api._sleep", no_sleep)
	monkeypatch.setattr("matele.max.api._sleep", no_sleep)


@pytest.mark.parametrize(
	("code", "description"),
	[(401, "Unauthorized"), (404, "Not Found")],  # a wrong token, a malformed one
)
async def test_telegram_refusing_the_token_stops_polling(code, description):
	tg = FakeTelegram()
	tg.fail("getMe", code, description)
	dp = Dispatcher(registry=CallbackRegistry())
	with pytest.raises(InvalidTokenError) as raised:
		await asyncio.wait_for(dp.start_polling(TelegramBot(TOKEN, api=tg.api()), timeout=1), 2)
	assert raised.value.status_code == code and raised.value.platform == "telegram"
	assert "refused the bot token" in str(raised.value) and TOKEN not in str(raised.value)
	assert tg.methods() == ["getMe"]  # asked once, not retried


async def test_max_refusing_the_token_stops_polling():
	server = FakeMaxServer()
	server.on("GET", "/me", {"code": "verify.token", "message": "Invalid access_token"}, status=401)
	dp = Dispatcher(registry=CallbackRegistry())
	with pytest.raises(InvalidTokenError) as raised:
		await asyncio.wait_for(dp.start_polling(MaxBot("t", api=server.api()), timeout=1), 2)
	assert raised.value.platform == "max" and "Invalid access_token" in str(raised.value)


async def test_an_outage_at_startup_is_still_retried():
	tg = FakeTelegram()
	tg.fail("getMe", 502, "Bad Gateway")
	tg.fail("getMe", 502, "Bad Gateway")
	tg.fail("getMe", 502, "Bad Gateway")
	tg.fail("getMe", 502, "Bad Gateway")  # the client's own retries give up: polling retries
	tg.ok("getMe", {"id": 5, "is_bot": True, "first_name": "B", "username": "b_bot"})
	bot = TelegramBot(TOKEN, api=tg.api())
	task = asyncio.create_task(Dispatcher(registry=CallbackRegistry()).start_polling(bot, timeout=1))
	for _ in range(200):
		await asyncio.sleep(0)
		if bot.me is not None:
			break
	task.cancel()
	await asyncio.gather(task, return_exceptions=True)
	assert bot.me is not None and bot.me.username == "b_bot"


async def test_a_refused_token_stops_the_other_messenger_too_after_its_handlers_finish():
	tg = FakeTelegram()
	tg.fail("getMe", 401, "Unauthorized")
	fake = FakeBot()
	dp = Dispatcher(registry=CallbackRegistry())
	started, finished = asyncio.Event(), []

	@dp.message()
	async def slow(ctx: Context):
		started.set()
		await asyncio.sleep(0.05)
		finished.append(ctx.text)

	# The working messenger's event is mid-handling when the other one fails.
	fake.push(fake.message("order"))
	bot = TelegramBot(TOKEN, api=tg.api())
	real_get_me = bot.get_me

	async def late_get_me():
		await started.wait()
		return await real_get_me()

	bot.get_me = late_get_me  # type: ignore[method-assign]
	with pytest.raises(InvalidTokenError):
		await asyncio.wait_for(dp.start_polling(fake, bot, timeout=1), 2)
	assert finished == ["order"]  # drained, not cut off
