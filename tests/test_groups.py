from __future__ import annotations

import pytest

from matele import GROUP, PRIVATE, ChatMemberUpdate, ChatType, Context, Dispatcher, Message, State, StorageKey
from matele.callbacks import CallbackRegistry
from matele.max import MaxBot
from matele.max import models as m
from matele.telegram import TelegramBot
from matele.testing import FakeBot
from tests.max_fake import FakeMaxServer, message, user
from tests.test_telegram import TOKEN, FakeTelegram, tg_message

GROUP_ID = -100
ASK = State("ask")


@pytest.fixture
def dp() -> Dispatcher:
	return Dispatcher(registry=CallbackRegistry())


@pytest.fixture
def bot() -> FakeBot:
	return FakeBot()


# ---------------------------------------------------------------- dispatcher
async def test_reply_goes_where_the_message_came_from(dp, bot):
	@dp.message()
	async def echo(ctx: Context):
		await ctx.reply(f"{ctx.chat_type}:{ctx.chat_id}")

	await dp.feed(bot.message("hi", user_id=1, chat_id=GROUP_ID))
	await dp.feed(bot.message("hi", user_id=1))
	group, private = bot.sent
	assert (group.chat_id, group.user_id, group.text) == (GROUP_ID, None, f"{GROUP}:{GROUP_ID}")
	assert (private.chat_id, private.user_id, private.text) == (None, 1, f"{PRIVATE}:None")


async def test_send_private_from_a_group(dp, bot):
	@dp.command("secret")
	async def secret(ctx: Context):
		await ctx.send_private("only for you")

	await dp.feed(bot.message("/secret", user_id=5, chat_id=GROUP_ID))
	assert (bot.sent[0].user_id, bot.sent[0].chat_id) == (5, None)


async def test_chat_type_filter(dp, bot):
	@dp.command("start", ChatType(PRIVATE))
	async def start_private(ctx: Context):
		await ctx.reply("private start")

	@dp.command("start", ChatType(GROUP))
	async def start_group(ctx: Context):
		await ctx.reply("group start")

	await dp.feed(bot.message("/start"))
	await dp.feed(bot.message("/start", chat_id=GROUP_ID))
	assert bot.texts == ["private start", "group start"]


async def test_state_is_per_person_and_per_chat(dp, bot):
	@dp.command("ask")
	async def ask(ctx: Context):
		ctx.state.set(ASK)

	@dp.message(state=ASK)
	async def answered(ctx: Context):
		ctx.state.clear()
		await ctx.reply(f"got {ctx.text}")

	@dp.message()
	async def other(ctx: Context):
		await ctx.reply("not asked")

	await dp.feed(bot.message("/ask", user_id=1, chat_id=GROUP_ID))
	await dp.feed(bot.message("x", user_id=2, chat_id=GROUP_ID))  # another person in the same group
	await dp.feed(bot.message("x", user_id=1))  # the same person, privately
	await dp.feed(bot.message("x", user_id=1, chat_id=-200))  # the same person, another group
	await dp.feed(bot.message("42", user_id=1, chat_id=GROUP_ID))
	assert bot.texts == ["not asked", "not asked", "not asked", "got 42"]
	assert await dp.storage.get(StorageKey(bot.platform, 1, GROUP_ID)) is None  # cleared


async def test_commands_for_other_bots_are_ignored(dp, bot):
	@dp.command("help")
	async def help_(ctx: Context):
		await ctx.reply("help")

	@dp.message()
	async def anything(ctx: Context):
		await ctx.reply("text")

	assert not await dp.feed(bot.message("/help@other_bot", chat_id=GROUP_ID))
	assert await dp.feed(bot.message("/help@Fake_Bot", chat_id=GROUP_ID))  # ours, any case
	assert await dp.feed(bot.message("/help", chat_id=GROUP_ID))
	assert bot.texts == ["help", "help"]


async def test_member_updates(dp, bot):
	seen = []

	@dp.member_update()
	async def members(ctx: Context):
		update = ctx.member_update
		assert update is not None
		seen.append((update.member.id, update.joined, update.is_me, update.from_user.id))
		if update.is_me and update.joined:
			await ctx.reply("Hi all!")
		elif update.joined:
			await ctx.reply(f"Welcome, {update.member.id}")

	await dp.feed(bot.member(GROUP_ID, user_id=bot.bot_id, by=7))
	await dp.feed(bot.member(GROUP_ID, user_id=3))
	await dp.feed(bot.member(GROUP_ID, user_id=3, joined=False))
	assert seen == [(bot.bot_id, True, True, 7), (3, True, False, 3), (3, False, False, 3)]
	assert bot.texts == ["Hi all!", "Welcome, 3"] and bot.sent[0].chat_id == GROUP_ID


# ---------------------------------------------------------------- MAX
@pytest.fixture
def server() -> FakeMaxServer:
	return FakeMaxServer()


