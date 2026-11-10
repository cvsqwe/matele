"""Typed callback data: every button payload is a small frozen dataclass
with a fixed prefix, packed to "prefix:field:field…" and parsed back by
the dispatcher, so handlers never split strings.

	@callback("order:set")
	class SetStatus(CallbackData):
		order_id: int
		status: Literal["paid", "shipped"]
		note: str = ""

	Button("✅ Оплачен", SetStatus(42, "paid").pack())

Fields may be int, str, bool, datetime.date or a Literal of strings.
Trailing fields equal to their defaults are left out when packing and get
their defaults back when parsing, so a field with a default can be added
to an existing payload without breaking buttons already sent. Only the
last field may be a str containing ":".

Payloads without fields are `action("menu:home")` instances.

Keep payloads short (ids, not names): Telegram allows 64 bytes.

Prefixes are unique within a registry: declaring the same one twice is an
error at import time, so two features can never fight over a button.
`callback()` and `parse()` use the default registry unless given another
(tests and plugins may want their own).

Button data comes back from the person's client, and a modified client
can send any string: `order:view:43` for an order that was never shown
to them. Give the bots a `callback_secret` and every payload is sent
with a signature (`sign()`); data that doesn't carry a valid one reaches
the handlers as an outdated button, not as a payload. A person can then
only press buttons the bot really made, though still any of them they
have seen (in a group, other people's too): who may do what is for the
handler to check.
"""

from __future__ import annotations

import base64
import dataclasses
import datetime as dt
import hashlib
import hmac
import typing
from collections.abc import Callable
from typing import Any, ClassVar, Literal, Self, TypeVar, cast, dataclass_transform

SEP = ":"

T = TypeVar("T")


SIGN_SEP = "#"
SIGNATURE_LENGTH = 10  # base64 characters: 60 bits of an HMAC-SHA256


def _signature(data: str, secret: str) -> str:
	digest = hmac.new(secret.encode(), data.encode(), hashlib.sha256).digest()
	return base64.urlsafe_b64encode(digest).decode()[:SIGNATURE_LENGTH]


def sign(data: str, secret: str) -> str:
	"""`data` with a signature only the holder of `secret` can make."""
	return f"{data}{SIGN_SEP}{_signature(data, secret)}"


def unsign(signed: str, secret: str) -> str | None:
	"""The data `sign()` was given; None if the signature is missing or wrong."""
	data, sep, given = signed.rpartition(SIGN_SEP)
	if not sep or not hmac.compare_digest(given.encode(), _signature(data, secret).encode()):
		return None
	return data


class CallbackError(ValueError):
	"""Payload that doesn't parse: a stale button from an older version, or forged."""


class CallbackData:
	prefix: ClassVar[str]
	_fields: ClassVar[tuple[tuple[str, Any, Any], ...]]  # (name, type, default or MISSING)

	def pack(self) -> str:
		values = [_dump(getattr(self, name)) for name, _, _ in self._fields]
		for i, value in enumerate(values[:-1]):
			if SEP in value:
				raise ValueError(f"{type(self).__name__}.{self._fields[i][0]}: only the last field may contain {SEP!r}")
		# drop trailing defaults, so optional fields don't widen old payloads
		n = len(values)
		while (
			n
			and self._fields[n - 1][2] is not dataclasses.MISSING
			and getattr(self, self._fields[n - 1][0]) == self._fields[n - 1][2]
		):
			n -= 1
		return SEP.join([self.prefix, *values[:n]])

	@classmethod
	def unpack(cls, data: str) -> Self:
		if data == cls.prefix:
			raw: list[str] = []
		elif data.startswith(cls.prefix + SEP):
			rest = data[len(cls.prefix) + 1 :]
			raw = rest.split(SEP, max(len(cls._fields) - 1, 0)) if cls._fields else [rest]
		else:
			raise CallbackError(f"not a {cls.__name__}: {data!r}")
		if len(raw) > len(cls._fields):
			raise CallbackError(f"too many fields for {cls.__name__}: {data!r}")
		kwargs = {}
		for i, (name, tp, default) in enumerate(cls._fields):
			if i < len(raw):
				kwargs[name] = _load(raw[i], tp)
			elif default is dataclasses.MISSING:
				raise CallbackError(f"{cls.__name__}.{name} missing: {data!r}")
		return cast(Callable[..., Self], cls)(**kwargs)


