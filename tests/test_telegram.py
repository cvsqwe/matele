from __future__ import annotations

import io
import json
import logging
from typing import Any

import httpx
import pytest

from matele import (
	TELEGRAM,
	BotApiError,
	BotForbiddenError,
	BotUnavailableError,
	Button,
	CallbackQuery,
	ChatStatus,
	Keyboard,
	Message,
)
from matele.telegram import CallbackAliases, InputFile, TelegramApi, TelegramBot

TOKEN = "123:SECRET"


class FakeTelegram:
	"""Scripted Bot API: method -> queued answers (dict = result, or full JSON / exception)."""

	def __init__(self) -> None:
		self.calls: list[tuple[str, Any, httpx.Request]] = []
		self.answers: dict[str, list[Any]] = {}

	def ok(self, method: str, *results: Any) -> None:
		self.answers.setdefault(method, []).extend({"ok": True, "result": r} for r in results)

	def fail(self, method: str, code: int, description: str, **extra: Any) -> None:
		self.answers.setdefault(method, []).append(
			{"ok": False, "error_code": code, "description": description, **extra}
		)

	def raise_(self, method: str, exc: Exception) -> None:
		self.answers.setdefault(method, []).append(exc)

	def handle(self, request: httpx.Request) -> httpx.Response:
		path = request.url.path
		if "/file/" in path:
			return httpx.Response(200, content=b"FILE:" + path.rsplit("/", 1)[-1].encode())
		method = path.rsplit("/", 1)[-1]
		content_type = request.headers.get("content-type", "")
		body: Any = json.loads(request.content) if content_type.startswith("application/json") else request.read()
		self.calls.append((method, body, request))
		queue = self.answers.get(method) or []
		if not queue:
			return httpx.Response(200, json={"ok": True, "result": True})
		answer = queue.pop(0)
		if isinstance(answer, Exception):
			raise answer
		return httpx.Response(200 if answer["ok"] else answer["error_code"], json=answer)

	def api(self) -> TelegramApi:
		return TelegramApi(
			TOKEN, api_url="https://tg.test", http=httpx.AsyncClient(transport=httpx.MockTransport(self.handle))
		)

	def methods(self) -> list[str]:
		return [c[0] for c in self.calls]


@pytest.fixture
def tg() -> FakeTelegram:
	return FakeTelegram()


@pytest.fixture
def bot(tg) -> TelegramBot:
	return TelegramBot(TOKEN, api=tg.api())


@pytest.fixture
def sleeps(monkeypatch) -> list[float]:
	recorded: list[float] = []

	async def fake_sleep(delay: float) -> None:
		recorded.append(delay)

	monkeypatch.setattr("matele.telegram.api._sleep", fake_sleep)
	return recorded


def tg_message(text: str | None = "hi", *, chat_type="private", message_id=10, chat_id=7, **extra) -> dict[str, Any]:
	return {
		"message_id": message_id,
		"chat": {"id": chat_id, "type": chat_type},
		"from": {"id": chat_id, "first_name": "Ann", "last_name": "Lee", "username": "ann"},
		"text": text,
		**extra,
	}


# ---------------------------------------------------------------- TelegramApi
async def test_call_drops_none_and_returns_result(tg):
	tg.ok("sendMessage", {"message_id": 1})
	result = await tg.api().call("sendMessage", chat_id=1, text="x", reply_markup=None)
	assert result == {"message_id": 1}
	assert tg.calls[0][1] == {"chat_id": 1, "text": "x"}


async def test_flood_control_and_bounded_5xx_retries(tg, sleeps):
	tg.fail("sendMessage", 429, "Too Many Requests: retry after 5", parameters={"retry_after": 5})
	tg.ok("sendMessage", {"message_id": 1})
	await tg.api().call("sendMessage", chat_id=1, text="x")
	for _ in range(4):
		tg.fail("getChat", 502, "Bad Gateway")
	with pytest.raises(BotApiError) as info:
		await tg.api().call("getChat", chat_id=1)
	assert info.value.status_code == 502
	assert sleeps == [5.0, 1.0, 2.0, 4.0]


