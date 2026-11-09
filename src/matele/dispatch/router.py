"""Router: where handlers register. Split a bot into feature routers and
join them with `include()`; the Dispatcher is the root router.

	router = Router("orders")

	@router.command("start")
	async def start(ctx): ...

	@router.callback(SetStatus, is_manager)
	async def set_status(ctx, payload: SetStatus): ...

	@router.message(state=Order.address)
	async def address(ctx): ...

Which handler takes an event: the first one whose command, state and
filters all match, looking in this order:

	messages:  command handlers → handlers bound to a state → the rest
	buttons:   handlers bound to a state → the rest
	within a tier, in registration order (routers depth-first, in the
	order they were included).

So `/cancel` reaches its command handler even mid-conversation, and a
state's input handler wins over a catch-all registered before it.
`state=` defaults to ANY_STATE (whatever state); `state=None` means "in
no state".
"""

from __future__ import annotations

import enum
import re
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from typing import Any, TypeVar

from matele.callbacks import CallbackData, as_class
from matele.dispatch.filters import Filter, FilterLike, as_filter
from matele.fsm.state import ANY_STATE, StateSpec, is_any

HandlerFunc = Callable[..., Awaitable[Any]]
_MENU_NAME = re.compile(r"^[a-z0-9_]{1,32}$")  # what both messengers accept in the menu
H = TypeVar("H", bound=HandlerFunc)
P = TypeVar("P", bound=CallbackData)


class Kind(enum.Enum):
	MESSAGE = "message"
	CALLBACK = "callback"
	CHAT_STATUS = "chat_status"
	MEMBER_UPDATE = "member_update"


@dataclass(eq=False)
class Handler:
	kind: Kind
	func: HandlerFunc
	filters: tuple[Filter, ...]
	state: StateSpec
	router: Router
	commands: frozenset[str] | None = None  # command handlers only
	menu: tuple[str, str] | None = None  # (name, description) shown in the command menu
	payload_cls: type[CallbackData] | None = None  # typed button handlers: func(ctx, payload)

	@property
	def tier(self) -> int:
		if self.commands is not None:
			return 0
		return 2 if is_any(self.state) else 1

	def __repr__(self) -> str:
		return f"<Handler {self.kind.value} {getattr(self.func, '__qualname__', self.func)} in {self.router.name!r}>"


class Router:
	def __init__(self, name: str = ""):
		self.name = name
		self.handlers: list[Handler] = []
		self.children: list[Router] = []
		self.parent: Router | None = None
		self._frozen = False

	def __repr__(self) -> str:
		return f"<{type(self).__name__} {self.name!r}>"

	def include(self, *routers: Router) -> None:
		self._check_open()
		for router in routers:
			if router is self or router.parent is not None:
				raise ValueError(f"{router!r} is already included somewhere")
			router.parent = self
			self.children.append(router)

	def walk(self) -> Iterator[Router]:
		yield self
		for child in self.children:
			yield from child.walk()

	# ------------------------------------------------------------ registration
	def message(self, *filters: FilterLike, state: StateSpec = ANY_STATE) -> Callable[[H], H]:
		"""Any incoming message (text, files…) that passes the filters."""
		return self._register(Kind.MESSAGE, filters, state)

	def command(
		self,
		*names_and_filters: str | FilterLike,
		state: StateSpec = ANY_STATE,
		description: str | None = None,
	) -> Callable[[H], H]:
		"""`/name` messages; strings are command names, the rest are filters:
		@router.command("start", "help", is_admin).

		With a `description`, the command (its first name) is shown in the
		messengers' command menu when the bot starts; without one it works
		but stays hidden."""
		names = [n for n in names_and_filters if isinstance(n, str)]
		filters = tuple(f for f in names_and_filters if not isinstance(f, str))
		if not names:
			raise ValueError("command() needs at least one command name")
		ordered = [n.removeprefix("/").lower() for n in names]
		if any(not c or " " in c for c in ordered):
			raise ValueError(f"bad command names: {names!r}")
		if description is not None:
			if not _MENU_NAME.match(ordered[0]):
				raise ValueError(f"/{ordered[0]}: a command in the menu must be 1-32 of a-z, 0-9, _")
			if not 1 <= len(description) <= 256:
				raise ValueError(f"/{ordered[0]}: the description must be 1-256 characters")
		return self._register(
			Kind.MESSAGE,
			filters,
			state,
			commands=frozenset(ordered),
			menu=(ordered[0], description) if description is not None else None,
		)

	def callback(
		self, target: type[P] | P, *filters: FilterLike, state: StateSpec = ANY_STATE
	) -> Callable[[Callable[[Any, P], Awaitable[Any]]], Callable[[Any, P], Awaitable[Any]]]:
		"""A button whose payload is `target` (a CallbackData class, or an
		action() instance). The handler gets (ctx, payload)."""
		return self._register(Kind.CALLBACK, filters, state, payload_cls=as_class(target))

	def raw_callback(self, *filters: FilterLike, state: StateSpec = ANY_STATE) -> Callable[[H], H]:
		"""Any button press that passes the filters, including payloads that
		don't parse (buttons from an older version of the bot: `ctx.payload`
		is None). Handler gets (ctx); raw data is `ctx.callback.data`."""
		return self._register(Kind.CALLBACK, filters, state)

	def chat_status(self, *filters: FilterLike) -> Callable[[H], H]:
		"""The person blocked or restarted the bot (`ctx.chat_status.reachable`)."""
		return self._register(Kind.CHAT_STATUS, filters, ANY_STATE)

	def member_update(self, *filters: FilterLike) -> Callable[[H], H]:
		"""Someone joined or left a group, or the bot was added to / removed
		from one (`ctx.member_update`: member, joined, is_me)."""
		return self._register(Kind.MEMBER_UPDATE, filters, ANY_STATE)

	def _register(
		self,
		kind: Kind,
		filters: tuple[FilterLike, ...],
		state: StateSpec,
		*,
		commands: frozenset[str] | None = None,
		payload_cls: type[CallbackData] | None = None,
		menu: tuple[str, str] | None = None,
	) -> Callable[[Any], Any]:
		self._check_open()

		def decorator(func: H) -> H:
			self._check_open()
			self.handlers.append(
				Handler(
					kind=kind,
					func=func,
					filters=tuple(as_filter(f) for f in filters),
					state=state,
					router=self,
					commands=commands,
					payload_cls=payload_cls,
					menu=menu,
				)
			)
			return func

		return decorator

	def _check_open(self) -> None:
		if self._frozen:
			raise RuntimeError(f"{self!r} is already in use by a running dispatcher; register handlers before starting")
