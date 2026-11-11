"""Hub: messages to a person on every messenger they use, and broadcasts.

	hub = Hub(max_bot, telegram_bot)

	# one person, known on both messengers
	await hub.send({"max": 123, "telegram": 456}, "Заказ готов")

	# many people, at a pace the messengers accept
	report = await hub.broadcast(subscribers, "Мы открылись!")
	print(report.delivered, report.failed, report.unreachable)

Every person is a mapping messenger -> user id (`{"max": 123}`). Delivery
on one messenger never waits for or fails because of another.

  • Each messenger gets at most `rate` messages per second (25 by default,
	under the ~30/s both MAX and Telegram allow a bot).
  • Flood control (429) and gateway errors are retried by the API clients;
	while a messenger asks to slow down, all sends to it pause.
  • When a messenger has just become unreachable, a send waits for it a
	little and retries; if it stays down, sends fail at once instead of
	holding up a broadcast. A send that may have reached the messenger
	before the failure (a timeout waiting for the answer) is not retried:
	the person could get the message twice.
  • A person who blocked the bot (or never started it) is reported in
	`unreachable`, and `on_unreachable` is called, so you can stop writing
	to them.

The hub works anywhere: in handlers, a scheduler, a web panel. It doesn't
need a running Dispatcher.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import TypeVar

from matele.errors import BotApiError, BotUnavailableError, is_unreachable, platform_title
from matele.types import BotClient, Keyboard, Message

logger = logging.getLogger(__name__)

T = TypeVar("T")
Accounts = Mapping[str, int]  # messenger -> user id
UnreachableHook = Callable[[str, int, BotApiError], Awaitable[None]]
SentHook = Callable[[Accounts, list["Delivery"]], Awaitable[None]]

DEFAULT_RATE = 25.0  # messages per second per messenger
RETRY_ATTEMPTS = 2  # extra tries while a messenger is briefly unreachable
RETRY_MIN_WAIT = 1.0
RETRY_MAX_WAIT = 35.0
OUTAGE_TRIPS = 3  # network failures in a row = an outage: stop waiting

_sleep = asyncio.sleep  # patched in tests


class TokenBucket:
	"""At most `rate` calls per second, with bursts up to one second's worth."""

	def __init__(self, rate: float):
		self.rate = max(rate, 0.1)
		self.capacity = max(self.rate, 1.0)
		self.tokens = self.capacity
		self.updated = time.monotonic()
		self._lock = asyncio.Lock()

	async def acquire(self) -> None:
		async with self._lock:
			while True:
				now = time.monotonic()
				self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
				self.updated = now
				if self.tokens >= 1:
					self.tokens -= 1
					return
				await _sleep((1 - self.tokens) / self.rate)


@dataclass
class Delivery:
	"""The outcome on one messenger."""

	platform: str
	user_id: int
	message: Message | None = None
	error: BaseException | None = None

	@property
	def ok(self) -> bool:
		return self.error is None


@dataclass
class BroadcastReport:
	# One per person and messenger. Without the sent messages (`message` is
	# None) and without the errors' tracebacks (which hold the stack frames
	# and the HTTP responses in them), so a report on a million people
	# stays small.
	deliveries: list[Delivery] = field(default_factory=list)
	people: int = 0
	reached: int = 0  # people who got the message on at least one messenger

	@property
	def delivered(self) -> int:
		return sum(d.ok for d in self.deliveries)

	@property
	def failed(self) -> int:
		return sum(not d.ok for d in self.deliveries)

	@property
	def unreachable(self) -> list[tuple[str, int]]:
		"""(messenger, user id) of people who blocked the bot or never started it."""
		return [(d.platform, d.user_id) for d in self.deliveries if d.error is not None and is_unreachable(d.error)]


