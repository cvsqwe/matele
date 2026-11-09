"""RedisStorage: conversation state in Redis, shared by any number of bot
processes (pip install matele[redis]).

	storage = RedisStorage.from_url("redis://localhost:6379/0", ttl=7 * 86400)
	dp = Dispatcher(storage=storage)

Each conversation is one JSON value under "<prefix>:<platform>:<user>:<chat>".
With `ttl`, a conversation nobody touched for that many seconds expires.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable
from typing import Any, Protocol

from matele.fsm.storage import StateRecord, StorageKey


class RedisLike(Protocol):
	"""The part of redis.asyncio.Redis this storage uses."""

	def get(self, name: str) -> Awaitable[Any]: ...

	def set(self, name: str, value: str, ex: int | None = None) -> Awaitable[Any]: ...

	def delete(self, *names: str) -> Awaitable[Any]: ...

	def aclose(self) -> Awaitable[Any]: ...


class RedisStorage:
	def __init__(self, redis: RedisLike, *, prefix: str = "matele", ttl: int | None = None, close_client: bool = False):
		"""`redis`: a redis.asyncio.Redis (or compatible) client."""
		self.redis = redis
		self.prefix = prefix
		self.ttl = ttl
		self._close_client = close_client

	@classmethod
	def from_url(cls, url: str, *, prefix: str = "matele", ttl: int | None = None) -> RedisStorage:
		try:
			from redis.asyncio import Redis
		except ImportError:
			raise RuntimeError("RedisStorage needs the redis package: pip install matele[redis]") from None
		return cls(Redis.from_url(url), prefix=prefix, ttl=ttl, close_client=True)

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

	async def close(self) -> None:
		if self._close_client:
			await self.redis.aclose()
