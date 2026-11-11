from __future__ import annotations

import asyncio
import logging

import pytest

from matele import MAX, Button, CallbackData, CallbackQuery, ChatStatus, Context, Dispatcher, Keyboard, Message
from matele.callbacks import CallbackRegistry, callback
from matele.errors import BotApiError
from matele.max import MaxBot
from matele.max import models as m
from matele.max.bot import is_linkable, move_unlinkable_buttons
from tests.max_fake import FakeMaxServer, message, user

reg = CallbackRegistry()


@callback("pick", registry=reg)
class Pick(CallbackData):
	item: int


@pytest.fixture
def server() -> FakeMaxServer:
	return FakeMaxServer()


@pytest.fixture
def bot(server) -> MaxBot:
	return MaxBot("t", api=server.api())


def test_parses_messages_buttons_and_lifecycle(bot):
	created = m.Update.from_dict(
		{
			"update_type": "message_created",
			"timestamp": 1,
			"message": message(
				"hello",
				sender=user(7, "Ann", last_name="Lee", username="ann"),
				attachments=[
					{"type": "file", "payload": {"url": "https://f/1", "token": "ft"}, "filename": "a.pdf"},
					{"type": "image", "payload": {"photo_id": 1, "token": "it", "url": "https://i/1"}},
					{"type": "sticker", "payload": {"url": "https://s", "code": "x"}},
				],
			),
		}
	)
	(event,) = bot.parse_update(created)
	assert isinstance(event, Message)
	assert event.text == "hello" and event.message_id == "mid.1" and event.chat_id == 100
	assert event.from_user.id == 7 and event.from_user.name == "Ann Lee" and event.from_user.platform == MAX
	assert [(a.type, a.token, a.filename) for a in event.attachments] == [
		("file", "ft", "a.pdf"),
		("image", "it", "image.jpg"),
	]

	(pressed,) = bot.parse_update(
		m.Update.from_dict(
			{
				"update_type": "message_callback",
				"timestamp": 1,
				"callback": {"timestamp": 1, "callback_id": "cb1", "payload": "pick:3", "user": user(7)},
				"message": message("menu", mid="mid.menu"),
			}
		)
	)
	assert isinstance(pressed, CallbackQuery) and pressed.data == "pick:3"
	assert pressed.message is not None and pressed.message.message_id == "mid.menu"

	reachable, started = bot.parse_update(
		m.Update.from_dict(
			{"update_type": "bot_started", "timestamp": 1, "chat_id": 100, "user": user(7), "payload": "ref42"}
		)
	)
	assert isinstance(reachable, ChatStatus) and reachable.reachable and reachable.from_user.id == 7
	assert isinstance(started, Message) and started.text == "/start ref42"

	(stopped,) = bot.parse_update(
		m.Update.from_dict({"update_type": "bot_stopped", "timestamp": 1, "chat_id": 100, "user": user(7)})
	)
	assert isinstance(stopped, ChatStatus) and not stopped.reachable


@pytest.mark.parametrize(
	"raw",
	[
		{"update_type": "message_created", "timestamp": 1, "message": message(chat_type="channel")},
		{"update_type": "message_created", "timestamp": 1, "message": {**message(), "sender": None}},
		{"update_type": "dialog_muted", "timestamp": 1, "chat_id": 1, "user": user()},
		{"update_type": "brand_new_update", "timestamp": 1},
	],
)
def test_updates_that_are_not_routed(bot, raw):
	assert bot.parse_update(m.Update.from_dict(raw)) == []


async def test_get_events_keeps_the_marker_and_skips_broken_updates(server, bot):
	server.on(
		"GET",
		"/updates",
		{
			"updates": [
				{"update_type": "message_created", "timestamp": 1, "message": message("one")},
				{"update_type": "message_callback", "timestamp": 1, "callback": {"callback_id": "x"}},  # no user
				{"update_type": "message_created", "timestamp": 1, "message": message("two")},
			],
			"marker": 55,
		},
	)
	server.on("GET", "/updates", {"updates": [], "marker": None})
	events = await bot.get_events(timeout=5)
	assert [e.text for e in events if isinstance(e, Message)] == ["one", "two"]
	await bot.get_events(timeout=5)
	polls = [c for c in server.calls if c.path == "/updates"]
	assert "marker" not in polls[0].params and polls[1].params["marker"] == "55"


