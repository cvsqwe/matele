from __future__ import annotations

import asyncio
from typing import Literal

import pytest

from matele import (
	MAX,
	TELEGRAM,
	Attachment,
	CallbackData,
	CallbackRegistry,
	Context,
	Dispatcher,
	HasAttachment,
	MemoryStorage,
	OnPlatform,
	Regex,
	Router,
	State,
	StatesGroup,
	StorageKey,
	Text,
	action,
	as_filter,
	callback,
)
from matele.dispatch import parse_command
from matele.testing import FakeBot

reg = CallbackRegistry()


@callback("item:set", registry=reg)
class SetItem(CallbackData):
	item_id: int
	status: Literal["on", "off"]


HOME = action("home", registry=reg)


class Form(StatesGroup):
	name = State()
	age = State()


@pytest.fixture
def dp() -> Dispatcher:
	return Dispatcher(registry=reg)


@pytest.fixture
def bot() -> FakeBot:
	return FakeBot()


# ---------------------------------------------------------------- commands
@pytest.mark.parametrize(
	("text", "expected"),
	[
		("/start", ("start", None, None)),
		("/start abc", ("start", "abc", None)),
		("/Menu@my_bot", ("menu", None, "my_bot")),
		("/go\nline two", ("go", "line two", None)),
		("/", None),
		("/ start", None),
		("hello", None),
		("", None),
	],
)
def test_parse_command(text, expected):
	assert parse_command(text) == expected


async def test_command_gets_its_arguments(dp, bot):
	@dp.command("start", "/begin")
	async def start(ctx: Context):
		await ctx.reply(f"hi {ctx.command} {ctx.args}")

	assert await dp.feed(bot.message("/START ref42"))
	assert await dp.feed(bot.message("/begin"))
	assert not await dp.feed(bot.message("/other"))
	assert bot.texts == ["hi start ref42", "hi begin None"]


def test_command_needs_a_name(dp):
	with pytest.raises(ValueError):
		dp.command(as_filter(lambda ctx: True))
	with pytest.raises(ValueError):
		dp.command("two words")


# ---------------------------------------------------------------- order of handlers
async def test_commands_win_over_state_input_and_state_input_over_catch_all(dp, bot):
	@dp.message()
	async def catch_all(ctx):
		await ctx.reply("catch-all")

	@dp.message(state=Form.name)
	async def name(ctx):
		ctx.state.update(name=ctx.text)
		ctx.state.set(Form.age)
		await ctx.reply("age?")

	@dp.command("cancel")
	async def cancel(ctx):
		ctx.state.clear()
		await ctx.reply("cancelled")

	@dp.command("form")
	async def form(ctx):
		ctx.state.set(Form.name)
		await ctx.reply("name?")

	for text in ["/form", "Ann", "/cancel", "hello"]:
		await dp.feed(bot.message(text))
	assert bot.texts == ["name?", "age?", "cancelled", "catch-all"]


async def test_state_survives_between_events_in_storage(bot):
	storage = MemoryStorage()
	dp = Dispatcher(storage=storage, registry=reg)

	@dp.command("form")
	async def form(ctx):
		ctx.state.set(Form.name)

	@dp.message(state=Form.name)
	async def name(ctx):
		ctx.state.update(name=ctx.text)
		ctx.state.set(Form.age)

	@dp.message(state=Form.age)
	async def age(ctx):
		await ctx.reply(f"{ctx.state.get('name')} is {ctx.text}")
		ctx.state.clear()

	await dp.feed(bot.message("/form", user_id=7))
	await dp.feed(bot.message("Ann", user_id=7))
	stored = await storage.get(StorageKey(MAX, 7))
	assert stored is not None and stored.state == "Form:age" and stored.data == {"name": "Ann"}
	await dp.feed(bot.message("30", user_id=7))
	assert bot.texts == ["Ann is 30"]
	assert await storage.get(StorageKey(MAX, 7)) is None  # emptied state is deleted


async def test_people_have_separate_states(dp, bot):
	@dp.command("form")
	async def form(ctx):
		ctx.state.set(Form.name)

	@dp.message(state=Form.name)
	async def name(ctx):
		await ctx.reply("in form")

	@dp.message(state=None)
	async def idle(ctx):
		await ctx.reply("idle")

	await dp.feed(bot.message("/form", user_id=1))
	await dp.feed(bot.message("x", user_id=1))
	await dp.feed(bot.message("x", user_id=2))
	assert bot.texts == ["in form", "idle"]


