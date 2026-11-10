from __future__ import annotations

import pytest

from matele import MAX, TELEGRAM, BotApiError, BotForbiddenError, BotUnavailableError, ConnectionGuard
from matele.hub import Hub, TokenBucket
from matele.testing import FakeBot


class FlakyBot(FakeBot):
	"""Fails for chosen users: `blocked` -> 403, `down` -> network error."""

	def __init__(self, *, platform: str, blocked: frozenset[int] = frozenset(), down: int = 0):
		super().__init__(platform=platform)
		self.blocked_users = set(blocked)
		self.down = down  # how many sends fail with a network error first
		self.guard = ConnectionGuard(platform, cooldown=5)

	async def send_message(self, text, *, chat_id=None, user_id=None, keyboard=None, reply_to=None):
		if user_id in self.blocked_users:
			raise BotForbiddenError(403, "", "bot was blocked by the user", platform=self.platform)
		if self.down:
			self.down -= 1
			self.guard.failed()
			raise BotUnavailableError(0, "network", "ConnectError", platform=self.platform)
		self.guard.ok()
		return await super().send_message(text, chat_id=chat_id, user_id=user_id, keyboard=keyboard, reply_to=reply_to)


@pytest.fixture
def sleeps(monkeypatch) -> list[float]:
	recorded: list[float] = []

	async def fake_sleep(delay: float) -> None:
		recorded.append(delay)

	monkeypatch.setattr("matele.hub._sleep", fake_sleep)
	return recorded


async def test_send_reaches_every_messenger_of_a_person():
	max_bot, tg_bot = FakeBot(platform=MAX), FakeBot(platform=TELEGRAM)
	hub = Hub(max_bot, tg_bot)
	deliveries = await hub.send({MAX: 1, TELEGRAM: 2, "icq": 3}, "Заказ готов")
	assert [(d.platform, d.user_id, d.ok) for d in deliveries] == [(MAX, 1, True), (TELEGRAM, 2, True)]
	assert max_bot.sent[0].user_id == 1 and tg_bot.sent[0].text == "Заказ готов"


async def test_one_messenger_failing_does_not_stop_the_other():
	unreachable = []

	async def mark(platform, user_id, exc):
		unreachable.append((platform, user_id))

	max_bot, tg_bot = FlakyBot(platform=MAX, blocked=frozenset({1})), FakeBot(platform=TELEGRAM)
	hub = Hub(max_bot, tg_bot, on_unreachable=mark)
	deliveries = await hub.send({MAX: 1, TELEGRAM: 2}, "hi")
	assert [d.ok for d in deliveries] == [False, True]
	assert isinstance(deliveries[0].error, BotForbiddenError)
	assert unreachable == [(MAX, 1)]


async def test_a_short_outage_is_waited_out(sleeps):
	bot = FlakyBot(platform=MAX, down=1)
	(delivery,) = await Hub(bot).send({MAX: 1}, "hi")
	assert delivery.ok and len(sleeps) == 1 and 0 < sleeps[0] <= 5


async def test_a_long_outage_fails_fast(sleeps):
	bot = FlakyBot(platform=MAX, down=10)
	(delivery,) = await Hub(bot).send({MAX: 1}, "hi")
	assert isinstance(delivery.error, BotUnavailableError)
	assert len(sleeps) == 2  # RETRY_ATTEMPTS, then give up


async def test_broadcast_report():
	max_bot = FlakyBot(platform=MAX, blocked=frozenset({2}))
	tg_bot = FakeBot(platform=TELEGRAM)
	hub = Hub(max_bot, tg_bot)

	def people():  # a generator: read lazily
		yield {MAX: 1}
		yield {MAX: 2}  # blocked
		yield {MAX: 3, TELEGRAM: 30}
		yield {TELEGRAM: 40}
		yield {MAX: 2, TELEGRAM: 20}  # blocked on MAX but reached on Telegram

	report = await hub.broadcast(people(), "Мы открылись!", concurrency=2)
	assert report.people == 5 and report.reached == 4
	assert report.delivered == 5 and report.failed == 2
	assert sorted(report.unreachable) == [(MAX, 2), (MAX, 2)]
	assert sorted(s.user_id or 0 for s in max_bot.sent) == [1, 3]


def test_rates_and_duplicates():
	hub = Hub(FakeBot(platform=MAX), FakeBot(platform=TELEGRAM), rate={MAX: 5})
	assert hub._buckets[MAX].rate == 5 and hub._buckets[TELEGRAM].rate == 25
	with pytest.raises(ValueError):
		Hub(FakeBot(platform=MAX), FakeBot(platform=MAX))
	assert hub.get(MAX) is not None and hub.get("icq") is None


async def test_token_bucket_paces_calls(sleeps, monkeypatch):
	clock = [100.0]
	monkeypatch.setattr("matele.hub.time.monotonic", lambda: clock[0])

	async def advance(delay: float) -> None:
		sleeps.append(delay)
		clock[0] += delay

	monkeypatch.setattr("matele.hub._sleep", advance)
	bucket = TokenBucket(rate=2)
	for _ in range(6):
		await bucket.acquire()
	# 2 at once (one second's burst), then one every half second
	assert sum(sleeps) == pytest.approx(2.0)


async def test_unreachable_hook_errors_are_logged_not_raised(caplog):
	async def broken(platform, user_id, exc):
		raise RuntimeError("db down")

	(delivery,) = await Hub(FlakyBot(platform=MAX, blocked=frozenset({1})), on_unreachable=broken).send({MAX: 1}, "x")
	assert not delivery.ok and isinstance(delivery.error, BotApiError)
	assert "on_unreachable failed" in caplog.text


async def test_a_broadcast_report_keeps_outcomes_not_messages():
	hub = Hub(FakeBot(platform=MAX))
	report = await hub.broadcast([{MAX: 1}, {MAX: 2}], "Мы открылись!")
	assert report.delivered == 2 and all(d.message is None for d in report.deliveries)
	(delivery,) = await hub.send({MAX: 3}, "Заказ готов")
	assert delivery.message is not None  # a single send still returns what was sent


async def test_on_sent_lets_a_broadcast_go_on_after_a_restart():
	people = [{MAX: 1}, {MAX: 2}, {MAX: 3}]
	done: set[int] = set()

	async def mark(accounts, deliveries) -> None:
		if accounts[MAX] == 2:
			raise RuntimeError("database hiccup")  # logged, the broadcast goes on
		done.add(accounts[MAX])

	first = FakeBot(platform=MAX)
	await Hub(first).broadcast(people, "Мы открылись!", concurrency=1, on_sent=mark)
	assert done == {1, 3} and len(first.sent) == 3

	second = FakeBot(platform=MAX)  # after a restart: only those not marked yet
	report = await Hub(second).broadcast((p for p in people if p[MAX] not in done), "Мы открылись!")
	assert report.people == 1 and [s.user_id for s in second.sent] == [2]
