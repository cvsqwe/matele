from __future__ import annotations

import datetime as dt
from typing import Literal

import pytest

from matele.callbacks import CallbackData, CallbackError, CallbackRegistry, action, callback, parse

reg = CallbackRegistry()


@callback("test:sample", registry=reg)
class Sample(CallbackData):
	number: int
	day: dt.date
	kind: Literal["a", "b"] = "a"
	rest: str = ""


@callback("test:flag", registry=reg)
class Flag:  # without the CallbackData base: the decorator adds it
	on: bool


HOME = action("menu:home", registry=reg)


def test_payload_round_trip_and_trailing_defaults():
	full = Sample(7, dt.date(2026, 10, 5), "b", "x:y")
	assert full.pack() == "test:sample:7:2026-10-05:b:x:y"
	assert reg.parse(full.pack()) == full
	short = Sample(7, dt.date(2026, 10, 5))
	assert short.pack() == "test:sample:7:2026-10-05"
	assert reg.parse("test:sample:7:2026-10-05") == short


def test_decorator_adds_the_base_and_freezes():
	flag = Flag(True)
	assert isinstance(flag, CallbackData)
	assert flag.pack() == "test:flag:1"
	assert reg.parse("test:flag:0") == Flag(False)
	with pytest.raises(AttributeError):
		flag.on = False  # type: ignore[misc]


def test_action_without_fields():
	assert HOME.pack() == "menu:home"
	assert type(reg.parse("menu:home")) is type(HOME)


@pytest.mark.parametrize(
	"data",
	[
		"test:sample:x:2026-10-05",  # not an int
		"test:sample:7:05.10.2026",  # not an ISO date
		"test:sample:7:2026-10-05:c",  # not in the Literal
		"test:sample:7",  # required field missing
		"test:flag:2",  # not a bool
		"test:nothing:1",  # unknown prefix
		"menu:home:extra",  # field on a payload that has none
	],
)
def test_bad_payloads_are_rejected(data):
	with pytest.raises(CallbackError):
		reg.parse(data)


def test_longest_prefix_wins():
	r = CallbackRegistry()

	@callback("a", registry=r)
	class Short:
		value: str

	@callback("a:b", registry=r)
	class Long:
		value: int

	assert r.parse("a:b:5") == Long(5)
	assert r.parse("a:c") == Short("c")


def test_prefixes_are_unique():
	with pytest.raises(ValueError, match="already used by Sample"):

		@callback("test:sample", registry=reg)
		class Other:
			x: int

	with pytest.raises(ValueError, match="already used"):
		action("menu:home", registry=reg)


def test_registries_are_independent():
	other = CallbackRegistry()
	action("menu:home", registry=other)  # same prefix, different registry
	with pytest.raises(CallbackError):
		parse("test:sample:1:2026-01-01", registry=other)


def test_only_the_last_field_may_contain_the_separator():
	r = CallbackRegistry()

	@callback("t", registry=r)
	class Two(CallbackData):
		first: str
		second: str

	assert r.parse(Two("a", "b:c").pack()) == Two("a", "b:c")
	with pytest.raises(ValueError, match="only the last field"):
		Two("a:b", "c").pack()


@pytest.mark.parametrize("prefix", ["", ":x", "x:"])
def test_bad_prefixes(prefix):
	with pytest.raises(ValueError, match="bad callback prefix"):
		callback(prefix, registry=CallbackRegistry())


def test_unsupported_field_type():
	with pytest.raises(TypeError, match="unsupported callback field type"):

		@callback("bad", registry=CallbackRegistry())
		class Bad:
			value: float


def test_signed_data_round_trips_and_forgeries_are_refused():
	from matele.callbacks import sign, unsign

	signed = sign("order:view:42", "s3cret")
	assert signed != "order:view:42" and unsign(signed, "s3cret") == "order:view:42"
	assert unsign("order:view:43" + signed[len("order:view:42") :], "s3cret") is None  # another order, same signature
	assert unsign("order:view:42", "s3cret") is None  # no signature at all
	assert unsign(signed, "other") is None
	assert unsign(sign("note:a#b", "s3cret"), "s3cret") == "note:a#b"  # data may contain the separator
	assert unsign("order:view:42#ы", "s3cret") is None