# ---------------------------------------------------------------- buttons
async def test_typed_button_payload(dp, bot):
	seen = []

	@dp.callback(SetItem)
	async def set_item(ctx: Context, payload: SetItem):
		seen.append(payload)
		await ctx.answer("saved")
		await ctx.edit("updated")

	@dp.callback(HOME)
	async def home(ctx, payload):
		await ctx.reply("home")

	shown = await bot.send_message("menu", user_id=1)
	assert await dp.feed(bot.press(SetItem(5, "off"), message=shown))
	assert await dp.feed(bot.press("home", message=shown))
	assert seen == [SetItem(5, "off")]
	assert bot.answers[0].text == "saved"  # the handler's answer, not a second silent one
	assert len(bot.answers) == 2  # the `home` press was answered silently
	assert bot.edits[0].message_id == shown.message_id and bot.edits[0].text == "updated"


async def test_stale_buttons_reach_raw_callback_and_are_always_answered(dp, bot):
	stale = []

	@dp.raw_callback()
	async def outdated(ctx):
		stale.append((ctx.callback.data, ctx.payload))

	assert await dp.feed(bot.press("removed:feature:1"))
	assert await dp.feed(bot.press("item:set:x:on"))  # doesn't parse
	assert stale == [("removed:feature:1", None), ("item:set:x:on", None)]
	assert len(bot.answers) == 2


async def test_unhandled_button_is_still_answered(dp, bot):
	assert not await dp.feed(bot.press(SetItem(1, "on")))
	assert len(bot.answers) == 1 and bot.answers[0].text is None


async def test_button_bound_to_a_state(dp, bot):
	@dp.callback(HOME, state=Form)
	async def in_form(ctx, payload):
		await ctx.reply("form home")

	@dp.callback(HOME)
	async def anywhere(ctx, payload):
		await ctx.reply("home")

	@dp.command("form")
	async def form(ctx):
		ctx.state.set(Form.name)

	await dp.feed(bot.press(HOME))
	await dp.feed(bot.message("/form"))
	await dp.feed(bot.press(HOME))
	assert bot.texts == ["home", "form home"]


async def test_edit_needs_a_button(dp, bot):
	@dp.message()
	async def wrong(ctx):
		await ctx.edit("x")

	errors = []

	@dp.on_error
	async def on_error(ctx, exc):
		errors.append(exc)

	await dp.feed(bot.message("hi"))
	assert isinstance(errors[0], RuntimeError)


# ---------------------------------------------------------------- filters
async def test_filters_sync_async_and_combined(dp, bot):
	admins = {1}
	is_admin = as_filter(lambda ctx: ctx.user.id in admins)

	async def is_even(ctx):
		await asyncio.sleep(0)
		return ctx.user.id % 2 == 0

	@dp.message(Text("ping", ignore_case=True), is_admin)
	async def admin_ping(ctx):
		await ctx.reply("admin pong")

	@dp.message(Text("ping") & (~is_admin | is_even))
	async def user_ping(ctx):
		await ctx.reply("pong")

	await dp.feed(bot.message(" PING ", user_id=1))
	await dp.feed(bot.message("ping", user_id=3))
	await dp.feed(bot.message("pong", user_id=3))
	assert bot.texts == ["admin pong", "pong"]


async def test_filters_stop_at_the_first_refusal(dp, bot):
	calls = []

	def no(ctx):
		calls.append("no")
		return False

	def never(ctx):
		calls.append("never")
		return True

	@dp.message(no, never)
	async def h(ctx): ...

	await dp.feed(bot.message("x"))
	assert calls == ["no"]


async def test_regex_puts_the_match_on_ctx(dp, bot):
	@dp.message(Regex(r"order #(\d+)"))
	async def order(ctx):
		assert ctx.match is not None
		await ctx.reply(ctx.match.group(1))

	await dp.feed(bot.message("where is order #42?"))
	assert bot.texts == ["42"]


async def test_attachment_and_platform_filters(dp):
	max_bot, tg_bot = FakeBot(platform=MAX), FakeBot(platform=TELEGRAM)

	@dp.message(HasAttachment("image"), OnPlatform(TELEGRAM))
	async def photo(ctx):
		await ctx.reply("tg photo")

	@dp.message(HasAttachment())
	async def any_file(ctx):
		await ctx.reply("file")

	image = [Attachment("image", "t1", "a.jpg")]
	await dp.feed(tg_bot.message(attachments=image))
	await dp.feed(max_bot.message(attachments=image))
	await dp.feed(max_bot.message("no files"))
	assert tg_bot.texts == ["tg photo"] and max_bot.texts == ["file"]


