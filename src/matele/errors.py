"""Errors every transport raises, and the per-client circuit breaker that
keeps messengers independent of each other."""

from __future__ import annotations

import time

from matele.types import MAX, TELEGRAM

PLATFORM_TITLES = {MAX: "MAX", TELEGRAM: "Telegram"}


def platform_title(platform: str) -> str:
	return PLATFORM_TITLES.get(platform, platform)


class BotApiError(Exception):
	"""Any failed call to a messenger's bot API."""

	def __init__(self, status_code: int, code: str, message: str, *, platform: str = ""):
		self.status_code = status_code
		self.code = code
		self.message = message
		self.platform = platform
		super().__init__(f"{platform_title(platform) or 'Bot'} API error {status_code} [{code}]: {message}")


class BotForbiddenError(BotApiError):
	"""HTTP 403: the user blocked the bot or never started it."""


class BotUnavailableError(BotApiError):
	"""The API can't be reached right now (network error, or the guard is
	still cooling down after one)."""


# Error texts (lowercased) that mean "this person can't get messages from
# the bot": Telegram answers 403 "bot was blocked by the user" / "user is
# deactivated" and 400 "chat not found"; MAX answers 403 (chat.denied and
# the like) or 404 with a "*.not.found" code for a chat or user it doesn't
# know.
_UNREACHABLE_NEEDLES = (
	"blocked",
	"deactivated",
	"chat not found",
	"user not found",
	"chat.not.found",
	"user.not.found",
	"chat.denied",
	"dialog.suspended",
)


def is_unreachable(exc: BaseException) -> bool:
	"""True if the failure says the recipient blocked the bot (or their chat
	is gone): retrying won't help until they come back to the bot."""
	if isinstance(exc, BotForbiddenError):
		return True
	if not isinstance(exc, BotApiError) or isinstance(exc, BotUnavailableError):
		return False
	if exc.status_code not in (400, 403, 404):
		return False
	text = f"{exc.code} {exc.message}".lower()
	return any(needle in text for needle in _UNREACHABLE_NEEDLES)


class ConnectionGuard:
	"""After a network failure, ordinary calls to that messenger fail
	instantly for `cooldown` seconds instead of each hanging until its own
	timeout, so a broadcast isn't held up by one unreachable messenger
	while the other works fine. Long polling bypasses the guard (it is the
	probe) and closes it as soon as the API answers."""

	def __init__(self, platform: str, cooldown: float = 30.0):
		self.platform = platform
		self.cooldown = cooldown
		self._down_until = 0.0
		# Network failures in a row since the API last answered: tells a
		# short blip (worth waiting out) from an outage.
		self.trips = 0

	def check(self) -> None:
		if time.monotonic() < self._down_until:
			raise BotUnavailableError(0, "unavailable", "API unreachable, retrying later", platform=self.platform)

	def retry_in(self) -> float:
		"""Seconds until ordinary calls are let through again (0 = now)."""
		return max(0.0, self._down_until - time.monotonic())

	def failed(self) -> None:
		self._down_until = time.monotonic() + self.cooldown
		self.trips += 1

	def ok(self) -> None:
		self._down_until = 0.0
		self.trips = 0
