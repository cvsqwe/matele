from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from matele import Context, Dispatcher
from matele.callbacks import CallbackRegistry
from matele.max import MaxBot
from matele.telegram import TelegramBot
from matele.webhook import Webhook
from tests.max_fake import FakeMaxServer, message, user
from tests.test_telegram import TOKEN, FakeTelegram, tg_message

SECRET = "s3cret-value_1"


@pytest.fixture
def server() -> FakeMaxServer:
	return FakeMaxServer()


@pytest.fixture
def tg() -> FakeTelegram:
	return FakeTelegram()


@pytest.fixture
def setup(server, tg):
	dp = Dispatcher(registry=CallbackRegistry())
	seen: list[str] = []

	@dp.message()
	async def echo(ctx: Context):
		seen.append(f"{ctx.platform}:{ctx.text}")

	webhook = Webhook(dp, base_url="https://bot.example.com/", secret=SECRET)
	max_bot = MaxBot("t", api=server.api())
	tg_bot = TelegramBot(TOKEN, api=tg.api())
	webhook.add(max_bot, "/max")
	webhook.add(tg_bot, "/tg")
	return webhook, seen, max_bot, tg_bot


async def settle(webhook: Webhook) -> None:
	for _ in range(20):
		await asyncio.sleep(0)
	if webhook._running:
		await asyncio.wait(set(webhook._running), timeout=2)


def max_update(text: str) -> bytes:
	return json.dumps({"update_type": "message_created", "timestamp": 1, "message": message(text)}).encode()


def tg_update(text: str) -> bytes:
	return json.dumps({"update_id": 1, "message": tg_message(text)}).encode()


async def test_requests_are_checked_and_dispatched(setup):
	webhook, seen, _, _ = setup
	assert await webhook.handle("/nope", {}, b"{}") == 404
	assert await webhook.handle("/max", {"X-Max-Bot-Api-Secret": "wrong"}, max_update("x")) == 403
	assert await webhook.handle("/max", {}, max_update("x")) == 403  # no secret at all
	assert await webhook.handle("/tg", {"X-Max-Bot-Api-Secret": SECRET}, tg_update("x")) == 403  # wrong header
	assert await webhook.handle("/max", {"x-max-bot-api-secret": SECRET}, b"not json") == 400
	assert await webhook.handle("/max", {"x-max-bot-api-secret": SECRET}, b"[1, 2]") == 400
	assert await webhook.handle("/max", {"X-Max-Bot-Api-Secret": SECRET}, max_update("from max")) == 200
	assert await webhook.handle("/tg", {"X-Telegram-Bot-Api-Secret-Token": SECRET}, tg_update("from tg")) == 200
	await settle(webhook)
	assert sorted(seen) == ["max:from max", "telegram:from tg"]


async def test_broken_updates_are_acknowledged_not_retried(setup):
	webhook, seen, _, _ = setup
	body = json.dumps({"update_type": "message_created", "timestamp": 1}).encode()  # no message
	assert await webhook.handle("/max", {"X-Max-Bot-Api-Secret": SECRET}, body) == 200
	await settle(webhook)
	assert seen == []


async def test_startup_registers_webhooks_and_shutdown_cleans_up(setup, server, tg):
	webhook, _, max_bot, tg_bot = setup
	webhook.delete_on_shutdown = True
	server.on("GET", "/me", user(1, "Bot", username="max_bot", is_bot=True))
	server.on("POST", "/subscriptions", {"success": True})
	server.on("DELETE", "/subscriptions", {"success": True})
	tg.ok("getMe", {"id": 2, "first_name": "Bot", "username": "tg_bot"})
	await webhook.startup()
	subscribe = next(c for c in server.calls if c.path == "/subscriptions")
	assert subscribe.json == {"url": "https://bot.example.com/max", "secret": SECRET}
	set_webhook = next(body for method, body, _ in tg.calls if method == "setWebhook")
	assert set_webhook["url"] == "https://bot.example.com/tg" and set_webhook["secret_token"] == SECRET
	assert max_bot.me is not None and max_bot.me.username == "max_bot"
	await webhook.shutdown()
	unsubscribe = [c for c in server.calls if c.method == "DELETE"]
	assert unsubscribe[0].params == {"url": "https://bot.example.com/max"}
	assert "deleteWebhook" in [method for method, _, _ in tg.calls]


