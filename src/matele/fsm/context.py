"""FSMContext: one person's conversation state, as `ctx.state`."""

from __future__ import annotations

import copy
from typing import Any

from matele.fsm.state import State, StateSpec, state_matches
from matele.fsm.storage import StateRecord, StorageKey


class FSMContext:
	def __init__(self, key: StorageKey, record: StateRecord | None = None):
		self.key = key
		self.record = record if record is not None else StateRecord()
		self._loaded = copy.deepcopy(self.record)

	@property
	def changed(self) -> bool:
		return self.record != self._loaded

	@property
	def current(self) -> str | None:
		"""Name of the current state, None outside any."""
		return self.record.state

	def set(self, state: State | None) -> None:
		self.record.state = state.name if state is not None else None

	def is_in(self, spec: StateSpec) -> bool:
		return state_matches(spec, self.record.state)

	@property
	def data(self) -> dict[str, Any]:
		return self.record.data

	def get(self, key: str, default: Any = None) -> Any:
		return self.record.data.get(key, default)

	def update(self, **values: Any) -> None:
		self.record.data.update(values)

	def clear(self) -> None:
		"""Ends the conversation: no state, no data."""
		self.record.state = None
		self.record.data = {}

	@property
	def meta(self) -> dict[str, Any]:
		"""Bookkeeping for library components; survives clear()."""
		return self.record.meta

	def __repr__(self) -> str:
		return f"FSMContext({self.key!r}, state={self.record.state!r})"