async def test_send_with_keyboard_and_edit_without_one(server, bot):
	server.on("POST", "/messages", {"message": message("menu", mid="mid.5")})
	server.on("PUT", "/messages", {"success": True})
	keyboard = Keyboard([[Button("Pick", Pick(3).pack()), Button("Site", url="https://example.ru")]])
	sent = await bot.send_message("menu", user_id=7, keyboard=keyboard)
	await bot.edit_message(sent.message_id, "done")
	assert server.calls[0].json == {
		"text": "menu",
		"format": "html",
		"attachments": [
			{
				"type": "inline_keyboard",
				"payload": {
					"buttons": [
						[
							{"type": "callback", "text": "Pick", "payload": "pick:3"},
							{"type": "link", "text": "Site", "url": "https://example.ru"},
						]
					]
				},
			}
		],
	}
	assert server.calls[1].params == {"message_id": "mid.5"}
	assert server.calls[1].json == {"text": "done", "format": "html", "attachments": []}


def test_local_link_buttons_move_into_the_text():
	keyboard = Keyboard(
		[
			[Button("Open", url="http://localhost:3000/x"), Button("Go", data="go")],
			[Button("Public", url="https://example.ru")],
			[Button("Lan", url="http://127.0.0.1/a")],
		]
	)
	text, kept = move_unlinkable_buttons("Hi", keyboard)
	assert text == "Hi\n\nOpen: http://localhost:3000/x\nLan: http://127.0.0.1/a"
	assert kept is not None and [[b.text for b in row] for row in kept.rows] == [["Go"], ["Public"]]
	assert move_unlinkable_buttons("x", None) == ("x", None)


async def test_alert_is_also_sent_as_a_message(server, bot):
	server.on("POST", "/answers", {"success": True})
	server.on("POST", "/messages", {"message": message()})
	bot.parse_update(
		m.Update.from_dict(
			{
				"update_type": "message_callback",
				"timestamp": 1,
				"callback": {"timestamp": 1, "callback_id": "cb9", "payload": "x", "user": user(7)},
			}
		)
	)
	await bot.answer_callback("cb9", text="No <access>", alert=True)
	assert server.calls[0].params == {"callback_id": "cb9"} and server.calls[0].json == {"notification": "No <access>"}
	assert server.calls[1].params == {"user_id": "7"} and server.calls[1].json["text"] == "No &lt;access&gt;"


async def test_documents_upload_then_send(server, bot):
	server.on("POST", "/uploads", {"url": "https://up/1"})
	server.on("POST", "https://up/1", {"token": "ft"})
	server.on("POST", "/messages", {"message": message(None)})
	await bot.send_document(b"data", "r.csv", user_id=7, caption="Report")
	assert server.calls[1].json == {
		"text": "Report",
		"format": "html",
		"attachments": [{"type": "file", "payload": {"token": "ft"}}],
	}


async def test_end_to_end_with_the_dispatcher(server, bot):
	dp = Dispatcher(registry=reg)

	@dp.command("start")
	async def start(ctx: Context):
		await ctx.reply(f"ref={ctx.args}", Keyboard([[Button("Pick 3", Pick(3).pack())]]))

	@dp.callback(Pick)
	async def pick(ctx: Context, payload: Pick):
		await ctx.answer(f"picked {payload.item}")
		await ctx.edit(f"You picked {payload.item}")

	server.on("POST", "/messages", {"message": message("ref=ref42", mid="mid.menu")})
	server.always("POST", "/answers", {"success": True})
	server.always("PUT", "/messages", {"success": True})
	started = m.Update.from_dict(
		{"update_type": "bot_started", "timestamp": 1, "chat_id": 100, "user": user(7), "payload": "ref42"}
	)
	pressed = m.Update.from_dict(
		{
			"update_type": "message_callback",
			"timestamp": 1,
			"callback": {"timestamp": 1, "callback_id": "cb1", "payload": "pick:3", "user": user(7)},
			"message": message("ref=ref42", mid="mid.menu"),
		}
	)
	for update in (started, pressed):
		for event in bot.parse_update(update):
			await dp.feed(event)
	sent, answer, edit = server.calls
	assert sent.json["text"] == "ref=ref42"
	assert answer.json == {"notification": "picked 3"}
	assert edit.params == {"message_id": "mid.menu"} and edit.json["text"] == "You picked 3"