async def test_forbidden_and_token_never_in_errors(tg):
	tg.fail("sendMessage", 403, "Forbidden: bot was blocked by the user")
	api = tg.api()
	with pytest.raises(BotForbiddenError):
		await api.call("sendMessage", chat_id=1, text="x")
	tg.raise_("sendMessage", httpx.ConnectError(f"cannot reach https://tg.test/bot{TOKEN}/sendMessage"))
	with pytest.raises(BotUnavailableError) as info:
		await api.call("sendMessage", chat_id=1, text="x")
	assert "SECRET" not in str(info.value)
	tg.raise_("sendMessage", httpx.ConnectError("still down"))
	with pytest.raises(BotUnavailableError):
		await api.call("sendMessage", chat_id=1, text="x")
	calls = len(tg.calls)
	with pytest.raises(BotUnavailableError):  # guard: two failures in a row, fails fast
		await api.call("sendMessage", chat_id=1, text="x")
	assert len(tg.calls) == calls
	await api.call("getMe")  # probes bypass the guard and reopen it
	await api.call("sendMessage", chat_id=1, text="x")


async def test_files_go_multipart_and_are_rewound_on_retry(tg, sleeps, tmp_path):
	tg.fail("sendDocument", 429, "flood", parameters={"retry_after": 1})
	tg.ok("sendDocument", tg_message())
	await tg.api().call("sendDocument", chat_id=5, document=InputFile(io.BytesIO(b"payload"), "r.csv"), caption="c")
	for method, body, request in tg.calls:
		assert method == "sendDocument"
		assert request.headers["content-type"].startswith("multipart/form-data")
		assert b"payload" in body and b'filename="r.csv"' in body and b'name="chat_id"' in body
	path = tmp_path / "photo.jpg"
	path.write_bytes(b"JPEG")
	await tg.api().call("sendPhoto", chat_id=5, photo=InputFile(path))
	assert b'filename="photo.jpg"' in tg.calls[-1][1] and b"image/jpeg" in tg.calls[-1][1]


# ---------------------------------------------------------------- TelegramBot: incoming
async def test_parse_updates(bot):
	(message,) = await bot.parse_update({"update_id": 1, "message": tg_message("hello")})
	assert isinstance(message, Message)
	assert (
		message.message_id == "7:10" and message.from_user.name == "Ann Lee" and message.from_user.platform == TELEGRAM
	)
	(photo,) = await bot.parse_update(
		{
			"update_id": 2,
			"message": tg_message(None, caption="look", photo=[{"file_id": "small"}, {"file_id": "big"}]),
		}
	)
	assert isinstance(photo, Message) and photo.text == "look" and photo.attachments[0].token == "big"
	assert await bot.parse_update({"update_id": 3, "message": tg_message(chat_type="channel")}) == []
	(blocked,) = await bot.parse_update(
		{
			"update_id": 4,
			"my_chat_member": {
				"chat": {"id": 7, "type": "private"},
				"from": {"id": 7, "first_name": "Ann"},
				"new_chat_member": {"status": "kicked"},
			},
		}
	)
	assert isinstance(blocked, ChatStatus) and not blocked.reachable


