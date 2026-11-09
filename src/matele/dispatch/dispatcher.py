"""Dispatcher: turns one incoming event into (at most) one handler call.

For every event it
  1. loads the person's conversation state from the storage;
  2. builds the Context (parsing the command or the button payload);
  3. runs the middleware chain, at the end of which the first matching
	handler is called (see router.py for the order);
  4. on an exception, calls the error handlers (or logs it);
  5. answers a pressed button if nobody did, so it never keeps spinning;
  6. saves the state if it changed, even when the handler failed.

Feed it events yourself (`await dp.feed(event)`, e.g. from a webhook) or
let it long-poll the messengers (`dp.run_polling(max_bot, tg_bot)`).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from matele.callbacks import CallbackError, CallbackRegistry, default_registry
from matele.dispatch.context import Context
from matele.dispatch.filters import run_filter
from matele.dispatch.router import Handler, Kind, Router
from matele.fsm import FSMContext, MemoryStorage, Storage, StorageKey, state_matches
from matele.types import BotClient, CallbackQuery, ChatStatus, IncomingEvent, Message

logger = logging.getLogger(__name__)

CallNext = Callable[[], Awaitable[None]]
Middleware = Callable[[Any, CallNext], Awaitable[None]]
ErrorHandler = Callable[[Any, Exception], Awaitable[Any]]
M = TypeVar("M", bound=Middleware)
E = TypeVar("E", bound=ErrorHandler)


def parse_command(text: str) -> tuple[str, str | None] | None:
	"""'/start abc' -> ('start', 'abc'); '/Menu@my_bot' -> ('menu', None)."""
	if not text.startswith("/") or len(text) < 2 or text[1].isspace():
		return None
	head, _, rest = text.partition(" ")
	if "\n" in head:
		head, _, more = head.partition("\n")
		rest = more + (" " + rest if rest else "")
	name = head[1:].split("@", 1)[0].lower()
	return (name, rest.strip() or None) if name else None


class Dispatcher(Router):
	def __init__(
		self,
		*,
		storage: Storage | None = None,
		registry: CallbackRegistry | None = None,
		context_class: type[Context] = Context,
		name: str = "dispatcher",
	):
		super().__init__(name)
		self.storage: Storage = storage if storage is not None else MemoryStorage()
		self.registry = registry if registry is not None else default_registry
		self.context_class = context_class
		self._middlewares: list[Middleware] = []
		self._error_handlers: list[ErrorHandler] = []
		self._table: dict[Kind, list[Handler]] | None = None

	# ------------------------------------------------------------ setup
	def middleware(self, mw: M) -> M:
		"""Registers `async def mw(ctx, call_next)`; usable as a decorator.
		Middleware runs in registration order around routing and the
		handler, for every event (also ones no handler takes). Not calling
		`call_next()` stops the event."""
		self._check_open()
		self._middlewares.append(mw)
		return mw

	def on_error(self, handler: E) -> E:
		"""Registers `async def handler(ctx, exc)`, called when a handler or a
		middleware raises. Without one, the exception is logged."""
		self._check_open()
		self._error_handlers.append(handler)
		return handler

	def freeze(self) -> None:
		"""Indexes every router. Called on the first event; registering
		handlers afterwards is an error."""
		if self._table is not None:
			return
		table: dict[Kind, list[Handler]] = {kind: [] for kind in Kind}
		for router in self.walk():
			router._frozen = True
			for handler in router.handlers:
				table[handler.kind].append(handler)
		for handlers in table.values():
			handlers.sort(key=lambda h: h.tier)  # stable: keeps registration order within a tier
		self._table = table

	# ------------------------------------------------------------ dispatch
	def storage_key(self, event: IncomingEvent) -> StorageKey:
		return StorageKey(event.from_user.platform, event.from_user.id)

	async def feed(self, event: IncomingEvent) -> bool:
		"""Processes one event. True if a handler took it. Never raises for
		handler errors (they go to the error handlers)."""
		self.freeze()
		key = self.storage_key(event)
		state = FSMContext(key, await self.storage.get(key))
		ctx = self.context_class(event, state)
		handled = False

		async def route() -> None:
			nonlocal handled
			handler = await self._resolve(ctx)
			if handler is None:
				logger.debug("No handler for %r", event)
				return
			handled = True
			if handler.payload_cls is not None:
				await handler.func(ctx, ctx.payload)
			else:
				await handler.func(ctx)

		try:
			await self._chain(ctx, route)()
		except asyncio.CancelledError:
			raise
		except Exception as exc:
			await self._handle_error(ctx, exc)
		finally:
			await ctx.answer()
			if state.changed:
				try:
					if state.record.empty:
						await self.storage.delete(key)
					else:
						await self.storage.set(key, state.record)
				except Exception:
					logger.exception("Could not save the state of %s", key)
		return handled

	def _chain(self, ctx: Context, route: CallNext) -> CallNext:
		call = route
		for mw in reversed(self._middlewares):

			def step(mw: Middleware = mw, nxt: CallNext = call) -> Awaitable[None]:
				return mw(ctx, nxt)

			call = step
		return call

	async def _resolve(self, ctx: Context) -> Handler | None:
		assert self._table is not None
		event = ctx.event
		if isinstance(event, Message):
			kind = Kind.MESSAGE
			command = parse_command(event.text or "")
			if command is not None:
				ctx.command, ctx.args = command
		elif isinstance(event, CallbackQuery):
			kind = Kind.CALLBACK
			try:
				ctx.payload = self.registry.parse(event.data)
			except CallbackError as exc:
				logger.debug("Unparsed button data: %s", exc)
		elif isinstance(event, ChatStatus):
			kind = Kind.CHAT_STATUS
		else:
			raise TypeError(f"not an event: {event!r}")

		current = ctx.state.current
		for handler in self._table[kind]:
			if handler.commands is not None and ctx.command not in handler.commands:
				continue
			if handler.payload_cls is not None and type(ctx.payload) is not handler.payload_cls:
				continue
			if not state_matches(handler.state, current):
				continue
			ctx.match = None
			if await self._passes(handler, ctx):
				return handler
		return None

	@staticmethod
	async def _passes(handler: Handler, ctx: Context) -> bool:
		for f in handler.filters:  # in order, stopping at the first refusal
			if not await run_filter(f, ctx):
				return False
		return True

	async def _handle_error(self, ctx: Context, exc: Exception) -> None:
		if not self._error_handlers:
			logger.exception("Unhandled error on %r", ctx.event, exc_info=exc)
			return
		for handler in self._error_handlers:
			try:
				await handler(ctx, exc)
			except Exception:
				logger.exception("Error handler %r failed", handler)

	# ------------------------------------------------------------ running
	async def start_polling(self, *clients: BotClient, timeout: int = 30) -> None:
		"""Long-polls every client until cancelled."""
		from matele.dispatch.polling import poll

		if not clients:
			raise ValueError("start_polling() needs at least one bot client")
		self.freeze()
		await asyncio.gather(*(poll(self, client, timeout=timeout) for client in clients))

	def run_polling(self, *clients: BotClient, timeout: int = 30) -> None:
		"""Blocking entry point: polls until Ctrl+C, then closes the clients
		and the storage."""

		async def main() -> None:
			try:
				await self.start_polling(*clients, timeout=timeout)
			finally:
				for client in clients:
					await client.close()
				await self.storage.close()

		try:
			asyncio.run(main())
		except KeyboardInterrupt:
			logger.info("Stopped")