async def test_polling_a_real_max_transport(server, bot):

	dp = Dispatcher(registry=reg)
	done = asyncio.Event()

	@dp.message()
	async def echo(ctx: Context):
		await ctx.reply(ctx.text.upper())
		done.set()

	server.on("GET", "/me", {**user(1, "Bot", username="my_bot", is_bot=True)})
	server.on(
		"GET",
		"/updates",
		{"updates": [{"update_type": "message_created", "timestamp": 1, "message": message("ping")}], "marker": 1},
	)
	server.always("GET", "/updates", {"updates": [], "marker": 1})
	server.on("POST", "/messages", {"message": message("PING")})
	task = asyncio.create_task(dp.start_polling(bot, timeout=1))
	await asyncio.wait_for(done.wait(), 5)
	task.cancel()
	with pytest.raises(asyncio.CancelledError):
		await task
	assert [c for c in server.calls if c.method == "POST"][0].json["text"] == "PING"
	assert bot.me is not None and bot.me.username == "my_bot"


async def test_with_a_secret_only_buttons_the_bot_made_are_accepted(server):
	from matele.max import models as m

	bot = MaxBot("secret-token", api=server.api(), callback_secret="s3cret")
	server.on("POST", "/messages", {"message": message()})
	await bot.send_message("menu", user_id=7, keyboard=Keyboard([[Button("Order", "order:view:42")]]))
	sent = server.calls[-1].json["attachments"][0]["payload"]["buttons"][0][0]["payload"]
	assert sent.startswith("order:view:42#")

	def press(data: str) -> m.Update:
		return m.Update.from_dict(
			{
				"update_type": "message_callback",
				"timestamp": 1,
				"callback": {"timestamp": 1, "callback_id": "cb", "payload": data, "user": user()},
				"message": message(),
			}
		)

	(real,) = bot.parse_update(press(sent))
	assert isinstance(real, CallbackQuery) and real.data == "order:view:42"
	(forged,) = bot.parse_update(press("order:view:43"))
	assert isinstance(forged, CallbackQuery) and forged.data == ""


# ------------------------------------------------------------ edits keep the media
PHOTO = {"type": "image", "payload": {"photo_id": 1, "token": "photo-token", "url": "https://i.max.test/1"}}
FILE = {
	"type": "file",
	"payload": {"url": "https://f.max.test/1", "token": "file-token"},
	"filename": "a.pdf",
	"size": 3,
}
OLD_KEYBOARD = {
	"type": "inline_keyboard",
	"payload": {"buttons": [[{"type": "callback", "text": "x", "payload": "x"}]]},
}


def _press(mid: str, attachments: list) -> m.Update:
	return m.Update.from_dict(
		{
			"update_type": "message_callback",
			"timestamp": 1,
			"callback": {"timestamp": 1, "callback_id": "cb1", "payload": "pick:3", "user": user(7)},
			"message": message("caption", mid=mid, attachments=attachments),
		}
	)


async def test_editing_a_pressed_photo_keeps_the_photo(server, bot):
	server.always("PUT", "/messages", {"success": True})
	bot.parse_update(_press("mid.p", [PHOTO, OLD_KEYBOARD]))
	await bot.edit_message("mid.p", None)  # remove the buttons
	await bot.edit_message("mid.p", "new caption", keyboard=Keyboard([[Button("Again", Pick(4).pack())]]))
	first, second = (c.json for c in server.calls)
	assert [c.method for c in server.calls] == ["PUT", "PUT"]  # known from the press: no lookup
	assert first["attachments"] == [{"type": "image", "payload": {"token": "photo-token"}}]
	assert second["attachments"][0] == {"type": "image", "payload": {"token": "photo-token"}}
	assert second["attachments"][1]["type"] == "inline_keyboard"


