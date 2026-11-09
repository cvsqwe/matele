"""Regression tests for problems found in a review of the code."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import IO, Any, cast
from urllib.parse import quote, urlencode

import pytest

from matele import Button, Context, Dispatcher, Keyboard, Message
from matele.callbacks import CallbackRegistry
from matele.fsm import MemoryStorage, StateRecord, StorageKey
from matele.max import MaxBot
from matele.telegram import TelegramBot
from matele.testing import FakeBot
from matele.webapp import verify_init_data
from matele.webhook import Webhook
from tests.max_fake import FakeMaxServer, message
from tests.test_telegram import TOKEN, FakeTelegram, tg_message
from tests.test_webapp import NOW, sign


class BrokenStorage(MemoryStorage):
	def __init__(self) -> None:
		super().__init__()
		self.writes = 0

	async def get(self, key: StorageKey) -> StateRecord | None:
		raise ConnectionError("redis is down")

	async def set(self, key: StorageKey, record: StateRecord) -> None:
		self.writes += 1


async def test_a_storage_failure_reaches_on_error_and_the_button_is_answered():
	storage = BrokenStorage()
	dp = Dispatcher(storage=storage, registry=CallbackRegistry())
	errors: list[Exception] = []
	called = []

	@dp.raw_callback()
	async def pressed(ctx: Context):
		called.append(1)

	@dp.on_error
	async def on_error(ctx: Context, exc: Exception):
		errors.append(exc)

	bot = FakeBot()
	assert await dp.feed(bot.press("x")) is False  # no exception escapes
	assert called == []  # the handler doesn't run without the person's state
	assert isinstance(errors[0], ConnectionError)
	assert len(bot.answers) == 1  # the button stops spinning
	assert storage.writes == 0  # nothing saved over the real state


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


async def test_typing_errors_never_break_the_handler():
	dp = Dispatcher(registry=CallbackRegistry())

	class NoTyping(FakeBot):
		calls = 0

		async def send_typing(self, chat_id: int) -> None:
			self.calls += 1
			raise RuntimeError("not supported")

	@dp.message()
	async def slow(ctx: Context):
		async with ctx.typing(every=0.01):
			await asyncio.sleep(0.05)
		await ctx.reply("done")

	bot = NoTyping()
	await dp.feed(bot.message("x"))
	assert bot.texts == ["done"]
	assert bot.calls >= 2  # the "typing…" loop survives an error and keeps trying


async def test_the_telegram_token_never_reaches_the_logs(caplog):
	caplog.set_level(logging.INFO, logger="httpx")
	tg = FakeTelegram()
	tg.ok("getMe", {"id": 1, "first_name": "Bot", "username": "b"})
	await TelegramBot(TOKEN, api=tg.api()).get_me()
	assert "HTTP Request" in caplog.text  # httpx did log the request
	assert TOKEN not in caplog.text and "SECRET" not in caplog.text
	assert "/bot***/getMe" in caplog.text


async def test_webhook_mounted_under_a_prefix():
	dp = Dispatcher(registry=CallbackRegistry())
	seen = []

	@dp.message()
	async def h(ctx: Context):
		seen.append(ctx.text)

	webhook = Webhook(dp, secret="mount-secret")
	webhook.add(MaxBot("t", api=FakeMaxServer().api()), "/max")
	body = json.dumps({"update_type": "message_created", "timestamp": 1, "message": message("hi")}).encode()
	sent: list[dict[str, Any]] = []

	async def receive() -> dict[str, Any]:
		return {"type": "http.request", "body": body, "more_body": False}

	async def send(msg: dict[str, Any]) -> None:
		sent.append(msg)

	scope = {
		"type": "http",
		"method": "POST",
		"path": "/hooks/max",
		"root_path": "/hooks",
		"headers": [(b"x-max-bot-api-secret", b"mount-secret")],
	}
	await webhook(scope, receive, send)
	await asyncio.wait(set(webhook._running), timeout=2)
	assert sent[0]["status"] == 200 and seen == ["hi"]


async def test_files_from_bytes_get_a_name_that_tells_the_type():
	tg = FakeTelegram()
	tg.ok("sendPhoto", tg_message(None))
	await TelegramBot(TOKEN, api=tg.api()).send_file(b"JPEG", kind="image", user_id=7)
	assert b'filename="image.jpg"' in tg.calls[0][1] and b"image/jpeg" in tg.calls[0][1]

	server = FakeMaxServer()
	server.on("POST", "/uploads", {"url": "https://up/1"})
	server.on("POST", "https://up/1", {"token": "vt"})
	server.on("POST", "/messages", {"message": message(None)})
	await MaxBot("t", api=server.api()).send_file(b"MP4", kind="video", user_id=7)
	assert b'filename="video.mp4"' in server.files[0].content


async def test_max_limits_are_explained():
	server = FakeMaxServer()
	bot = MaxBot("t", api=server.api())
	with pytest.raises(ValueError, match="1024"):
		await bot.send_message("x", user_id=7, keyboard=Keyboard([[Button("Big", "x" * 1025)]]))
	assert server.calls == []  # refused before calling MAX

	server.on("PATCH", "/me/commands", {"commands": []})
	await bot.set_commands([(f"c{i}", "d") for i in range(40)])
	assert len(server.calls[0].json["commands"]) == 32

	dp = Dispatcher(registry=CallbackRegistry())
	with pytest.raises(ValueError, match="1-128"):
		dp.command("x", description="d" * 129)


def test_init_data_hashed_without_the_signature_field_is_accepted():
	fields = {"user": json.dumps({"id": 42, "first_name": "A"}), "auth_date": str(NOW - 10)}
	data = urlencode({**fields, "signature": "sig", "hash": sign(fields, TOKEN)}, quote_via=quote)
	assert verify_init_data(data, TOKEN, platform="telegram", now=NOW).user is not None
	forged = urlencode({**fields, "signature": "sig", "hash": sign(fields, "999:OTHER")}, quote_via=quote)
	with pytest.raises(ValueError):
		verify_init_data(forged, TOKEN, platform="telegram", now=NOW)


async def test_telegram_service_messages_are_not_routed():
	bot = TelegramBot(TOKEN, api=FakeTelegram().api())
	for service in (
		{"pinned_message": {"message_id": 1}},
		{"new_chat_title": "New title"},
		{"migrate_to_chat_id": -1001},
	):
		raw = tg_message(None, chat_type="group", chat_id=-5)
		del raw["text"]
		assert await bot.parse_update({"update_id": 1, "message": {**raw, **service}}) == []
	sticker = tg_message(None)
	del sticker["text"]
	(event,) = await bot.parse_update({"update_id": 2, "message": {**sticker, "sticker": {"file_id": "s"}}})
	assert isinstance(event, Message) and event.text is None  # real content without text is still routed


async def test_a_consumed_stream_is_not_resent_empty():
	import io

	class OneShot(io.RawIOBase):
		def __init__(self) -> None:
			self.data = b"payload"

		def readable(self) -> bool:
			return True

		def seekable(self) -> bool:
			return False

		def readinto(self, buffer) -> int:
			n = min(len(buffer), len(self.data))
			buffer[:n], self.data = self.data[:n], self.data[n:]
			return n

	tg = FakeTelegram()
	tg.fail("sendDocument", 429, "Too Many Requests", parameters={"retry_after": 1})
	tg.ok("sendDocument", tg_message(None))
	from matele.telegram import InputFile

	with pytest.raises(Exception, match="Too Many Requests"):
		await tg.api().call("sendDocument", chat_id=1, document=InputFile(cast(IO[bytes], OneShot()), "x.bin"))
	assert len(tg.calls) == 1  # not retried with an empty body


async def test_max_polling_warns_about_a_subscribed_webhook(caplog):
	server = FakeMaxServer()
	server.on("GET", "/subscriptions", {"subscriptions": [{"url": "https://old.example.com/max", "time": 1}]})
	server.always("GET", "/updates", {"updates": [], "marker": None})
	bot = MaxBot("t", api=server.api())
	await bot.get_events(timeout=1)
	await bot.get_events(timeout=1)
	assert "https://old.example.com/max" in caplog.text
	assert [c.path for c in server.calls].count("/subscriptions") == 1  # checked once
