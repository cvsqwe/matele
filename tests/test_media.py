from __future__ import annotations

import asyncio
import io

import pytest

from matele import Button, Context, Dispatcher, Keyboard
from matele.callbacks import CallbackRegistry
from matele.max import MaxBot
from matele.telegram import TelegramBot
from matele.testing import FakeBot
from tests.max_fake import FakeMaxServer, message
from tests.telegram_fake import TOKEN, FakeTelegram, tg_message


@pytest.fixture
def dp() -> Dispatcher:
	return Dispatcher(registry=CallbackRegistry())


# ---------------------------------------------------------------- Context
async def test_reply_with_quote(dp):
	bot = FakeBot()

	@dp.message()
	async def h(ctx: Context):
		await ctx.reply("quoted", quote=True)
		await ctx.reply("plain")

	incoming = bot.message("hi")
	await dp.feed(incoming)
	assert [s.reply_to for s in bot.sent] == [incoming.message_id, None]


async def test_reply_file_from_bytes_path_and_stream(dp, tmp_path):
	bot = FakeBot()
	photo = tmp_path / "cat.jpg"
	photo.write_bytes(b"JPEG")

	@dp.message()
	async def h(ctx: Context):
		await ctx.reply_file(photo, kind="image", caption="cat")
		await ctx.reply_file(io.BytesIO(b"mp3"), kind="audio", filename="song.mp3")
		await ctx.reply_file(b"a,b", filename="r.csv", keyboard=Keyboard([[Button("ok", "ok")]]))

	await dp.feed(bot.message("files", chat_id=-5))
	assert [(s.kind, s.document, s.text, s.chat_id) for s in bot.sent] == [
		("image", ("cat.jpg", b"JPEG"), "cat", -5),
		("audio", ("song.mp3", b"mp3"), None, -5),
		("file", ("r.csv", b"a,b"), None, -5),
	]
	assert bot.sent[2].keyboard is not None


async def test_typing_is_renewed_and_stops(dp):
	bot = FakeBot()

	@dp.message()
	async def slow(ctx: Context):
		async with ctx.typing(every=0.01):
			await asyncio.sleep(0.05)
		await ctx.reply("done")

	incoming = bot.message("work", chat_id=-5)
	await dp.feed(incoming)
	shown = len(bot.typing)
	assert shown >= 2 and set(bot.typing) == {-5}
	await asyncio.sleep(0.05)
	assert len(bot.typing) == shown  # stopped after the block
	assert bot.texts == ["done"]


# ---------------------------------------------------------------- MAX
async def test_max_files_replies_and_typing(tmp_path):
	server = FakeMaxServer()
	bot = MaxBot("t", api=server.api())
	photo = tmp_path / "cat.jpg"
	photo.write_bytes(b"JPEG")
	server.on("POST", "/uploads", {"url": "https://up/1"})
	server.on("POST", "https://up/1", {"photos": {"p": {"token": "pt"}}})
	server.on("POST", "/messages", {"message": message(None)})
	server.on("POST", "/messages", {"message": message("re")})
	server.on("POST", "/chats/100/actions", {"success": True})

	await bot.send_file(
		photo, kind="image", chat_id=100, caption="cat", keyboard=Keyboard([[Button("ok", "ok")]]), reply_to="mid.0"
	)
	await bot.send_message("re", user_id=7, reply_to="mid.1")
	await bot.send_typing(100)

	upload, send_photo, send_text, action = server.calls
	assert upload.params == {"type": "image"}
	assert b'filename="cat.jpg"' in server.files[0].content
	assert send_photo.json["attachments"][0] == {"type": "image", "payload": {"photos": {"p": {"token": "pt"}}}}
	assert send_photo.json["attachments"][1]["type"] == "inline_keyboard"
	assert send_photo.json["link"] == {"type": "reply", "mid": "mid.0"} and send_photo.json["text"] == "cat"
	assert send_text.json["link"] == {"type": "reply", "mid": "mid.1"}
	assert action.json == {"action": "typing_on"}


# ---------------------------------------------------------------- Telegram
async def test_telegram_files_replies_and_typing(tmp_path):
	tg = FakeTelegram()
	bot = TelegramBot(TOKEN, api=tg.api())
	photo = tmp_path / "cat.jpg"
	photo.write_bytes(b"JPEG")
	tg.ok("sendPhoto", tg_message(None, chat_id=-5, chat_type="group", message_id=3))
	tg.ok("sendMessage", tg_message("re"))

	await bot.send_file(
		photo, kind="image", chat_id=-5, caption="cat", keyboard=Keyboard([[Button("ok", "ok")]]), reply_to="-5:2"
	)
	await bot.send_message("re", user_id=7, reply_to="7:9")
	await bot.send_typing(-5)

	(m1, photo_body, photo_req), (m2, text_body, _), (m3, typing_body, _) = tg.calls
	assert (m1, m2, m3) == ("sendPhoto", "sendMessage", "sendChatAction")
	assert photo_req.headers["content-type"].startswith("multipart/form-data")
	assert b'name="photo"; filename="cat.jpg"' in photo_body and b"JPEG" in photo_body
	assert b'name="caption"' in photo_body and b"inline_keyboard" in photo_body
	assert b'"message_id": 2' in photo_body
	assert text_body["reply_parameters"] == {"message_id": 9, "allow_sending_without_reply": True}
	assert typing_body == {"chat_id": -5, "action": "typing"}


@pytest.mark.parametrize(("kind", "method"), [("video", "sendVideo"), ("audio", "sendAudio"), ("file", "sendDocument")])
async def test_telegram_file_kinds(kind, method):
	tg = FakeTelegram()
	tg.ok(method, tg_message(None))
	await TelegramBot(TOKEN, api=tg.api()).send_file(b"data", kind=kind, filename="x.bin", user_id=7)
	assert tg.calls[0][0] == method


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