# ---------------------------------------------------------------- middleware, errors, context
async def test_middleware_order_data_and_short_circuit(dp, bot):
	trace = []

	@dp.middleware
	async def outer(ctx, call_next):
		trace.append("outer>")
		ctx.data["who"] = f"user{ctx.user.id}"
		await call_next()
		trace.append("<outer")

	@dp.middleware
	async def ban(ctx, call_next):
		if ctx.user.id == 666:
			trace.append("banned")
			return
		await call_next()

	@dp.message()
	async def h(ctx):
		trace.append(ctx.data["who"])

	assert await dp.feed(bot.message("x", user_id=1))
	assert not await dp.feed(bot.message("x", user_id=666))
	assert trace == ["outer>", "user1", "<outer", "outer>", "banned", "<outer"]


async def test_errors_go_to_the_error_handler_and_state_is_still_saved(dp, bot):
	errors = []

	@dp.on_error
	async def on_error(ctx, exc):
		errors.append(str(exc))
		await ctx.answer("Something went wrong", alert=True)

	@dp.raw_callback()
	async def boom(ctx):
		ctx.state.set(Form.name)
		raise ValueError("boom")

	await dp.feed(bot.press("x"))
	assert errors == ["boom"]
	assert bot.answers[0].text == "Something went wrong" and len(bot.answers) == 1
	assert (await dp.storage.get(StorageKey(MAX, 1))).state == "Form:name"  # type: ignore[union-attr]


async def test_errors_without_a_handler_are_logged(dp, bot, caplog):
	@dp.message()
	async def boom(ctx):
		raise ValueError("boom")

	await dp.feed(bot.message("x"))
	assert "Unhandled error" in caplog.text


async def test_custom_context_class(bot):
	class Ctx(Context):
		account: str | None = None

	dp = Dispatcher(registry=reg, context_class=Ctx)

	@dp.middleware
	async def load(ctx: Ctx, call_next):
		ctx.account = "acc-1"
		await call_next()

	@dp.message()
	async def h(ctx: Ctx):
		await ctx.reply(f"{type(ctx).__name__} {ctx.account}")

	await dp.feed(bot.message("x"))
	assert bot.texts == ["Ctx acc-1"]


async def test_chat_status_handler(dp, bot):
	seen = []

	@dp.chat_status()
	async def status(ctx):
		seen.append((ctx.user.id, ctx.chat_status.reachable))

	await dp.feed(bot.blocked(user_id=3))
	await dp.feed(bot.blocked(user_id=3, reachable=True))
	assert seen == [(3, False), (3, True)]


async def test_delete_and_documents(dp, bot):
	@dp.message(Text("report"))
	async def report(ctx):
		await ctx.delete()
		await ctx.reply_document(b"data", "report.csv", caption="here")

	incoming = bot.message("report")
	await dp.feed(incoming)
	assert bot.deleted == [incoming.message_id]
	assert bot.sent[0].document == ("report.csv", b"data") and bot.sent[0].text == "here"


# ---------------------------------------------------------------- routers
async def test_included_routers_in_order(dp, bot):
	first, second, nested = Router("first"), Router("second"), Router("nested")

	@second.message()
	async def from_second(ctx):
		await ctx.reply("second")

	@nested.message()
	async def from_nested(ctx):
		await ctx.reply("nested")

	first.include(nested)
	dp.include(first, second)
	await dp.feed(bot.message("x"))
	assert bot.texts == ["nested"]


def test_router_can_be_included_once(dp):
	router = Router("r")
	dp.include(router)
	with pytest.raises(ValueError, match="already included"):
		Dispatcher(registry=reg).include(router)
	with pytest.raises(ValueError):
		dp.include(dp)


async def test_registration_after_start_is_an_error(dp, bot):
	router = Router("late")
	dp.include(router)
	await dp.feed(bot.message("x"))
	with pytest.raises(RuntimeError, match="already in use"):
		router.message()
	with pytest.raises(RuntimeError):
		dp.include(Router())