async def test_editing_an_unknown_message_looks_up_its_media_once(server, bot):
	server.on("GET", "/messages/mid.f", message("report", mid="mid.f", attachments=[FILE, OLD_KEYBOARD]))
	server.always("PUT", "/messages", {"success": True})
	await bot.edit_message("mid.f", "report v2")
	await bot.edit_message("mid.f", "report v3")
	assert [(c.method, c.path) for c in server.calls] == [
		("GET", "/messages/mid.f"),
		("PUT", "/messages"),
		("PUT", "/messages"),
	]
	for call in server.calls[1:]:
		assert call.json["attachments"] == [{"type": "file", "payload": {"token": "file-token"}}]


async def test_editing_a_sent_file_keeps_the_file(server, bot):
	server.on("POST", "/messages", {"message": message("doc", mid="mid.s", attachments=[FILE, OLD_KEYBOARD])})
	server.always("PUT", "/messages", {"success": True})
	sent = await bot.send_file_by_token("file-token", user_id=7, caption="doc")
	await bot.edit_message(sent.message_id, "doc, checked")
	assert server.calls[-1].json["attachments"] == [{"type": "file", "payload": {"token": "file-token"}}]


async def test_ctx_edit_of_a_text_menu_sends_only_the_keyboard(server, bot):
	dp = Dispatcher(registry=reg)

	@dp.callback(Pick)
	async def pick(ctx: Context, payload: Pick):
		await ctx.edit("done")

	server.always("POST", "/answers", {"success": True})
	server.always("PUT", "/messages", {"success": True})
	for event in bot.parse_update(_press("mid.t", [OLD_KEYBOARD])):
		await dp.feed(event)
	edit = next(c for c in server.calls if c.method == "PUT")
	assert edit.json["attachments"] == []  # the old keyboard goes, nothing else to keep
	assert not any(c.method == "GET" for c in server.calls)


@pytest.mark.parametrize(
	("url", "linkable"),
	[
		("https://example.ru/a", True),
		("http://shop.example.com:8080/x", True),
		("http://localhost:3000", False),
		("http://app.localhost/", False),
		("http://127.0.0.1/a", False),
		("http://0.0.0.0:8000", False),
		("http://192.168.1.10/admin", False),
		("http://10.0.0.5", False),
		("http://8.8.8.8/", False),
		("http://[::1]:8000/", False),
		("http://[broken/", False),
		("tg://resolve?domain=x", False),
		("example.ru", False),
	],
)
def test_which_links_max_takes(url, linkable):
	assert is_linkable(url) is linkable


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


async def test_max_polling_warns_about_a_subscribed_webhook(caplog):
	server = FakeMaxServer()
	server.on("GET", "/subscriptions", {"subscriptions": [{"url": "https://old.example.com/max", "time": 1}]})
	server.always("GET", "/updates", {"updates": [], "marker": None})
	bot = MaxBot("t", api=server.api())
	await bot.get_events(timeout=1)
	await bot.get_events(timeout=1)
	assert "https://old.example.com/max" in caplog.text
	assert [c.path for c in server.calls].count("/subscriptions") == 1  # checked once


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
	server.on("GET", "/messages/mid.1", message("menu"))
	server.on("PUT", "/messages", {"success": True})
	keyboard = Keyboard([[Button("Local", url="http://localhost:3000"), Button("Site", url="https://example.com")]])
	with caplog.at_level(logging.WARNING, logger="matele.max.bot"):
		await MaxBot("t", api=server.api()).edit_message("mid.1", None, keyboard=keyboard)
	body = server.calls[-1].json
	assert "text" not in body  # the message keeps its text
	buttons = body["attachments"][0]["payload"]["buttons"][0]
	assert buttons[0] == {"type": "callback", "text": "Local", "payload": "noop"}
	assert buttons[1]["type"] == "link" and buttons[1]["url"] == "https://example.com"
	assert "localhost:3000" in caplog.text


def test_moved_links_without_a_text_do_not_start_with_blank_lines():
	text, _ = move_unlinkable_buttons(None, Keyboard([[Button("Local", url="http://localhost:3000")]]))
	assert text == "Local: http://localhost:3000"
