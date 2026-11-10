from __future__ import annotations

import asyncio
from typing import Any

import pytest

from matele import Context, Dispatcher, State, StorageKey
from matele.callbacks import CallbackRegistry
from matele.fsm import MemoryStorage, RedisStorage, SQLiteStorage, StateRecord
from matele.telegram import CallbackAliases
from matele.testing import FakeBot


class FakeRedis:
	def __init__(self) -> None:
		self.values: dict[str, str] = {}
		self.expiry: dict[str, int | None] = {}
		self.closed = False

	async def get(self, name: str) -> Any:
		value = self.values.get(name)
		return value.encode() if value is not None else None  # redis returns bytes

	async def set(self, name: str, value: str, ex: int | None = None, px: int | None = None, nx: bool = False) -> Any:
		if nx and name in self.values:
			return None
		self.values[name], self.expiry[name] = value, ex
		return True

	async def eval(self, script: str, numkeys: int, *keys_and_args: str) -> Any:
		name, token = keys_and_args  # the release script: delete if still ours
		if self.values.get(name) == token:
			del self.values[name]
			return 1
		return 0

	async def delete(self, *names: str) -> Any:
		return sum(self.values.pop(n, None) is not None for n in names)

	async def aclose(self) -> Any:
		self.closed = True


@pytest.fixture(params=["memory", "sqlite", "redis"])
def storage(request, tmp_path):
	if request.param == "memory":
		return MemoryStorage()
	if request.param == "sqlite":
		return SQLiteStorage(tmp_path / "state.db")
	return RedisStorage(FakeRedis())


PRIVATE_KEY = StorageKey("max", 7)
GROUP_KEY = StorageKey("max", 7, -100)


async def test_storage_contract(storage):
	assert await storage.get(PRIVATE_KEY) is None
	record = StateRecord("Form:name", {"name": "Аня", "items": [1, 2], "nested": {"a": None}}, {"panel": "m1"})
	await storage.set(PRIVATE_KEY, record)
	assert await storage.get(PRIVATE_KEY) == record
	assert await storage.get(GROUP_KEY) is None  # private and group state are separate
	assert await storage.get(StorageKey("telegram", 7)) is None  # and per messenger
	await storage.set(PRIVATE_KEY, StateRecord(None, {"x": 1}))  # overwrite
	assert await storage.get(PRIVATE_KEY) == StateRecord(None, {"x": 1})
	await storage.delete(PRIVATE_KEY)
	await storage.delete(PRIVATE_KEY)  # deleting twice is fine
	assert await storage.get(PRIVATE_KEY) is None
	await storage.close()


async def test_sqlite_survives_a_restart(tmp_path):
	path = tmp_path / "bot.db"
	ask = State("ask")

	def build(storage: SQLiteStorage) -> Dispatcher:
		dp = Dispatcher(storage=storage, registry=CallbackRegistry())

		@dp.command("ask")
		async def start(ctx: Context):
			ctx.state.set(ask)
			ctx.state.update(asked_at="now")

		@dp.message(state=ask)
		async def answered(ctx: Context):
			await ctx.reply(f"{ctx.text} ({ctx.state.get('asked_at')})")
			ctx.state.clear()

		return dp

	bot = FakeBot()
	first = SQLiteStorage(path)
	await build(first).feed(bot.message("/ask"))
	await first.close()

	second = SQLiteStorage(path)  # a new process, same file
	await build(second).feed(bot.message("42"))
	assert bot.texts == ["42 (now)"]
	assert await second.get(StorageKey(bot.platform, 1)) is None
	await second.close()


async def test_sqlite_alias_store(tmp_path, monkeypatch):
	storage = SQLiteStorage(tmp_path / "bot.db")
	store = storage.aliases()
	aliases = CallbackAliases(store)
	alias = aliases.encode("report:" + "x" * 100)
	await aliases.flush()
	reopened = SQLiteStorage(tmp_path / "bot.db")
	assert await CallbackAliases(reopened.aliases()).decode(alias) == "report:" + "x" * 100
	await reopened.close()

	now = 1_000_000.0
	monkeypatch.setattr("matele.fsm.sqlite.time.time", lambda: now)
	await store.save("~old", "old data")
	now += 61 * 86400
	await store.save("~new", "new data")
	assert await store.purge(older_than_days=60) >= 1
	assert await store.get("~old") is None and await store.get("~new") == "new data"
	await storage.close()


async def test_redis_keys_ttl_and_close():
	fake = FakeRedis()
	storage = RedisStorage(fake, prefix="mybot", ttl=3600, close_client=True)
	await storage.set(GROUP_KEY, StateRecord("s"))
	await storage.set(PRIVATE_KEY, StateRecord("p"))
	assert set(fake.values) == {"mybot:max:7:-100", "mybot:max:7:0"}
	assert fake.expiry["mybot:max:7:-100"] == 3600
	await storage.close()
	assert fake.closed


def test_redis_from_url_builds_a_client():
	storage = RedisStorage.from_url("redis://localhost:6379/0", ttl=10)  # no connection until used
	assert storage.ttl == 10 and storage.key(PRIVATE_KEY) == "matele:max:7:0"


async def test_redis_lock_keeps_two_processes_from_overwriting_each_other():
	"""Two bot processes (two dispatchers, one Redis) get two events of one
	person at the same moment: both must count."""
	fake = FakeRedis()
	bot = FakeBot()

	def process() -> Dispatcher:
		dp = Dispatcher(storage=RedisStorage(fake), registry=CallbackRegistry())

		@dp.message()
		async def count(ctx: Context):
			seen = ctx.state.get("seen", 0)
			await asyncio.sleep(0.01)  # the other process gets in here
			ctx.state.update(seen=seen + 1)

		return dp

	async def handle(dp: Dispatcher) -> None:
		event = bot.message("hi", user_id=7)
		async with dp.exclusive(dp.storage_key(event)):
			await dp.feed(event)

	first, second = process(), process()
	await asyncio.gather(handle(first), handle(second))
	record = await first.storage.get(StorageKey("max", 7))
	assert record is not None and record.data == {"seen": 2}
	assert not any(name.endswith(":lock") for name in fake.values)  # released


async def test_events_are_still_handled_when_the_lock_cannot_be_taken():
	class DownRedis(FakeRedis):
		async def set(self, name, value, ex=None, px=None, nx=False):
			if nx:
				raise ConnectionError("redis is down")
			return await super().set(name, value, ex)

	dp = Dispatcher(storage=RedisStorage(DownRedis()), registry=CallbackRegistry())
	bot = FakeBot()

	@dp.message()
	async def hello(ctx: Context):
		await ctx.reply("hi")

	event = bot.message("hi")
	async with dp.exclusive(dp.storage_key(event)):
		assert await dp.feed(event) is True
	assert bot.sent[-1].text == "hi"
