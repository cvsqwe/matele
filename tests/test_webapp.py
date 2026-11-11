from __future__ import annotations

import json
from urllib.parse import quote, urlencode

import pytest

from matele import MAX, TELEGRAM, Button, Keyboard
from matele.max import MaxBot
from matele.telegram import TelegramBot
from matele.types import User
from matele.webapp import InitDataError, verify_init_data
from tests.init_data import NOW, init_data, sign
from tests.max_fake import FakeMaxServer, message
from tests.telegram_fake import TOKEN, FakeTelegram, tg_message


def test_valid_init_data():
	data = verify_init_data(init_data(), TOKEN, platform=TELEGRAM, now=NOW)
	assert data.user == User(id=42, platform=TELEGRAM, name="Аня Ли", username="anya")
	assert data.start_param == "orders" and data.language == "ru" and data.query_id == "AAH"
	assert data.auth_date == NOW - 60 and data.fields["signature"].startswith("ed25519")


@pytest.mark.parametrize(
	("tamper", "reason"),
	[
		(lambda s: s.replace("anya", "admin"), "wrong signature"),  # someone edited the user
		(lambda s: s + "&extra=1", "wrong signature"),  # an added field
		(lambda s: s.split("&hash=")[0], "no hash"),
		(lambda s: s + "&user=x", "more than once"),
		(lambda s: "garbage", "no hash"),
	],
)
def test_tampered_init_data_is_rejected(tamper, reason):
	with pytest.raises(InitDataError, match=reason):
		verify_init_data(tamper(init_data()), TOKEN, platform=TELEGRAM, now=NOW)


def test_another_bots_token_does_not_match():
	with pytest.raises(InitDataError, match="wrong signature"):
		verify_init_data(init_data(token="999:OTHER"), TOKEN, platform=TELEGRAM, now=NOW)


def test_age_is_checked_unless_disabled():
	old = init_data(auth_date=str(NOW - 2 * 86400))
	with pytest.raises(InitDataError, match="too old"):
		verify_init_data(old, TOKEN, platform=TELEGRAM, now=NOW)
	assert verify_init_data(old, TOKEN, platform=TELEGRAM, now=NOW, max_age=None).user is not None
	assert verify_init_data(old, TOKEN, platform=TELEGRAM, now=NOW, max_age=3 * 86400).user is not None


def test_max_launch_fragment_is_accepted():
	fragment = "#" + urlencode({"WebAppData": init_data(), "WebAppPlatform": "ios", "WebAppVersion": "25.9"})
	data = verify_init_data(fragment, TOKEN, platform=MAX, now=NOW)
	assert data.user is not None and data.user.platform == MAX and data.start_param == "orders"


def test_bots_verify_with_their_own_token():
	tg = TelegramBot(TOKEN, api=FakeTelegram().api())
	assert tg.verify_init_data(init_data(), max_age=None).user is not None
	max_bot = MaxBot("max-token", api=FakeMaxServer().api())
	with pytest.raises(InitDataError):
		max_bot.verify_init_data(init_data(), max_age=None)  # signed with the Telegram token
	assert max_bot.verify_init_data(init_data(token="max-token"), max_age=None).user is not None


# ---------------------------------------------------------------- buttons
def test_start_param_is_validated():
	Button("Open", app="")
	Button("Open", app="orders-42_x")
	with pytest.raises(ValueError):
		Button("Open", app="has space")
	with pytest.raises(ValueError):
		Button("Open", app="x" * 513)


async def test_telegram_app_button():
	tg = FakeTelegram()
	tg.ok("sendMessage", tg_message("menu"), tg_message("menu"))
	bot = TelegramBot(TOKEN, api=tg.api(), app_url="https://app.example.com/index.html?v=2")
	keyboard = Keyboard([[Button("Заказы", app="orders"), Button("Главная", app="")]])
	await bot.send_message("menu", user_id=7, keyboard=keyboard)
	row = tg.calls[0][1]["reply_markup"]["inline_keyboard"][0]
	assert row == [
		{"text": "Заказы", "web_app": {"url": "https://app.example.com/index.html?v=2&startapp=orders"}},
		{"text": "Главная", "web_app": {"url": "https://app.example.com/index.html?v=2"}},
	]
	with pytest.raises(ValueError, match="app_url"):
		await TelegramBot(TOKEN, api=tg.api()).send_message("x", user_id=7, keyboard=keyboard)


async def test_max_app_button():
	server = FakeMaxServer()
	server.on("POST", "/messages", {"message": message("menu")})
	bot = MaxBot("t", api=server.api())
	keyboard = Keyboard([[Button("Заказы", app="orders")]])
	with pytest.raises(ValueError, match="get_me"):
		await bot.send_message("menu", user_id=7, keyboard=keyboard)
	bot.me = User(id=5, platform=MAX, username="shop_bot")
	await bot.send_message("menu", user_id=7, keyboard=keyboard)
	button = server.calls[0].json["attachments"][0]["payload"]["buttons"][0][0]
	assert button == {"type": "open_app", "text": "Заказы", "web_app": "shop_bot", "contact_id": 5, "payload": "orders"}


def test_a_non_ascii_hash_is_a_wrong_signature_not_a_crash():
	with pytest.raises(InitDataError, match="wrong signature"):
		verify_init_data("auth_date=1&hash=%D1%8B", "token", platform="max")


def test_init_data_hashed_without_the_signature_field_is_accepted():
	fields = {"user": json.dumps({"id": 42, "first_name": "A"}), "auth_date": str(NOW - 10)}
	data = urlencode({**fields, "signature": "sig", "hash": sign(fields, TOKEN)}, quote_via=quote)
	assert verify_init_data(data, TOKEN, platform="telegram", now=NOW).user is not None
	forged = urlencode({**fields, "signature": "sig", "hash": sign(fields, "999:OTHER")}, quote_via=quote)
	with pytest.raises(ValueError):
		verify_init_data(forged, TOKEN, platform="telegram", now=NOW)
