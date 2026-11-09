"""Filters decide whether a handler takes an event.

A filter is any callable `(ctx) -> bool` or `async (ctx) -> bool`. Wrap
one in `as_filter()` (or subclass Filter) to combine with `&`, `|`, `~`:

	is_admin = as_filter(lambda ctx: ctx.user.id in ADMINS)

	@router.command("ban", is_admin)
	@router.message(Text("да", "yes", ignore_case=True) & ~is_admin)
"""

from __future__ import annotations

import inspect
import re
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any

from matele.dispatch.context import Context

FilterFunc = Callable[[Any], bool | Awaitable[bool]]


class Filter(ABC):
	@abstractmethod
	def check(self, ctx: Context) -> bool | Awaitable[bool]: ...

	async def __call__(self, ctx: Context) -> bool:
		return await run_filter(self.check, ctx)

	def __and__(self, other: FilterLike) -> Filter:
		return _All((self, as_filter(other)))

	def __or__(self, other: FilterLike) -> Filter:
		return _Any((self, as_filter(other)))

	def __invert__(self) -> Filter:
		return _Not(self)


FilterLike = Filter | FilterFunc


async def run_filter(f: FilterLike, ctx: Context) -> bool:
	result = f(ctx)
	if inspect.isawaitable(result):
		result = await result
	return bool(result)


def as_filter(f: FilterLike) -> Filter:
	return f if isinstance(f, Filter) else _Func(f)


class _Func(Filter):
	def __init__(self, func: FilterFunc):
		self.func = func

	def check(self, ctx: Context) -> bool | Awaitable[bool]:
		return self.func(ctx)

	def __repr__(self) -> str:
		return f"as_filter({getattr(self.func, '__qualname__', self.func)!r})"


class _All(Filter):
	def __init__(self, filters: tuple[Filter, ...]):
		self.filters = filters

	async def check(self, ctx: Context) -> bool:
		for f in self.filters:
			if not await f(ctx):
				return False
		return True


class _Any(Filter):
	def __init__(self, filters: tuple[Filter, ...]):
		self.filters = filters

	async def check(self, ctx: Context) -> bool:
		for f in self.filters:
			if await f(ctx):
				return True
		return False


class _Not(Filter):
	def __init__(self, inner: Filter):
		self.inner = inner

	async def check(self, ctx: Context) -> bool:
		return not await self.inner(ctx)


# ---------------------------------------------------------------------
# Built-in filters
# ---------------------------------------------------------------------
class Text(Filter):
	"""The message text is exactly one of `variants` (spaces around ignored)."""

	def __init__(self, *variants: str, ignore_case: bool = False):
		if not variants:
			raise ValueError("Text() needs at least one variant")
		self.ignore_case = ignore_case
		self.variants = {self._norm(v) for v in variants}

	def _norm(self, value: str) -> str:
		value = value.strip()
		return value.casefold() if self.ignore_case else value

	def check(self, ctx: Context) -> bool:
		return bool(ctx.text) and self._norm(ctx.text) in self.variants


class Regex(Filter):
	"""The message text (or, for a button, its raw data) matches; the match
	is put on `ctx.match`."""

	def __init__(self, pattern: str | re.Pattern[str], flags: int = 0):
		self.pattern = re.compile(pattern, flags) if isinstance(pattern, str) else pattern

	def check(self, ctx: Context) -> bool:
		source = ctx.callback.data if ctx.callback is not None else ctx.text
		match = self.pattern.search(source)
		if match is None:
			return False
		ctx.match = match
		return True


class HasAttachment(Filter):
	"""The message has an attachment (of one of `types`, if given:
	"file", "image", "video", "audio", "voice", …)."""

	def __init__(self, *types: str):
		self.types = set(types)

	def check(self, ctx: Context) -> bool:
		message = ctx.event if ctx.callback is None else None
		attachments = getattr(message, "attachments", None) or []
		return any(not self.types or a.type in self.types for a in attachments)


class OnPlatform(Filter):
	"""The event came from one of these messengers (matele.MAX, matele.TELEGRAM)."""

	def __init__(self, *platforms: str):
		if not platforms:
			raise ValueError("OnPlatform() needs at least one platform")
		self.platforms = set(platforms)

	def check(self, ctx: Context) -> bool:
		return ctx.platform in self.platforms


class ChatType(Filter):
	"""The event happened in one of these chat types (matele.PRIVATE, matele.GROUP)."""

	def __init__(self, *chat_types: str):
		if not chat_types:
			raise ValueError("ChatType() needs at least one chat type")
		self.chat_types = set(chat_types)

	def check(self, ctx: Context) -> bool:
		return ctx.chat_type in self.chat_types
