from __future__ import annotations

import io
from typing import Any

import httpx
import pytest

from matele import BotApiError, BotForbiddenError, BotUnavailableError, is_unreachable
from matele.max import MaxApi
from matele.max import models as m
from matele.max.api import ssl_context
from tests.max_fake import FakeMaxServer, message, user


@pytest.fixture
def server() -> FakeMaxServer:
	return FakeMaxServer()


@pytest.fixture
def sleeps(monkeypatch) -> list[float]:
	recorded: list[float] = []

	async def fake_sleep(delay: float) -> None:
		recorded.append(delay)

	monkeypatch.setattr("matele.max.api._sleep", fake_sleep)
	return recorded


def test_ssl_context_includes_the_russian_root():
	ctx = ssl_context()
	certs: list[Any] = ctx.get_ca_certs()
	subjects = [dict(x[0] for x in c["subject"]).get("commonName") for c in certs]
	assert "Russian Trusted Root CA" in subjects


def test_empty_token_is_refused():
	with pytest.raises(ValueError):
		MaxApi("")


async def test_token_header_and_typed_result(server):
	server.on("GET", "/me", {**user(1, "Bot", username="my_bot", is_bot=True), "commands": [{"name": "start"}]})
	api = server.api()
	me = await api.get_me()
	assert me.username == "my_bot" and me.commands == [m.BotCommand(name="start")]
	assert server.calls[0].headers["Authorization"] == "secret-token"


async def test_send_message_body_and_params(server):
	server.on("POST", "/messages", {"message": message("ok", mid="mid.9")})
	api = server.api()
	kb = m.InlineKeyboardAttachmentRequest(
		payload=m.InlineKeyboardAttachmentRequestPayload(buttons=[[m.CallbackButton(text="A", payload="a")]])
	)
	sent = await api.send_message(user_id=5, text="<b>hi</b>", format="html", attachments=[kb], notify=False)
	assert sent.body.mid == "mid.9"
	call = server.calls[0]
	assert call.params == {"user_id": "5"}
	assert call.json == {
		"text": "<b>hi</b>",
		"format": "html",
		"notify": False,
		"attachments": [
			{"type": "inline_keyboard", "payload": {"buttons": [[{"type": "callback", "text": "A", "payload": "a"}]]}}
		],
	}


async def test_send_message_needs_one_recipient(server):
	api = server.api()
	with pytest.raises(ValueError):
		await api.send_message(text="x")
	with pytest.raises(ValueError):
		await api.send_message(user_id=1, chat_id=2, text="x")


async def test_list_and_bool_params(server):
	server.on("GET", "/chats/5/members", {"members": [], "marker": None})
	server.on("DELETE", "/chats/5/members", {"success": True})
	server.on("GET", "/updates", {"updates": [], "marker": 77})
	api = server.api()
	await api.get_members(5, user_ids=[1, 2, 3])
	await api.remove_member(5, 9, block=True)
	result = await api.get_updates(types=["message_created", "bot_started"], timeout=10)
	assert server.calls[0].params == {"user_ids": "1,2,3"}
	assert server.calls[1].params == {"user_id": "9", "block": "true"}
	assert server.calls[2].params == {"types": "message_created,bot_started", "timeout": "10"}
	assert result.marker == 77


async def test_retries_429_honouring_retry_after(server, sleeps):
	server.on(
		"POST", "/answers", {"code": "too.many", "message": "slow down"}, status=429, headers={"Retry-After": "3"}
	)
	server.on("POST", "/answers", {"success": True})
	await server.api().answer_callback("cb1", notification="ok")
	assert sleeps == [3.0] and len(server.calls) == 2


async def test_retries_are_bounded(server, sleeps):
	server.always("DELETE", "/messages", {"code": "busy", "message": "busy"}, status=503)
	with pytest.raises(BotApiError) as info:
		await server.api().delete_message("mid.1")
	assert info.value.status_code == 503 and sleeps == [1.0, 2.0, 4.0] and len(server.calls) == 4


async def test_other_errors_are_not_retried(server, sleeps):
	server.on("PUT", "/messages", {"code": "message.not.found", "message": "nope"}, status=404)
	with pytest.raises(BotApiError) as info:
		await server.api().edit_message("mid.1", text="x")
	assert info.value.code == "message.not.found" and not isinstance(info.value, BotForbiddenError)
	assert sleeps == [] and len(server.calls) == 1


async def test_no_dialog_is_forbidden_and_unreachable(server):
	server.on("POST", "/messages", {"code": "dialog.not.found", "message": "Dialog not found"}, status=404)
	with pytest.raises(BotForbiddenError) as info:
		await server.api().send_message(user_id=1, text="x")
	assert is_unreachable(info.value)


