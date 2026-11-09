"""Conversation states.

	class Order(StatesGroup):
		address = State()
		phone = State()

	@router.message(state=Order.address)
	async def got_address(ctx): ...

A state is stored by name ("Order:address"), so renaming a group or an
attribute orphans conversations already in that state; pass an explicit
name (`State("order:address")`) to keep it stable across refactors.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

SEP = ":"


class State:
	def __init__(self, name: str | None = None):
		self._name = name
		self.group: type[StatesGroup] | None = None

	def __set_name__(self, owner: type, attr: str) -> None:
		if self._name is None:
			self._name = f"{owner.__qualname__}{SEP}{attr}"

	@property
	def name(self) -> str:
		if self._name is None:
			raise RuntimeError("a State outside a StatesGroup needs an explicit name: State('my:state')")
		return self._name

	def __repr__(self) -> str:
		return f"State({self._name!r})"


class _StatesGroupMeta(type):
	__states__: tuple[State, ...]

	def __new__(mcs, name: str, bases: tuple[type, ...], namespace: dict[str, Any]):
		cls = super().__new__(mcs, name, bases, namespace)
		states = tuple(v for v in namespace.values() if isinstance(v, State))
		for state in states:
			state.group = cls  # type: ignore[assignment]
		cls.__states__ = states
		return cls

	def __iter__(cls) -> Iterator[State]:
		return iter(cls.__states__)

	def __contains__(cls, item: object) -> bool:
		name = item.name if isinstance(item, State) else item
		return any(s.name == name for s in cls.__states__)


class StatesGroup(metaclass=_StatesGroupMeta):
	"""A namespace of related states; a handler can be bound to the whole group."""


class _AnyState:
	def __repr__(self) -> str:
		return "ANY_STATE"


ANY_STATE: Any = _AnyState()
"""Default for `state=`: the handler runs whatever state the person is in."""

StateSpec = State | type[StatesGroup] | None | _AnyState | tuple["StateSpec", ...]


def state_matches(spec: StateSpec, current: str | None) -> bool:
	"""Does the person's current state (a name, or None) satisfy `spec`?
	None means "not in any state"."""
	if isinstance(spec, _AnyState):
		return True
	if spec is None:
		return current is None
	if isinstance(spec, State):
		return spec.name == current
	if isinstance(spec, tuple):
		return any(state_matches(s, current) for s in spec)
	if isinstance(spec, type) and issubclass(spec, StatesGroup):
		return current is not None and current in spec
	raise TypeError(f"not a state spec: {spec!r}")


def is_any(spec: StateSpec) -> bool:
	return isinstance(spec, _AnyState)