# ---------------------------------------------------------------- polling
async def test_polling_keeps_each_persons_events_in_order(dp):
	bot = FakeBot()
	order = []
	release = asyncio.Event()

	@dp.message(Text("slow"))
	async def slow(ctx):
		await release.wait()
		order.append(f"{ctx.user.id}:slow")

	@dp.message()
	async def fast(ctx):
		order.append(f"{ctx.user.id}:{ctx.text}")

	bot.push(bot.message("slow", user_id=1), bot.message("after", user_id=1), bot.message("other", user_id=2))
	task = asyncio.create_task(dp.start_polling(bot, timeout=1))
	for _ in range(50):
		await asyncio.sleep(0)
	assert order == ["2:other"]  # person 2 isn't held up by person 1's slow handler
	release.set()
	for _ in range(50):
		await asyncio.sleep(0)
	assert order == ["2:other", "1:slow", "1:after"]
	task.cancel()
	with pytest.raises(asyncio.CancelledError):
		await task


async def test_polling_retries_after_failures(dp, monkeypatch):
	bot = FakeBot()
	sleeps = []
	calls = 0
	real_get_me = bot.get_me

	async def flaky_get_me():
		nonlocal calls
		calls += 1
		if calls < 3:
			raise ConnectionError("down")
		return await real_get_me()

	async def fake_sleep(delay):
		sleeps.append(delay)

	monkeypatch.setattr(bot, "get_me", flaky_get_me)
	monkeypatch.setattr("matele.dispatch.polling._sleep", fake_sleep)

	@dp.message()
	async def h(ctx):
		await ctx.reply("ok")

	bot.push(bot.message("x"))
	task = asyncio.create_task(dp.start_polling(bot, timeout=1))
	for _ in range(50):
		await asyncio.sleep(0)
	task.cancel()
	with pytest.raises(asyncio.CancelledError):
		await task
	assert sleeps == [1.0, 2.0] and bot.texts == ["ok"]


async def test_polling_lets_running_handlers_finish_on_stop(dp):
	bot = FakeBot()
	started, finished = asyncio.Event(), []

	@dp.message()
	async def slow(ctx):
		started.set()
		await asyncio.sleep(0.05)
		finished.append(ctx.text)

	bot.push(bot.message("order"))
	task = asyncio.create_task(dp.start_polling(bot, timeout=1))
	await started.wait()
	task.cancel()
	await asyncio.gather(task, return_exceptions=True)
	assert finished == ["order"]


async def test_polling_cancels_handlers_that_outlast_the_shutdown_timeout(dp):
	bot = FakeBot()
	started, cancelled = asyncio.Event(), []

	@dp.message()
	async def stuck(ctx):
		started.set()
		try:
			await asyncio.sleep(60)
		except asyncio.CancelledError:
			cancelled.append(ctx.text)
			raise

	bot.push(bot.message("order"))
	task = asyncio.create_task(dp.start_polling(bot, timeout=1, shutdown_timeout=0.01))
	await started.wait()
	task.cancel()
	await asyncio.gather(task, return_exceptions=True)
	assert cancelled == ["order"]


async def test_polling_handles_a_bounded_number_of_events_at_once(dp):
	bot = FakeBot()
	now, most, done = 0, 0, 0

	@dp.message()
	async def slow(ctx):
		nonlocal now, most, done
		now += 1
		most = max(most, now)
		await asyncio.sleep(0.01)
		now -= 1
		done += 1

	for user_id in range(1, 9):
		bot.push(bot.message("hi", user_id=user_id))
	task = asyncio.create_task(dp.start_polling(bot, timeout=1, concurrency=3))
	while done < 8:
		await asyncio.sleep(0.01)
	task.cancel()
	await asyncio.gather(task, return_exceptions=True)
	assert most == 3


async def test_at_least_once_asks_for_the_next_batch_only_when_this_one_is_handled(dp):
	"""Asking for the next batch is what tells the messenger the current one
	arrived: it must not happen while a handler is still running."""

	class Recording(FakeBot):
		def __init__(self) -> None:
			super().__init__()
			self.log: list[str] = []

		async def get_events(self, timeout: int = 30):
			self.log.append("ask")
			return await super().get_events(timeout)

	bot = Recording()
	release = asyncio.Event()

	@dp.message()
	async def slow(ctx):
		bot.log.append(f"start {ctx.text}")
		await release.wait()
		bot.log.append(f"done {ctx.text}")

	bot.push(bot.message("a"))
	task = asyncio.create_task(dp.start_polling(bot, timeout=1, at_least_once=True))
	while "start a" not in bot.log:
		await asyncio.sleep(0)
	for _ in range(20):
		await asyncio.sleep(0)
	assert bot.log == ["ask", "start a"]  # not asking again yet
	release.set()
	while bot.log.count("ask") < 2:
		await asyncio.sleep(0)
	assert bot.log == ["ask", "start a", "done a", "ask"]
	task.cancel()
	await asyncio.gather(task, return_exceptions=True)