async def test_long_callback_data_round_trips_through_an_alias(bot, tg):
	long_data = "report:" + "x" * 80
	tg.ok("sendMessage", tg_message("menu", message_id=11))
	await bot.send_message("menu", user_id=7, keyboard=Keyboard([[Button("Go", long_data)]]))
	sent_data = tg.calls[0][1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
	assert sent_data.startswith("~") and len(sent_data.encode()) <= 64
	(pressed,) = await bot.parse_update(
		{
			"update_id": 5,
			"callback_query": {
				"id": "cb",
				"from": {"id": 7, "first_name": "A"},
				"data": sent_data,
				"message": tg_message(),
			},
		}
	)
	assert isinstance(pressed, CallbackQuery) and pressed.data == long_data
	(unknown,) = await TelegramBot(TOKEN, api=tg.api()).parse_update(
		{"update_id": 6, "callback_query": {"id": "cb", "from": {"id": 7, "first_name": "A"}, "data": sent_data}}
	)
	assert isinstance(unknown, CallbackQuery) and unknown.data == ""  # a restart without a store forgets it


async def test_aliases_survive_a_restart_with_a_store():
	class Store:
		def __init__(self) -> None:
			self.rows: dict[str, str] = {}

		async def save(self, alias: str, data: str) -> None:
			self.rows[alias] = data

		async def get(self, alias: str) -> str | None:
			return self.rows.get(alias)

		async def purge(self, older_than_days: int) -> int:
			return 0

	store = Store()
	alias = CallbackAliases(store).encode("y" * 100)
	first = CallbackAliases(store)
	first.encode("y" * 100)
	await first.flush()
	assert await CallbackAliases(store).decode(alias) == "y" * 100


async def test_get_events_clears_the_webhook_once_and_advances_the_offset(bot, tg):
	tg.ok("getUpdates", [{"update_id": 40, "message": tg_message("a")}, {"update_id": 41, "message": tg_message("b")}])
	tg.ok("getUpdates", [])
	events = await bot.get_events(timeout=5)
	await bot.get_events(timeout=5)
	assert [e.text for e in events if isinstance(e, Message)] == ["a", "b"]
	assert tg.methods() == ["getWebhookInfo", "getUpdates", "getUpdates"]  # checked once, nothing to remove
	assert tg.calls[2][1]["offset"] == 42


async def test_polling_does_not_remove_a_webhook_on_its_own(bot, tg):
	tg.ok("getWebhookInfo", {"url": "https://prod.example.com/tg"}, {"url": ""})
	tg.ok("getUpdates", [])
	with pytest.raises(BotApiError, match="replace_webhook") as caught:
		await bot.get_events(timeout=5)
	assert caught.value.status_code == 409
	assert "deleteWebhook" not in tg.methods() and "getUpdates" not in tg.methods()
	await bot.get_events(timeout=5)  # the webhook is gone: polling starts
	assert tg.methods()[-1] == "getUpdates"


async def test_polling_removes_the_webhook_when_asked_to(tg):
	bot = TelegramBot(TOKEN, api=tg.api(), replace_webhook=True)
	tg.ok("getWebhookInfo", {"url": "https://old.example.com/tg"})
	tg.ok("getUpdates", [])
	await bot.get_events(timeout=5)
	assert tg.methods() == ["getWebhookInfo", "deleteWebhook", "getUpdates"]


# ---------------------------------------------------------------- TelegramBot: outgoing
async def test_bad_html_is_resent_as_plain_text(bot, tg):
	tg.fail("sendMessage", 400, "Bad Request: can't parse entities: unclosed tag")
	tg.ok("sendMessage", tg_message("a < b"))
	await bot.send_message("a < b", user_id=7)
	assert tg.calls[0][1]["parse_mode"] == "HTML" and "parse_mode" not in tg.calls[1][1]


async def test_edit_ignores_not_modified_and_strips_keyboard(bot, tg):
	tg.fail("editMessageText", 400, "Bad Request: message is not modified")
	await bot.edit_message("7:10", "same")
	await bot.edit_message("7:10", None)
	assert tg.calls[1][0] == "editMessageReplyMarkup"
	assert tg.calls[1][1] == {"chat_id": 7, "message_id": 10, "reply_markup": {"inline_keyboard": []}}
	with pytest.raises(BotApiError):
		await bot.edit_message("not-an-id", "x")


async def test_link_buttons_that_telegram_rejects_become_inert(bot, tg):
	tg.ok("sendMessage", tg_message())
	keyboard = Keyboard([[Button("Web", url="https://example.com"), Button("Local", url="localhost:3000")]])
	await bot.send_message("x", user_id=7, keyboard=keyboard)
	row = tg.calls[0][1]["reply_markup"]["inline_keyboard"][0]
	assert row == [{"text": "Web", "url": "https://example.com"}, {"text": "Local", "callback_data": "noop"}]


async def test_answer_callback_alert_and_stale(bot, tg):
	tg.fail("answerCallbackQuery", 400, "Bad Request: query is too old and response timeout expired")
	await bot.answer_callback("cb1", text="hi", alert=True)  # swallowed
	assert tg.calls[0][1] == {"callback_query_id": "cb1", "text": "hi", "show_alert": True}


async def test_long_caption_goes_separately(bot, tg):
	tg.ok("sendDocument", tg_message(None, message_id=20))
	tg.ok("sendMessage", tg_message("long", message_id=21))
	await bot.send_document(b"data", "r.csv", user_id=7, caption="c" * 2000)
	assert tg.methods() == ["sendDocument", "sendMessage"]
	assert b'name="caption"' not in tg.calls[0][1]


async def test_download_by_file_id(bot, tg):
	tg.ok("getFile", {"file_path": "documents/file_3.pdf"})
	content, name = await bot.download_file("FID", file_type="file", filename="")
	assert content == b"FILE:file_3.pdf" and name == "file_3.pdf"


async def test_download_refuses_a_file_over_the_limit(tg):
	api = TelegramApi(
		TOKEN,
		api_url="https://tg.test",
		http=httpx.AsyncClient(transport=httpx.MockTransport(tg.handle)),
		max_download=5,
	)
	with pytest.raises(BotApiError) as caught:
		await api.download("docs/report.pdf")  # the fake serves b"FILE:report.pdf"
	assert caught.value.status_code == 413
	assert await api.download("docs/report.pdf", max_size=100) == b"FILE:report.pdf"


@pytest.mark.parametrize("name", ["httpx", "httpcore.http11", "httpcore.connection", "httpcore.proxy"])
def test_the_token_is_hidden_in_http_client_logs(tg, caplog, name):
	tg.api()  # creating a client registers its token for redaction
	with caplog.at_level(logging.DEBUG, logger=name):
		logging.getLogger(name).debug("POST https://tg.test/bot%s/getMe", TOKEN)
	assert TOKEN not in caplog.text and "bot***/getMe" in caplog.text


async def test_the_alias_cache_is_bounded_and_refilled_from_the_store():
	class Store:
		def __init__(self) -> None:
			self.rows: dict[str, str] = {}

		async def save(self, alias: str, data: str) -> None:
			self.rows[alias] = data

		async def get(self, alias: str) -> str | None:
			return self.rows.get(alias)

		async def purge(self, older_than_days: int) -> int:
			return 0

	aliases = CallbackAliases(Store(), cache_size=2)
	first, _, third = (aliases.encode(letter * 100) for letter in "abc")
	await aliases.flush()
	assert len(aliases._by_alias) == 2 and len(aliases._saved_at) <= 2
	assert await aliases.decode(first) == "a" * 100  # pushed out of memory, found in the store
	assert await aliases.decode(third) == "c" * 100

	memory_only = CallbackAliases(cache_size=2)
	gone = memory_only.encode("a" * 100)
	memory_only.encode("b" * 100)
	memory_only.encode("c" * 100)
	assert await memory_only.decode(gone) is None


async def test_flood_control_pauses_the_other_calls_too(tg, sleeps):
	for _ in range(3):  # more 429s than one call retries
		tg.fail("sendMessage", 429, "Too Many Requests: retry after 7", parameters={"retry_after": 7})
	api = tg.api()
	with pytest.raises(BotApiError):
		await api.call("sendMessage", chat_id=1, text="first")
	assert sleeps == [7.0, 7.0]
	await api.call("sendMessage", chat_id=2, text="second")  # waits out the pause before it is sent
	assert len(sleeps) == 3 and 6 < sleeps[2] <= 7
	await api.call("getUpdates")  # long polling doesn't wait
	assert len(sleeps) == 3


async def test_with_a_secret_only_buttons_the_bot_made_are_accepted(tg):
	bot = TelegramBot(TOKEN, api=tg.api(), callback_secret="s3cret")
	tg.ok("sendMessage", tg_message("menu"))
	await bot.send_message("menu", user_id=7, keyboard=Keyboard([[Button("Order", "order:view:42")]]))
	sent = tg.calls[-1][1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
	assert sent.startswith("order:view:42#")

	def press(data: str) -> dict[str, Any]:
		return {"callback_query": {"id": "1", "data": data, "from": {"id": 7}, "message": tg_message("menu")}}

	(real,) = await bot.parse_update(press(sent))
	assert isinstance(real, CallbackQuery) and real.data == "order:view:42"
	(forged,) = await bot.parse_update(press("order:view:43"))
	assert isinstance(forged, CallbackQuery) and forged.data == ""  # like an outdated button
