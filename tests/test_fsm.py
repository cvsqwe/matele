from __future__ import annotations

import pytest

from matele.fsm import ANY_STATE, FSMContext, MemoryStorage, State, StateRecord, StatesGroup, StorageKey, state_matches


class Order(StatesGroup):
	address = State()
	phone = State()
	custom = State("order:custom")


class Other(StatesGroup):
	step = State()


KEY = StorageKey("max", 1)


def test_state_names_come_from_the_group():
	assert Order.address.name == "Order:address"
	assert Order.custom.name == "order:custom"
	assert Order.address.group is Order
	assert list(Order) == [Order.address, Order.phone, Order.custom]
	assert Order.phone in Order and "Order:phone" in Order
	assert Other.step not in Order


def test_standalone_state_needs_a_name():
	assert State("solo").name == "solo"
	with pytest.raises(RuntimeError, match="explicit name"):
		_ = State().name


@pytest.mark.parametrize(
	("spec", "current", "expected"),
	[
		(ANY_STATE, None, True),
		(ANY_STATE, "Order:address", True),
		(None, None, True),
		(None, "Order:address", False),
		(Order.address, "Order:address", True),
		(Order.address, "Order:phone", False),
		(Order, "Order:phone", True),
		(Order, None, False),
		(Order, "Other:step", False),
		((Order.address, Other), "Other:step", True),
		((Order.address, None), None, True),
	],
)
def test_state_matching(spec, current, expected):
	assert state_matches(spec, current) is expected


def test_fsm_context_tracks_changes_and_clear_keeps_meta():
	fsm = FSMContext(KEY)
	assert not fsm.changed and fsm.current is None
	fsm.set(Order.address)
	fsm.update(item=5)
	fsm.meta["ui"] = "42"
	assert fsm.changed and fsm.is_in(Order) and fsm.get("item") == 5
	fsm.clear()
	assert fsm.current is None and fsm.data == {} and fsm.meta == {"ui": "42"}


async def test_memory_storage_returns_copies():
	storage = MemoryStorage()
	record = StateRecord("Order:address", {"items": [1]})
	await storage.set(KEY, record)
	record.data["items"].append(2)  # mutating after save doesn't leak in
	loaded = await storage.get(KEY)
	assert loaded == StateRecord("Order:address", {"items": [1]})
	assert loaded is not None
	loaded.data["items"].append(3)
	assert (await storage.get(KEY)) == StateRecord("Order:address", {"items": [1]})
	await storage.delete(KEY)
	assert await storage.get(KEY) is None and len(storage) == 0
