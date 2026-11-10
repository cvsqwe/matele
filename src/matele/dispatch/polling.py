"""Long polling: one loop per messenger, independent of each other. A
messenger whose API is down (or whose token is wrong) keeps retrying with
backoff while the other carries on.

Events run as tasks, so one slow handler doesn't stall polling, while a
per-person lock keeps each person's own events in order.

A messenger takes a batch of events for received when the next one is
asked for. By default that happens while the batch's handlers may still
be running, so delivery is at most once: on a stop the running handlers
are given time to finish, and an event is lost only if the process is
killed in the middle of handling it.

With `at_least_once=True` the next batch is asked for only after every
handler of the current one has finished. An event whose handling was cut
short then comes again after a restart, and so may events of the same
batch that were already handled: handlers must cope with a repeat (check
"is this order already paid?" before paying). The price is speed: one
slow handler holds up that messenger's next batch.
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
SHUTDOWN_TIMEOUT = 10.0  # the Dispatcher has the same default
CONCURRENCY = 100  # events of one messenger handled at once
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


async def poll(
	dp: Dispatcher,
	client: BotClient,
	*,
	timeout: int = 30,
	max_backoff: float = MAX_BACKOFF,
	shutdown_timeout: float = SHUTDOWN_TIMEOUT,
	concurrency: int = CONCURRENCY,
	at_least_once: bool = False,
) -> None:
	"""Long-polls one messenger until cancelled; never raises otherwise.
	On cancel, events still being handled get `shutdown_timeout` seconds
	to finish; the rest are cancelled (and so is everything on a second
	cancel).

	At most `concurrency` events are handled at once. When handlers can't
	keep up, polling waits for a free slot instead of piling up tasks: the
	messenger keeps the events that weren't asked for yet.

	`at_least_once`: ask for the next batch only when the current one is
	fully handled, so events cut short by a crash come again (see the
	module docstring)."""
	log = logging.getLogger(f"matele.polling.{client.platform}")
	locks = KeyedLocks()
	running: set[asyncio.Task[None]] = set()
	slots = asyncio.Semaphore(max(concurrency, 1))

	async def handle(event: IncomingEvent) -> None:
		try:
			key = dp.storage_key(event)
			async with locks.hold(key), dp.exclusive(key):
				await dp.feed(event)
		finally:
			slots.release()

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
			batch: list[asyncio.Task[None]] = []
			for event in events:
				await slots.acquire()
				task = asyncio.create_task(handle(event))
				running.add(task)
				task.add_done_callback(running.discard)
				batch.append(task)
			if at_least_once and batch:
				await asyncio.wait(batch)
			# Let the handlers run even if the transport answered without
			# really waiting (a proxy that ignores long polling, a test fake).
			await asyncio.sleep(0)
	finally:
		await drain(running, shutdown_timeout)


async def drain(running: set[asyncio.Task[None]], timeout: float) -> None:
	"""Waits up to `timeout` seconds for the tasks, then cancels the rest."""
	if not running:
		return
	pending = set(running)
	try:
		if timeout > 0:
			_, pending = await asyncio.wait(pending, timeout=timeout)
	finally:  # also when cancelled again while waiting
		for task in pending:
			task.cancel()
		if pending:
			await asyncio.gather(*pending, return_exceptions=True)
