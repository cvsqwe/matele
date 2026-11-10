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
import signal
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, NamedTuple, Protocol, TypeVar

from matele.callbacks import CallbackError, CallbackRegistry, default_registry
from matele.dispatch.context import Context
from matele.dispatch.filters import run_filter
from matele.dispatch.router import Handler, Kind, Router
from matele.fsm import FSMContext, MemoryStorage, Storage, StorageKey, state_matches
from matele.types import (
	PRIVATE,
	BotClient,
	CallbackQuery,
	ChatMemberUpdate,
	ChatStatus,
	IncomingEvent,
	Message,
	describe,
)

logger = logging.getLogger(__name__)

SHUTDOWN_TIMEOUT = 10.0  # seconds running handlers get to finish on a stop
CONCURRENCY = 100  # events of one messenger handled at once while polling

CallNext = Callable[[], Awaitable[None]]
Middleware = Callable[[Any, CallNext], Awaitable[None]]
ErrorHandler = Callable[[Any, Exception], Awaitable[Any]]
M = TypeVar("M", bound=Middleware)
E = TypeVar("E", bound=ErrorHandler)


class Command(NamedTuple):
	name: str  # lowercased, without "/" and "@bot"
	args: str | None
	mention: str | None  # "my_bot" for "/start@my_bot"


def parse_command(text: str) -> Command | None:
	"""'/start abc' -> Command('start', 'abc', None);
	'/Menu@my_bot' -> Command('menu', None, 'my_bot')."""
	if not text.startswith("/") or len(text) < 2 or text[1].isspace():
		return None
	head, _, rest = text.partition(" ")
	if "\n" in head:
		head, _, more = head.partition("\n")
		rest = more + (" " + rest if rest else "")
	name, _, mention = head[1:].partition("@")
	if not name:
		return None
	return Command(name.lower(), rest.strip() or None, mention or None)


def event_chat_id(event: IncomingEvent) -> int | None:
	"""The group an event happened in (None for a private chat)."""
	if isinstance(event, ChatMemberUpdate):
		return event.chat_id
	message = event if isinstance(event, Message) else event.message if isinstance(event, CallbackQuery) else None
	return message.chat_id if message is not None and message.chat_type != PRIVATE else None