class Hub:
	def __init__(
		self,
		*bots: BotClient,
		rate: float | Mapping[str, float] = DEFAULT_RATE,
		on_unreachable: UnreachableHook | None = None,
	):
		"""`rate`: messages per second, for all messengers or per messenger
		({"max": 20, "telegram": 25}). If several processes send through the
		same bot, give each a share so the total stays under the limit."""
		self.bots: dict[str, BotClient] = {}
		self._buckets: dict[str, TokenBucket] = {}
		self.on_unreachable = on_unreachable
		for bot in bots:
			if bot.platform in self.bots:
				raise ValueError(f"two bots for {platform_title(bot.platform)}")
			self.bots[bot.platform] = bot
			per = rate.get(bot.platform, DEFAULT_RATE) if isinstance(rate, Mapping) else rate
			self._buckets[bot.platform] = TokenBucket(per)

	def get(self, platform: str) -> BotClient | None:
		return self.bots.get(platform)

	# ------------------------------------------------------------ one person
	async def send(self, accounts: Accounts, text: str, keyboard: Keyboard | None = None) -> list[Delivery]:
		"""Sends `text` to the person on every messenger they have an account
		on (and the hub has a bot for). Never raises: see each Delivery."""
		targets = [(p, uid) for p, uid in accounts.items() if p in self.bots]
		return list(await asyncio.gather(*(self._send_one(p, uid, text, keyboard) for p, uid in targets)))

	async def _send_one(self, platform: str, user_id: int, text: str, keyboard: Keyboard | None) -> Delivery:
		bot = self.bots[platform]
		try:
			message = await self._deliver(
				platform, user_id, lambda: bot.send_message(text, user_id=user_id, keyboard=keyboard)
			)
		except Exception as exc:
			logger.warning("%s: could not deliver to %s: %s", bot.title, user_id, exc)
			return Delivery(platform, user_id, error=exc)
		return Delivery(platform, user_id, message=message)

	async def _deliver(self, platform: str, user_id: int, send: Callable[[], Awaitable[T]]) -> T:
		bot = self.bots[platform]
		attempt = 0
		while True:
			try:
				await self._buckets[platform].acquire()
				return await send()
			except BotUnavailableError as exc:
				if exc.maybe_sent:  # it may have arrived: a second try could deliver it twice
					raise
				guard = getattr(bot, "guard", None)
				if guard is None or attempt >= RETRY_ATTEMPTS or guard.trips >= OUTAGE_TRIPS:
					raise
				attempt += 1
				wait = min(max(guard.retry_in(), RETRY_MIN_WAIT), RETRY_MAX_WAIT)
				logger.info("%s unreachable, retrying delivery to %s in %.0fs", bot.title, user_id, wait)
				await _sleep(wait)
			except BotApiError as exc:
				if is_unreachable(exc) and self.on_unreachable is not None:
					try:
						await self.on_unreachable(platform, user_id, exc)
					except Exception:
						logger.exception("on_unreachable failed")
				raise

	# ------------------------------------------------------------ many people
	async def broadcast(
		self,
		people: Iterable[Accounts],
		text: str,
		keyboard: Keyboard | None = None,
		*,
		concurrency: int = 10,
		on_sent: SentHook | None = None,
	) -> BroadcastReport:
		"""Sends to everyone; the pace is set by the per-messenger rate,
		`concurrency` only bounds how many people are being sent to at once.
		`people` is read lazily, so a generator over a database works.

		`on_sent(accounts, deliveries)` is awaited after each person. Mark
		the person as done there (in your database), and a broadcast cut
		short by a restart can go on from where it stopped: run it again
		over the people not marked yet. A failing hook is logged and doesn't
		stop the broadcast."""
		report = BroadcastReport()
		queue = iter(people)

		async def worker() -> None:
			for accounts in queue:  # workers share one iterator: each person once
				deliveries = await self.send(accounts, text, keyboard)
				for delivery in deliveries:
					delivery.message = None  # the report keeps the outcome only
				report.deliveries.extend(deliveries)
				report.people += 1
				report.reached += any(d.ok for d in deliveries)
				if on_sent is not None:
					try:
						await on_sent(accounts, deliveries)
					except Exception:
						logger.exception("on_sent failed")
				for delivery in deliveries:  # after on_sent, which may still log them
					_drop_tracebacks(delivery.error)

		await asyncio.gather(*(worker() for _ in range(max(concurrency, 1))))
		return report


def _drop_tracebacks(exc: BaseException | None) -> None:
	"""Lets the stack frames of a failure (and of the failures that led to
	it) be freed, keeping the exception itself."""
	seen: set[int] = set()
	while exc is not None and id(exc) not in seen:
		seen.add(id(exc))
		exc.__traceback__ = None
		exc = exc.__cause__ or exc.__context__