class CallbackRegistry:
	"""Prefix -> payload class. The longest registered prefix wins."""

	def __init__(self) -> None:
		self._classes: dict[str, type[CallbackData]] = {}

	def check_free(self, prefix: str) -> None:
		existing = self._classes.get(prefix)
		if existing is not None:
			raise ValueError(f"callback prefix {prefix!r} is already used by {existing.__qualname__}")

	def register(self, cls: type[CallbackData]) -> None:
		self.check_free(cls.prefix)
		self._classes[cls.prefix] = cls

	def parse(self, data: str) -> CallbackData:
		parts = data.split(SEP)
		for n in range(len(parts), 0, -1):
			cls = self._classes.get(SEP.join(parts[:n]))
			if cls is not None:
				return cls.unpack(data)
		raise CallbackError(f"unknown callback {data!r}")


default_registry = CallbackRegistry()


@dataclass_transform(frozen_default=True)
def callback(prefix: str, *, registry: CallbackRegistry | None = None) -> Callable[[type[T]], type[T]]:
	"""Class decorator: makes a frozen dataclass and registers its prefix."""
	if not prefix or prefix.startswith(SEP) or prefix.endswith(SEP):
		raise ValueError(f"bad callback prefix {prefix!r}")
	target = registry if registry is not None else default_registry

	def decorate(cls: type[T]) -> type[T]:
		target.check_free(prefix)
		base = dataclasses.dataclass(frozen=True)(cls)
		bases: tuple[type, ...] = (base,) if issubclass(base, CallbackData) else (base, CallbackData)
		dc = cast(Callable[..., type[CallbackData]], type)(
			cls.__name__,
			bases,
			{"__module__": cls.__module__, "__qualname__": cls.__qualname__, "__doc__": cls.__doc__},
		)
		hints = typing.get_type_hints(cls)
		dc.prefix = prefix
		dc._fields = tuple((f.name, hints[f.name], f.default) for f in dataclasses.fields(cast(Any, dc)))
		for name, tp, _ in dc._fields:
			_check_type(dc, name, tp)
		target.register(dc)
		return cast(type[T], dc)

	return decorate


def action(prefix: str, *, registry: CallbackRegistry | None = None) -> CallbackData:
	"""A payload without fields, ready to put on a button."""
	cls = callback(prefix, registry=registry)(type(_class_name(prefix), (), {"__module__": __name__}))
	return cast(type[CallbackData], cls)()


def parse(data: str, *, registry: CallbackRegistry | None = None) -> CallbackData:
	"""The payload for raw callback data; the longest registered prefix wins."""
	return (registry if registry is not None else default_registry).parse(data)


def as_class(target: type[CallbackData] | CallbackData) -> type[CallbackData]:
	return target if isinstance(target, type) else type(target)


# ----------------------------------------------------------------------
def _class_name(prefix: str) -> str:
	return "".join(p.capitalize() for p in prefix.replace("-", "_").replace(SEP, "_").split("_")) or "Action"


def _check_type(cls: type, name: str, tp: Any) -> None:
	if tp in (int, str, bool, dt.date):
		return
	if typing.get_origin(tp) is Literal and all(isinstance(a, str) for a in typing.get_args(tp)):
		return
	raise TypeError(f"{cls.__qualname__}.{name}: unsupported callback field type {tp!r}")


def _dump(value: Any) -> str:
	if isinstance(value, bool):
		return "1" if value else "0"
	if isinstance(value, dt.date):
		return value.isoformat()
	return str(value)


def _load(raw: str, tp: Any) -> Any:
	try:
		if tp is bool:
			if raw not in ("0", "1"):
				raise ValueError(raw)
			return raw == "1"
		if tp is int:
			return int(raw)
		if tp is dt.date:
			return dt.date.fromisoformat(raw)
		if tp is str:
			return raw
	except ValueError as exc:
		raise CallbackError(f"bad {tp.__name__} {raw!r}") from exc
	if raw not in typing.get_args(tp):  # Literal
		raise CallbackError(f"{raw!r} not one of {typing.get_args(tp)}")
	return raw