class CommandMenuOwner(Protocol):
	title: str

	async def set_commands(self, commands: list[tuple[str, str]]) -> None: ...


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
		"""Whose conversation state an event belongs to: the person's, and in
		a group the person's in that group (so a form filled in one group
		doesn't leak into another, or into the private chat)."""
		return StorageKey(event.from_user.platform, event.from_user.id, event_chat_id(event))

	@asynccontextmanager
	async def exclusive(self, key: StorageKey) -> AsyncIterator[None]:
		"""Holds the storage's own lock on a conversation, if it has one
		(RedisStorage does), so other processes sharing the storage wait
		with that person's events. Polling and Webhook wrap feed() in it.
		If the lock can't be taken (the storage is down), the event is
		handled anyway: feed() will report the storage error."""
		make = getattr(self.storage, "lock", None)
		if make is None:
			yield
			return
		held = make(key)
		try:
			await held.__aenter__()
		except Exception:
			logger.exception("Could not lock the state of %s", key)
			yield
			return
		try:
			yield
		finally:
			try:
				await held.__aexit__(None, None, None)
			except Exception:
				logger.exception("Could not unlock the state of %s", key)

	async def feed(self, event: IncomingEvent) -> bool:
		"""Processes one event. True if a handler took it. Never raises:
		errors (of handlers, middleware or the storage) go to the error
		handlers, or to the log.

		Feed one person's events one at a time (polling and Webhook do):
		two events of the same person handled at once would both read the
		same state and the later save would overwrite the earlier one.
		When calling feed() yourself from several processes, wrap it in
		`async with dp.exclusive(dp.storage_key(event))`."""
		self.freeze()
		key = self.storage_key(event)
		try:
			record = await self.storage.get(key)
		except Exception as exc:
			# Without the person's state the event can't be routed safely, and
			# saving a blank state over the real one would lose it: stop here.
			ctx = self.context_class(event, FSMContext(key))
			await self._handle_error(ctx, exc)
			await self._answer(ctx)
			return False
		state = FSMContext(key, record)
		ctx = self.context_class(event, state)
		handled = False

		async def route() -> None:
			nonlocal handled
			handler = await self._resolve(ctx)
			if handler is None:
				logger.debug("No handler for %s", describe(event))
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
			await self._answer(ctx)
			if state.changed:
				try:
					if state.record.empty:
						await self.storage.delete(key)
					else:
						await self.storage.set(key, state.record)
				except Exception:
					logger.exception("Could not save the state of %s", key)
		return handled

	@staticmethod
	async def _answer(ctx: Context) -> None:
		"""Stops the button's spinner if the handler didn't. Whatever goes
		wrong here must not escape feed() or keep the state from being saved."""
		try:
			await ctx.answer()
		except Exception:
			logger.exception("Could not answer the button press")

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
				me = getattr(event.bot, "me", None)
				if (
					command.mention
					and me is not None
					and me.username
					and command.mention.lower() != me.username.lower()
				):
					logger.debug("/%s is addressed to @%s, not to this bot", command.name, command.mention)
					return None
				ctx.command, ctx.args = command.name, command.args
		elif isinstance(event, CallbackQuery):
			kind = Kind.CALLBACK
			try:
				ctx.payload = self.registry.parse(event.data)
			except CallbackError as exc:
				logger.debug("Unparsed button data: %s", exc)
		elif isinstance(event, ChatStatus):
			kind = Kind.CHAT_STATUS
		elif isinstance(event, ChatMemberUpdate):
			kind = Kind.MEMBER_UPDATE
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
			logger.exception("Unhandled error on %s", describe(ctx.event), exc_info=exc)
			return
		for handler in self._error_handlers:
			try:
				await handler(ctx, exc)
			except Exception:
				logger.exception("Error handler %r failed", handler)

	# ------------------------------------------------------------ running
	def menu_commands(self) -> list[tuple[str, str]]:
		"""(name, description) of every command registered with a description,
		in registration order."""
		self.freeze()
		assert self._table is not None
		menu: dict[str, str] = {}
		for handler in self._table[Kind.MESSAGE]:
			if handler.menu is not None:
				name, description = handler.menu
				menu.setdefault(name, description)  # the first registration wins
		return list(menu.items())

	async def publish_commands(self, *clients: CommandMenuOwner) -> None:
		"""Shows the described commands in each messenger's command menu.
		A failure is logged, not raised: the bot works without a menu."""
		commands = self.menu_commands()
		if not commands:
			return
		for client in clients:
			try:
				await client.set_commands(commands)
			except Exception as exc:
				logger.warning("%s: could not set the command menu: %s", client.title, exc)

	async def start_polling(
		self,
		*clients: BotClient,
		timeout: int = 30,
		shutdown_timeout: float = SHUTDOWN_TIMEOUT,
		concurrency: int = CONCURRENCY,
		at_least_once: bool = False,
	) -> None:
		"""Long-polls every client until cancelled. The command menu is
		published once each messenger is first reached. On cancel, handlers
		still running get `shutdown_timeout` seconds to finish. At most
		`concurrency` events per messenger are handled at once; beyond
		that polling waits, and the messenger keeps the rest.

		`at_least_once=True`: an event whose handling was cut short by a
		crash comes again after a restart (handlers must cope with
		repeats); see matele.dispatch.polling."""
		from matele.dispatch.polling import poll

		if not clients:
			raise ValueError("start_polling() needs at least one bot client")
		self.freeze()
		await asyncio.gather(
			*(
				poll(
					self,
					client,
					timeout=timeout,
					shutdown_timeout=shutdown_timeout,
					concurrency=concurrency,
					at_least_once=at_least_once,
				)
				for client in clients
			)
		)

	def run_polling(
		self,
		*clients: BotClient,
		timeout: int = 30,
		shutdown_timeout: float = SHUTDOWN_TIMEOUT,
		concurrency: int = CONCURRENCY,
		at_least_once: bool = False,
	) -> None:
		"""Blocking entry point: polls until Ctrl+C or SIGTERM (what Docker
		and systemd stop a service with), lets running handlers finish (up
		to `shutdown_timeout` seconds), then closes the clients and the
		storage."""

		async def main() -> None:
			task = asyncio.current_task()
			assert task is not None
			try:
				asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
			except (NotImplementedError, RuntimeError, ValueError):
				pass  # Windows, or not the main thread: Ctrl+C still works
			try:
				await self.start_polling(
					*clients,
					timeout=timeout,
					shutdown_timeout=shutdown_timeout,
					concurrency=concurrency,
					at_least_once=at_least_once,
				)
			finally:
				for client in clients:
					await client.close()
				await self.storage.close()

		try:
			asyncio.run(main())
		except (KeyboardInterrupt, asyncio.CancelledError):
			logger.info("Stopped")
