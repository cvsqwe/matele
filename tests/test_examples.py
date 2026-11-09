from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import httpx
import pytest

from matele.telegram import TelegramBot
from tests.test_telegram import TOKEN, FakeTelegram, tg_message
from tests.test_webapp import init_data

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


@pytest.fixture
def mini_app(monkeypatch):
	monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
	monkeypatch.delenv("MAX_BOT_TOKEN", raising=False)
	spec = importlib.util.spec_from_file_location("mini_app_example", EXAMPLES / "mini_app" / "app.py")
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	sys.modules["mini_app_example"] = module
	spec.loader.exec_module(module)
	yield module
	sys.modules.pop("mini_app_example", None)


async def test_mini_app_example_end_to_end(mini_app):
	tg = FakeTelegram()
	tg.ok("sendMessage", tg_message("ok"))
	mini_app.bots["telegram"] = TelegramBot(TOKEN, api=tg.api(), app_url="https://app.example.com/")
	transport = httpx.ASGITransport(app=mini_app.app)
	async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
		page = await client.get("/")
		assert page.status_code == 200 and "max-web-app.js" in page.text

		good = {"X-Platform": "telegram", "X-Init-Data": init_data()}
		ok = await client.post("/api/order", json={"item": "Кофе <3"}, headers=good)
		forged = await client.post(
			"/api/order", json={"item": "x"}, headers={**good, "X-Init-Data": init_data().replace("anya", "admin")}
		)
		unknown = await client.post("/api/order", json={"item": "x"}, headers={**good, "X-Platform": "icq"})

	assert ok.status_code == 200
	sent = tg.calls[0][1]
	assert sent["chat_id"] == 42 and sent["text"] == "Заказ принят: <b>Кофе &lt;3</b>"
	assert forged.status_code == 401 and unknown.status_code == 400
	assert len(tg.calls) == 1  # nothing was sent for the bad requests
