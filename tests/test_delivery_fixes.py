"""Sends are never repeated when they may have gone through; one-way file
streams survive a resend; MAX edits and button answers."""

from __future__ import annotations

import asyncio
import io
import logging

import httpx
import pytest

from matele import Button, Keyboard
from matele.errors import BotApiError, BotUnavailableError
from matele.hub import Hub
from matele.max import MaxBot
from matele.max.bot import move_unlinkable_buttons
from matele.telegram import TelegramBot
from tests.max_fake import FakeMaxServer, message
from tests.test_hub import FlakyBot
from tests.test_telegram import TOKEN, FakeTelegram, tg_message


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
	async def instant(delay: float) -> None:
		await asyncio.sleep(0)

	monkeypatch.setattr("matele.telegram.api._sleep", instant)
	monkeypatch.setattr("matele.max.api._sleep", instant)
	monkeypatch.setattr("matele.hub._sleep", instant)


# ------------------------------------------------------------ Telegram
@pytest.mark.parametrize("status", [502, 504])
async def test_telegram_does_not_repeat_a_send_that_may_have_gone_through(status):
	tg = FakeTelegram()
	tg.fail("sendMessage", status, "Bad Gateway")
	with pytest.raises(BotApiError) as raised:
		await TelegramBot(TOKEN, api=tg.api()).send_message("paid", user_id=1)
	assert raised.value.status_code == status and tg.methods() == ["sendMessage"]


async def test_telegram_repeats_a_send_that_was_turned_away():
	tg = FakeTelegram()
	tg.fail("sendMessage", 503, "Service Unavailable")
	tg.ok("sendMessage", tg_message("paid"))
	await TelegramBot(TOKEN, api=tg.api()).send_message("paid", user_id=1)
	assert tg.methods() == ["sendMessage", "sendMessage"]


async def test_telegram_still_repeats_calls_that_send_nothing():
	tg = FakeTelegram()
	tg.fail("editMessageText", 502, "Bad Gateway")
	await TelegramBot(TOKEN, api=tg.api()).edit_message("1:2", "new")
	assert tg.methods() == ["editMessageText", "editMessageText"]


@pytest.mark.parametrize(
	("exc", "maybe_sent"),
	[
		(httpx.ReadTimeout("slow"), True),
		(httpx.RemoteProtocolError("dropped"), True),
		(httpx.ConnectError("no"), False),
	],
)
async def test_telegram_network_errors_tell_whether_the_request_may_have_arrived(exc, maybe_sent):
	tg = FakeTelegram()
	tg.raise_("sendMessage", exc)
	with pytest.raises(BotUnavailableError) as raised:
		await TelegramBot(TOKEN, api=tg.api()).send_message("paid", user_id=1)
	assert raised.value.maybe_sent is maybe_sent


class OneWay(io.RawIOBase):
	"""A stream that can be read once, like a socket or a pipe."""

	def __init__(self, data: bytes) -> None:
		self._data = io.BytesIO(data)

	def readable(self) -> bool:
		return True

	def seekable(self) -> bool:
		return False

	def readinto(self, buffer) -> int:  # type: ignore[override]
		chunk = self._data.read(len(buffer))
		buffer[: len(chunk)] = chunk
		return len(chunk)


async def test_telegram_resends_a_one_way_stream_whole_when_the_caption_is_not_html():
	tg = FakeTelegram()
	tg.fail("sendDocument", 400, "Bad Request: can't parse entities: unclosed tag")
	tg.ok("sendDocument", tg_message(None))
	bot = TelegramBot(TOKEN, api=tg.api())
	content = b"report " * 50_000  # over the in-memory part: goes through the disk
	await bot.send_file(io.BufferedReader(OneWay(content)), filename="r.txt", user_id=1, caption="<b>broken")
	assert tg.methods() == ["sendDocument", "sendDocument"]
	for _, body, _ in tg.calls:  # both uploads carry the whole file
		assert body.count(b"report ") == 50_000


# ------------------------------------------------------------ MAX
@pytest.mark.parametrize("status", [502, 504])
async def test_max_does_not_repeat_a_send_that_may_have_gone_through(status):
	server = FakeMaxServer()
	server.on("POST", "/messages", {"code": "gateway", "message": "Bad Gateway"}, status=status)
	with pytest.raises(BotApiError):
		await MaxBot("t", api=server.api()).send_message("paid", user_id=1)
	assert [c.path for c in server.calls] == ["/messages"]


async def test_max_repeats_a_send_that_was_turned_away():
	server = FakeMaxServer()
	server.on("POST", "/messages", {"code": "busy", "message": "Service Unavailable"}, status=503)
	server.on("POST", "/messages", {"message": message("paid")})
	await MaxBot("t", api=server.api()).send_message("paid", user_id=1)
	assert [c.path for c in server.calls] == ["/messages", "/messages"]


async def test_max_answers_a_press_without_text_with_an_empty_answer():
	server = FakeMaxServer()
	server.on("POST", "/answers", {"success": True})
	await MaxBot("t", api=server.api()).answer_callback("cb1")
	assert server.calls[0].json == {}


async def test_max_falls_back_to_an_empty_notification_if_an_empty_answer_is_refused():
	server = FakeMaxServer()
	server.on("POST", "/answers", {"code": "proto.payload", "message": "empty answer"}, status=400)
	server.on("POST", "/answers", {"success": True})
	await MaxBot("t", api=server.api()).answer_callback("cb1")
	assert [c.json for c in server.calls] == [{}, {"notification": ""}]


async def test_max_edit_without_text_keeps_the_text_and_makes_bad_links_inert(caplog):
	server = FakeMaxServer()
	server.on("PUT", "/messages", {"success": True})
	keyboard = Keyboard([[Button("Local", url="http://localhost:3000"), Button("Site", url="https://example.com")]])
	with caplog.at_level(logging.WARNING, logger="matele.max.bot"):
		await MaxBot("t", api=server.api()).edit_message("mid.1", None, keyboard=keyboard)
	body = server.calls[0].json
	assert "text" not in body  # the message keeps its text
	buttons = body["attachments"][0]["payload"]["buttons"][0]
	assert buttons[0] == {"type": "callback", "text": "Local", "payload": "noop"}
	assert buttons[1]["type"] == "link" and buttons[1]["url"] == "https://example.com"
	assert "localhost:3000" in caplog.text


def test_moved_links_without_a_text_do_not_start_with_blank_lines():
	text, _ = move_unlinkable_buttons(None, Keyboard([[Button("Local", url="http://localhost:3000")]]))
	assert text == "Local: http://localhost:3000"


# ------------------------------------------------------------ Hub
async def test_hub_does_not_resend_what_may_have_arrived():
	class SlowAnswer(FlakyBot):
		async def send_message(self, text, *, chat_id=None, user_id=None, keyboard=None, reply_to=None):
			self.attempts = getattr(self, "attempts", 0) + 1
			raise BotUnavailableError(0, "network", "ReadTimeout", platform=self.platform, maybe_sent=True)

	bot = SlowAnswer(platform="max")
	[delivery] = await Hub(bot).send({"max": 1}, "paid")
	assert not delivery.ok and bot.attempts == 1
