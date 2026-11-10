"""RedisStorage: conversation state in Redis, shared by any number of bot
processes (pip install matele[redis]).

	storage = RedisStorage.from_url("redis://localhost:6379/0", ttl=7 * 86400)
	dp = Dispatcher(storage=storage)

Each conversation is one JSON value under "<prefix>:<platform>:<user>:<chat>".
With `ttl`, a conversation whose state hasn't changed for that many
seconds expires (reading it doesn't extend it).

Processes sharing the storage also share a lock per conversation
("<key>:lock"), taken while an event of that person is handled: two
processes getting two events of one person handle them one after the
other, instead of both reading the same state and one overwriting what
the other saved. A lock is given up after `lock_ttl` seconds, so a
process that died doesn't block the person for good; a handler running
longer than that loses the protection.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from typing import Any, Protocol

from matele.fsm.storage import StateRecord, StorageKey


class RedisLike(Protocol):
	"""The part of redis.asyncio.Redis this storage uses."""

	def get(self, name: str) -> Awaitable[Any]: ...

	def set(
		self, name: str, value: str, ex: int | None = None, px: int | None = None, nx: bool = False
	) -> Awaitable[Any]: ...

	def eval(self, script: str, numkeys: int, *keys_and_args: str) -> Awaitable[Any]: ...

	def delete(self, *names: str) -> Awaitable[Any]: ...

	def aclose(self) -> Awaitable[Any]: ...


LOCK_TTL = 60.0
LOCK_POLL = 0.05  # seconds between attempts to take a held lock
# Deletes the lock only if it is still ours (it may have expired and been taken).
_RELEASE = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"


class RedisStorage:
	def __init__(
		self,
		redis: RedisLike,
		*,
		prefix: str = "matele",
		ttl: int | None = None,
		lock_ttl: float = LOCK_TTL,
		close_client: bool = False,
	):
		"""`redis`: a redis.asyncio.Redis (or compatible) client.
		`lock_ttl`: seconds after which a held conversation lock is given up."""
		self.redis = redis
		self.prefix = prefix
		self.ttl = ttl
		self.lock_ttl = lock_ttl
		self._close_client = close_client

	@classmethod
	def from_url(
		cls, url: str, *, prefix: str = "matele", ttl: int | None = None, lock_ttl: float = LOCK_TTL
	) -> RedisStorage:
		try:
			from redis.asyncio import Redis
		except ImportError:
			raise RuntimeError("RedisStorage needs the redis package: pip install matele[redis]") from None
		return cls(Redis.from_url(url), prefix=prefix, ttl=ttl, lock_ttl=lock_ttl, close_client=True)

	def key(self, key: StorageKey) -> str:
		chat = key.chat_id if key.chat_id is not None else 0
		return f"{self.prefix}:{key.platform}:{key.user_id}:{chat}"

	async def get(self, key: StorageKey) -> StateRecord | None:
		raw = await self.redis.get(self.key(key))
		if raw is None:
			return None
		value = json.loads(raw)
		return StateRecord(value.get("state"), value.get("data") or {}, value.get("meta") or {})

	async def set(self, key: StorageKey, record: StateRecord) -> None:
		value = json.dumps({"state": record.state, "data": record.data, "meta": record.meta}, ensure_ascii=False)
		await self.redis.set(self.key(key), value, ex=self.ttl)

	async def delete(self, key: StorageKey) -> None:
		await self.redis.delete(self.key(key))

	@asynccontextmanager
	async def lock(self, key: StorageKey) -> AsyncIterator[None]:
		"""Holds the conversation's lock across processes (polling and
		Webhook take it around each event)."""
		name, token = self.key(key) + ":lock", secrets.token_hex(16)
		while not await self.redis.set(name, token, nx=True, px=int(self.lock_ttl * 1000)):
			await asyncio.sleep(LOCK_POLL)
		try:
			yield
		finally:
			await self.redis.eval(_RELEASE, 1, name, token)

	async def close(self) -> None:
		if self._close_client:
			await self.redis.aclose()
