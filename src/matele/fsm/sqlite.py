"""SQLiteStorage: conversation state in a SQLite file (standard library only).

	storage = SQLiteStorage("bot.db")
	dp = Dispatcher(storage=storage)

	# the same file can keep Telegram's long-button aliases across restarts
	tg = TelegramBot(token, aliases=CallbackAliases(storage.aliases()))

Good for a single bot process. Calls run in a worker thread so they don't
block the event loop; the database is in WAL mode. For several processes
or machines sharing state, use RedisStorage.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from matele.fsm.storage import StateRecord, StorageKey

_SCHEMA = """
CREATE TABLE IF NOT EXISTS matele_state (
	platform TEXT NOT NULL,
	user_id INTEGER NOT NULL,
	chat_id INTEGER NOT NULL,  -- 0: the person's private chat
	state TEXT,
	data TEXT NOT NULL,
	meta TEXT NOT NULL,
	updated_at REAL NOT NULL,
	PRIMARY KEY (platform, user_id, chat_id)
);
CREATE TABLE IF NOT EXISTS matele_callback_aliases (
	alias TEXT PRIMARY KEY,
	data TEXT NOT NULL,
	saved_at REAL NOT NULL
);
"""


class SQLiteStorage:
	def __init__(self, path: str | Path):
		self.path = str(path)
		self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
		self._lock = threading.Lock()
		with self._lock:
			self._conn.execute("PRAGMA journal_mode=WAL")
			self._conn.execute("PRAGMA synchronous=NORMAL")
			self._conn.executescript(_SCHEMA)

	async def _run(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
		def work() -> list[tuple[Any, ...]]:
			with self._lock:
				return self._conn.execute(sql, params).fetchall()

		return await asyncio.to_thread(work)

	async def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
		"""Runs a statement; returns the number of rows it changed."""

		def work() -> int:
			with self._lock:
				return self._conn.execute(sql, params).rowcount

		return await asyncio.to_thread(work)

	async def get(self, key: StorageKey) -> StateRecord | None:
		rows = await self._run(
			"SELECT state, data, meta FROM matele_state WHERE platform = ? AND user_id = ? AND chat_id = ?",
			_key(key),
		)
		if not rows:
			return None
		state, data, meta = rows[0]
		return StateRecord(state, json.loads(data), json.loads(meta))

	async def set(self, key: StorageKey, record: StateRecord) -> None:
		data, meta = json.dumps(record.data, ensure_ascii=False), json.dumps(record.meta, ensure_ascii=False)
		await self._run(
			"INSERT INTO matele_state (platform, user_id, chat_id, state, data, meta, updated_at)"
			" VALUES (?, ?, ?, ?, ?, ?, ?)"
			" ON CONFLICT (platform, user_id, chat_id)"
			" DO UPDATE SET state = excluded.state, data = excluded.data, meta = excluded.meta,"
			" updated_at = excluded.updated_at",
			(*_key(key), record.state, data, meta, time.time()),
		)

	async def delete(self, key: StorageKey) -> None:
		await self._run("DELETE FROM matele_state WHERE platform = ? AND user_id = ? AND chat_id = ?", _key(key))

	async def close(self) -> None:
		def work() -> None:
			with self._lock:
				self._conn.close()

		await asyncio.to_thread(work)

	def aliases(self) -> SQLiteAliasStore:
		"""A Telegram AliasStore in the same database file."""
		return SQLiteAliasStore(self)


class SQLiteAliasStore:
	"""Keeps Telegram's long-callback aliases (see matele.telegram.CallbackAliases)."""

	def __init__(self, storage: SQLiteStorage):
		self._storage = storage

	async def save(self, alias: str, data: str) -> None:
		await self._storage._run(
			"INSERT INTO matele_callback_aliases (alias, data, saved_at) VALUES (?, ?, ?)"
			" ON CONFLICT (alias) DO UPDATE SET data = excluded.data, saved_at = excluded.saved_at",
			(alias, data, time.time()),
		)

	async def get(self, alias: str) -> str | None:
		rows = await self._storage._run("SELECT data FROM matele_callback_aliases WHERE alias = ?", (alias,))
		return rows[0][0] if rows else None

	async def purge(self, older_than_days: int) -> int:
		cutoff = time.time() - older_than_days * 86400
		return await self._storage._execute("DELETE FROM matele_callback_aliases WHERE saved_at < ?", (cutoff,))


def _key(key: StorageKey) -> tuple[str, int, int]:
	return key.platform, key.user_id, key.chat_id if key.chat_id is not None else 0
