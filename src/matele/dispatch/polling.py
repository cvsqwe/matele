"""Long polling: one loop per messenger, independent of each other. A
messenger whose API is down (or whose token is wrong) keeps retrying with
backoff while the other carries on.

Events run as tasks, so one slow handler doesn't stall polling, while a
per-person lock keeps each person's own events in order.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Hashable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from matele.types import BotClient, IncomingEvent

if TYPE_CHECKING:
	from matele.dispatch.dispatcher import Dispatcher

logger = logging.getLogger(__name__)

MAX_BACKOFF = 60.0
_sleep = asyncio.sleep  # patched in tests


class KeyedLocks:
	"""One lock per key, dropped as soon as nobody holds or awaits it, so
	per-person locks don't pile up forever."""

	def __init__(self) -> None:
		self._locks: dict[Hashable, tuple[asyncio.Lock, int]] = {}

	def __len__(self) -> int:
		return len(self._locks)

	@asynccontextmanager
	async def hold(self, key: Hashable) -> AsyncIterator[None]:
		lock, users = self._locks.get(key, (None, 0))
		if lock is None:
			lock = asyncio.Lock()
		self._locks[key] = (lock, users + 1)
		try:
			async with lock:
				yield
		finally:
			lock, users = self._locks[key]
			if users <= 1:
				del self._locks[key]
			else:
				self._locks[key] = (lock, users - 1)


async def poll(dp: Dispatcher, client: BotClient, *, timeout: int = 30, max_backoff: float = MAX_BACKOFF) -> None:
	"""Long-polls one messenger until cancelled; never raises otherwise.
	On cancel, events still being handled are cancelled too."""
	log = logging.getLogger(f"matele.polling.{client.platform}")
	locks = KeyedLocks()
	running: set[asyncio.Task[None]] = set()

	async def handle(event: IncomingEvent) -> None:
		async with locks.hold(dp.storage_key(event)):
			await dp.feed(event)

	backoff, connected = 1.0, False
	try:
		while True:
			try:
				if not connected:
					me = await client.get_me()
					log.info("%s: polling as @%s (id %s)", client.title, me.username, me.id)
					connected = True
					await dp.publish_commands(client)
				events = await client.get_events(timeout=timeout)
			except asyncio.CancelledError:
				raise
			except Exception as exc:  # network, auth, API errors alike: retry
				log.warning("%s: polling failed (%s), retrying in %.0fs", client.title, exc, backoff)
				await _sleep(backoff)
				backoff = min(backoff * 2, max_backoff)
				continue
			backoff = 1.0
			for event in events:
				task = asyncio.create_task(handle(event))
				running.add(task)
				task.add_done_callback(running.discard)
			# Let the handlers run even if the transport answered without
			# really waiting (a proxy that ignores long polling, a test fake).
			await asyncio.sleep(0)
	finally:
		for task in running:
			task.cancel()
		if running:
			await asyncio.gather(*running, return_exceptions=True)
