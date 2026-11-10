"""Where conversation state lives between events.

A storage keeps one StateRecord per StorageKey. The dispatcher loads the
record before routing an event and saves it afterwards only if it changed,
so a handler works with plain synchronous attributes (`ctx.state`).

Records must be JSON-serialisable (ids, strings, numbers, lists, small
dicts) if the storage persists them.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, NamedTuple, Protocol


class StorageKey(NamedTuple):
	platform: str
	user_id: int
	# None: the person's private dialog with the bot. Group chats will key
	# state per (chat, person).
	chat_id: int | None = None


@dataclass
class StateRecord:
	state: str | None = None
	data: dict[str, Any] = field(default_factory=dict)
	# Library bookkeeping (e.g. the id of a UI message) that outlives
	# `ctx.state.clear()`.
	meta: dict[str, Any] = field(default_factory=dict)

	@property
	def empty(self) -> bool:
		return self.state is None and not self.data and not self.meta


class Storage(Protocol):
	async def get(self, key: StorageKey) -> StateRecord | None: ...

	async def set(self, key: StorageKey, record: StateRecord) -> None: ...

	async def delete(self, key: StorageKey) -> None: ...

	async def close(self) -> None: ...


# A storage shared by several processes may also have
#
# 	def lock(self, key: StorageKey) -> AbstractAsyncContextManager[None]
#
# held while an event of that conversation is handled (see RedisStorage).


class MemoryStorage:
	"""In-process dict: fast, lost on restart, not shared between processes."""

	def __init__(self) -> None:
		self._records: dict[StorageKey, StateRecord] = {}

	def __len__(self) -> int:
		return len(self._records)

	async def get(self, key: StorageKey) -> StateRecord | None:
		record = self._records.get(key)
		return copy.deepcopy(record) if record is not None else None

	async def set(self, key: StorageKey, record: StateRecord) -> None:
		self._records[key] = copy.deepcopy(record)

	async def delete(self, key: StorageKey) -> None:
		self._records.pop(key, None)

	async def close(self) -> None:
		pass