async def test_success_false_raises(server):
	server.on("PUT", "/chats/1/pin", {"success": False, "message": "no rights"})
	with pytest.raises(BotApiError, match="no rights"):
		await server.api().pin_message(1, "mid.1")


async def test_attachment_not_ready_is_retried(server, sleeps):
	server.on("POST", "/messages", {"code": "attachment.not.ready", "message": "wait"}, status=400)
	server.on("POST", "/messages", {"code": "attachment.not.ready", "message": "wait"}, status=400)
	server.on("POST", "/messages", {"message": message()})
	await server.api().send_message(chat_id=1, attachments=[m.FileAttachmentRequest(payload=m.UploadedInfo(token="t"))])
	assert sleeps == [1.0, 2.0]


async def test_network_failure_trips_the_guard(server):
	server.on("POST", "/messages", httpx.ConnectError("down"))
	api = server.api()
	with pytest.raises(BotUnavailableError):
		await api.send_message(user_id=1, text="x")
	with pytest.raises(BotUnavailableError, match="unreachable"):
		await api.send_message(user_id=1, text="x")  # fails fast: no request made
	assert len(server.calls) == 1
	server.on("GET", "/updates", {"updates": []})
	await api.get_updates()  # long polling is the probe and reopens the guard
	server.on("POST", "/messages", {"message": message()})
	await api.send_message(user_id=1, text="x")


@pytest.mark.parametrize(
	("kind", "endpoint", "upload_answer", "expected"),
	[
		("file", {"url": "https://up/f"}, {"token": "ft"}, m.FileAttachmentRequest(payload=m.UploadedInfo(token="ft"))),
		("file", {"url": "https://up/f"}, {"file": {"token": "nested"}}, None),
		(
			"image",
			{"url": "https://up/f"},
			{"photos": {"p1": {"token": "pt"}}},
			m.PhotoAttachmentRequest(payload=m.PhotoAttachmentRequestPayload(photos={"p1": m.PhotoToken(token="pt")})),
		),
		(
			"video",
			{"url": "https://up/f", "token": "vt"},
			{},
			m.VideoAttachmentRequest(payload=m.UploadedInfo(token="vt")),
		),
	],
)
async def test_upload_flow(server, kind, endpoint, upload_answer, expected):
	server.on("POST", "/uploads", endpoint)
	server.on("POST", "https://up/f", upload_answer)
	api = server.api()
	result = await api.upload(io.BytesIO(b"hello"), type=kind, filename="hello.txt")
	if expected is None:
		assert isinstance(result, m.FileAttachmentRequest) and result.payload.token == "nested"
	else:
		assert result == expected
	assert server.calls[0].params == {"type": kind}
	upload = server.files[0]
	assert "authorization" not in upload.headers  # the file host never sees the bot token
	assert b"hello" in upload.content and b'filename="hello.txt"' in upload.content


async def test_upload_without_token_is_an_error(server):
	server.on("POST", "/uploads", {"url": "https://up/f"})
	server.on("POST", "https://up/f", {"weird": 1})
	with pytest.raises(BotApiError, match="no token"):
		await server.api().upload(b"x", type="file")


async def test_upload_from_path_uses_the_file_name(server, tmp_path):
	path = tmp_path / "report.csv"
	path.write_bytes(b"a,b")
	server.on("POST", "/uploads", {"url": "https://up/f"})
	server.on("POST", "https://up/f", {"token": "t"})
	await server.api().upload(path, type="file")
	assert b'filename="report.csv"' in server.files[0].content


async def test_subscriptions_and_chat_methods(server):
	server.on("POST", "/subscriptions", {"success": True})
	server.on("GET", "/subscriptions", {"subscriptions": [{"url": "https://me/hook", "time": 1}]})
	server.on("POST", "/chats/3/actions", {"success": True})
	server.on("GET", "/chats/3/pin", {"message": None})
	server.on("POST", "/chats/3/members", {"success": True, "failed_user_ids": [9]})
	api = server.api()
	await api.subscribe("https://me/hook", update_types=["message_created"], secret="s3cret")
	subs = await api.get_subscriptions()
	await api.send_action(3, "typing_on")
	assert await api.get_pinned_message(3) is None
	result = await api.add_members(3, [8, 9])
	assert server.calls[0].json == {"url": "https://me/hook", "secret": "s3cret", "update_types": ["message_created"]}
	assert subs[0].url == "https://me/hook"
	assert server.calls[2].json == {"action": "typing_on"}
	assert server.calls[4].json == {"user_ids": [8, 9]} and result.failed_user_ids == [9]


async def test_raw_request_for_uncovered_endpoints(server):
	server.on("GET", "/new/endpoint", {"anything": 1})
	assert await server.api().request("GET", "/new/endpoint", params={"a": None, "b": 2}) == {"anything": 1}
	assert server.calls[0].params == {"b": "2"}
