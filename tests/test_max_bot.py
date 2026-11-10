from __future__ import annotations

import pytest

from matele import MAX, Button, CallbackData, CallbackQuery, ChatStatus, Context, Dispatcher, Keyboard, Message
from matele.callbacks import CallbackRegistry, callback
from matele.max import MaxBot
from matele.max import models as m
from matele.max.bot import move_unlinkable_buttons
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
	import asyncio

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
