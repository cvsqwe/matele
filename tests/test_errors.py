from __future__ import annotations

import pytest

from matele import MAX, TELEGRAM
from matele.errors import BotApiError, BotForbiddenError, BotUnavailableError, ConnectionGuard, is_unreachable


def test_unreachable_errors_are_recognised():
	assert is_unreachable(BotForbiddenError(403, "", "Forbidden: bot was blocked by the user", platform=TELEGRAM))
	assert is_unreachable(BotApiError(400, "sendMessage", "Bad Request: chat not found", platform=TELEGRAM))
	assert is_unreachable(BotApiError(404, "chat.not.found", "Chat 5 not found", platform=MAX))
	assert not is_unreachable(BotApiError(429, "sendMessage", "Too Many Requests", platform=TELEGRAM))
	assert not is_unreachable(BotApiError(400, "sendMessage", "Bad Request: can't parse entities", platform=TELEGRAM))
	assert not is_unreachable(BotUnavailableError(0, "network", "ConnectError", platform=TELEGRAM))
	assert not is_unreachable(ValueError("blocked"))


def test_error_text_names_the_messenger():
	assert str(BotApiError(400, "x", "boom", platform=MAX)) == "MAX API error 400 [x]: boom"
	assert str(BotApiError(400, "x", "boom")) == "Bot API error 400 [x]: boom"


class Clock:
	def __init__(self) -> None:
		self.now = 1000.0

	def __call__(self) -> float:
		return self.now


@pytest.fixture
def clock(monkeypatch):
	c = Clock()
	monkeypatch.setattr("matele.errors.time.monotonic", c)
	return c


def test_guard_fails_fast_during_cooldown_and_reopens(clock):
	guard = ConnectionGuard(MAX, cooldown=30)
	guard.check()  # closed: passes
	guard.failed()
	assert guard.trips == 1
	guard.check()  # one failure is not an outage yet
	guard.failed()
	with pytest.raises(BotUnavailableError):
		guard.check()
	clock.now += 10
	assert guard.retry_in() == pytest.approx(20)
	clock.now += 20
	guard.check()  # cooldown over
	assert guard.retry_in() == 0


def test_guard_counts_trips_until_the_api_answers(clock):
	guard = ConnectionGuard(TELEGRAM)
	guard.failed()
	guard.failed()
	assert guard.trips == 2
	guard.ok()
	assert guard.trips == 0
	guard.check()


def test_escape_makes_outside_text_safe_for_html_messages():
	from matele import escape

	assert escape('<a href="x">Ann & Co</a>') == '&lt;a href="x"&gt;Ann &amp; Co&lt;/a&gt;'
	assert escape('"x"', quote=True) == "&quot;x&quot;"
	assert escape(None) == "" and escape(42) == "42"