def test_secret_validation_and_default():
	dp = Dispatcher(registry=CallbackRegistry())
	with pytest.raises(ValueError):
		Webhook(dp, secret="bad secret!")
	generated = Webhook(dp).secret
	assert len(generated) >= 32 and generated != Webhook(dp).secret


def test_routes_must_be_unique_paths(setup):
	webhook, _, max_bot, _ = setup
	with pytest.raises(ValueError):
		webhook.add(max_bot, "/max")
	with pytest.raises(ValueError):
		webhook.add(max_bot, "max2")


async def test_events_of_one_person_stay_in_order(setup):
	webhook, _, _, _ = setup
	order: list[str] = []
	release = asyncio.Event()
	dp = Dispatcher(registry=CallbackRegistry())

	@dp.message()
	async def slow_first(ctx: Context):
		if ctx.text == "1":
			await release.wait()
		order.append(ctx.text)

	webhook.dp = dp
	headers = {"X-Max-Bot-Api-Secret": SECRET}
	await webhook.handle("/max", headers, max_update("1"))
	await webhook.handle("/max", headers, max_update("2"))
	for _ in range(20):
		await asyncio.sleep(0)
	assert order == []
	release.set()
	await settle(webhook)
	assert order == ["1", "2"]


# ---------------------------------------------------------------- ASGI
async def call_asgi(app: Webhook, method: str, path: str, body: bytes, headers: dict[str, str]) -> int:
	chunks = [body[:5], body[5:]]
	sent: list[dict[str, Any]] = []

	async def receive() -> dict[str, Any]:
		chunk = chunks.pop(0)
		return {"type": "http.request", "body": chunk, "more_body": bool(chunks)}

	async def send(message: dict[str, Any]) -> None:
		sent.append(message)

	scope = {
		"type": "http",
		"method": method,
		"path": path,
		"headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
	}
	await app(scope, receive, send)
	return sent[0]["status"]


async def test_asgi_http(setup):
	webhook, seen, _, _ = setup
	headers = {"X-Max-Bot-Api-Secret": SECRET}
	assert await call_asgi(webhook, "POST", "/max", max_update("via asgi"), headers) == 200
	assert await call_asgi(webhook, "GET", "/max", b"", headers) == 405
	assert await call_asgi(webhook, "GET", "/other", b"", headers) == 404
	await settle(webhook)
	assert seen == ["max:via asgi"]


async def test_asgi_lifespan(setup, server, tg):
	webhook, _, _, _ = setup
	server.on("GET", "/me", user(1, "Bot", username="max_bot", is_bot=True))
	server.on("POST", "/subscriptions", {"success": True})
	tg.ok("getMe", {"id": 2, "first_name": "Bot", "username": "tg_bot"})
	inbox = [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]
	sent: list[str] = []

	async def receive() -> dict[str, Any]:
		return inbox.pop(0)

	async def send(message: dict[str, Any]) -> None:
		sent.append(message["type"])

	await webhook({"type": "lifespan"}, receive, send)
	assert sent == ["lifespan.startup.complete", "lifespan.shutdown.complete"]


async def test_asgi_lifespan_reports_startup_failure(setup, server):
	webhook, _, _, _ = setup
	server.on("GET", "/me", {"code": "verify.token", "message": "Invalid access_token"}, status=401)
	sent: list[dict[str, Any]] = []

	async def receive() -> dict[str, Any]:
		return {"type": "lifespan.startup"}

	async def send(message: dict[str, Any]) -> None:
		sent.append(message)

	await webhook({"type": "lifespan"}, receive, send)
	assert sent[0]["type"] == "lifespan.startup.failed" and "Invalid access_token" in sent[0]["message"]


async def test_a_repeated_telegram_update_is_handled_once():
	bot = TelegramBot(TOKEN, api=FakeTelegram().api())
	update = {"update_id": 77, "message": tg_message("pay 100")}
	assert len(await bot.parse_webhook(update)) == 1
	assert await bot.parse_webhook(update) == []  # Telegram's retry of the same update
	assert len(await bot.parse_webhook({"update_id": 78, "message": tg_message("next")})) == 1