def test_max_group_messages_and_members(server):
	bot = MaxBot("t", api=server.api())
	from matele.types import User

	bot.me = User(id=1, platform="max", username="my_bot")
	(msg,) = bot.parse_update(
		m.Update.from_dict(
			{"update_type": "message_created", "timestamp": 1, "message": message("hi", chat_type="chat", chat_id=-5)}
		)
	)
	assert isinstance(msg, Message) and msg.chat_type == GROUP and msg.chat_id == -5

	def parse(raw):
		events = bot.parse_update(m.Update.from_dict({"timestamp": 1, "chat_id": -5, "is_channel": False, **raw}))
		assert len(events) <= 1
		if not events:
			return None
		event = events[0]
		assert isinstance(event, ChatMemberUpdate)
		return event

	added = parse({"update_type": "bot_added", "user": user(7)})
	assert added is not None and added.is_me and added.joined and added.member.id == 1 and added.from_user.id == 7
	joined = parse({"update_type": "user_added", "user": user(3), "inviter_id": 7})
	assert joined is not None and not joined.is_me and joined.member.id == 3 and joined.from_user.id == 7
	left = parse({"update_type": "user_removed", "user": user(3)})
	assert left is not None and not left.joined and left.from_user.id == 3  # left on their own
	removed = parse({"update_type": "bot_removed", "user": user(7)})
	assert removed is not None and removed.is_me and not removed.joined
	assert parse({"update_type": "bot_added", "user": user(7), "is_channel": True}) is None


async def test_max_sends_to_a_chat(server):
	server.on("POST", "/messages", {"message": message("ok", chat_type="chat", chat_id=-5)})
	sent = await MaxBot("t", api=server.api()).send_message("hello", chat_id=-5)
	assert server.calls[0].params == {"chat_id": "-5"} and sent.chat_type == GROUP
	with pytest.raises(ValueError):
		await MaxBot("t", api=server.api()).send_message("x")


async def test_max_alert_from_a_group_goes_privately_and_never_fails(server):
	bot = MaxBot("t", api=server.api())
	bot.parse_update(
		m.Update.from_dict(
			{
				"update_type": "message_callback",
				"timestamp": 1,
				"callback": {"timestamp": 1, "callback_id": "cb", "payload": "x", "user": user(7)},
				"message": message("menu", chat_type="chat", chat_id=-5),
			}
		)
	)
	server.on("POST", "/answers", {"success": True})
	server.on("POST", "/messages", {"code": "dialog.not.found", "message": "no dialog"}, status=404)
	await bot.answer_callback("cb", text="Not for you", alert=True)  # no exception
	assert server.calls[1].params == {"user_id": "7"}


# ---------------------------------------------------------------- Telegram
async def test_telegram_group_messages_and_members():
	tg = FakeTelegram()
	bot = TelegramBot(TOKEN, api=tg.api())
	from matele.types import User

	bot.me = User(id=99, platform="telegram", username="my_bot")
	(msg,) = await bot.parse_update({"update_id": 1, "message": tg_message("hi", chat_type="supergroup", chat_id=-5)})
	assert isinstance(msg, Message) and msg.chat_type == GROUP and msg.chat_id == -5

	joined = await bot.parse_update(
		{
			"update_id": 2,
			"message": tg_message(
				None,
				chat_type="group",
				chat_id=-5,
				new_chat_members=[
					{"id": 3, "first_name": "A"},
					{"id": 4, "first_name": "B"},
					{"id": 99, "first_name": "Me"},
				],
			),
		}
	)
	assert [(e.member.id, e.joined, e.is_me) for e in joined if isinstance(e, ChatMemberUpdate)] == [
		(3, True, False),
		(4, True, False),
	]  # the bot's own join comes as my_chat_member
	(left,) = await bot.parse_update(
		{
			"update_id": 3,
			"message": tg_message(None, chat_type="group", chat_id=-5, left_chat_member={"id": 3, "first_name": "A"}),
		}
	)
	assert isinstance(left, ChatMemberUpdate) and not left.joined

	def my_member(old: str, new: str) -> dict:
		return {
			"update_id": 4,
			"my_chat_member": {
				"chat": {"id": -5, "type": "supergroup"},
				"from": {"id": 7, "first_name": "Admin"},
				"old_chat_member": {"status": old, "user": {"id": 99, "first_name": "Me"}},
				"new_chat_member": {"status": new, "user": {"id": 99, "first_name": "Me"}},
			},
		}

	(added,) = await bot.parse_update(my_member("left", "member"))
	assert isinstance(added, ChatMemberUpdate) and added.is_me and added.joined and added.from_user.id == 7
	assert await bot.parse_update(my_member("member", "administrator")) == []  # a promotion, not a join
	(kicked,) = await bot.parse_update(my_member("administrator", "kicked"))
	assert isinstance(kicked, ChatMemberUpdate) and not kicked.joined


async def test_telegram_sends_to_a_chat():
	tg = FakeTelegram()
	tg.ok("sendMessage", tg_message("ok", chat_type="group", chat_id=-5))
	await TelegramBot(TOKEN, api=tg.api()).send_message("hello", chat_id=-5)
	assert tg.calls[0][1]["chat_id"] == -5
