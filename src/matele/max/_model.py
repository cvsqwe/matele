"""Runtime for the generated MAX models: JSON <-> dataclasses.

Decoding is deliberately lenient, so a change on MAX's side doesn't take
a bot down:
  • unknown fields are ignored (the original dict stays on `.raw`);
  • an unknown discriminator value ("type": "poll") decodes to the base
	class (e.g. `Attachment`) with its common fields and `.raw`;
  • a required field the server left out becomes None (logged once);
  • scalars are not validated: the server's values are trusted.

Encoding drops None values: an optional field left at None is simply not
sent. An empty list is sent as an empty list (MAX treats `attachments: []`
as "remove all attachments").
"""

from __future__ import annotations

import dataclasses
import logging
import types
import typing
from typing import Any, ClassVar, Literal, Self, Union

logger = logging.getLogger(__name__)

_FieldSpec = tuple[str, str, Any, bool]  # (python name, wire name, type, required)
_specs: dict[type, tuple[_FieldSpec, ...]] = {}
_warned: set[tuple[type, str]] = set()


def wire(name: str) -> Any:
	"""Field whose JSON name isn't a valid Python name (`from` -> `from_`)."""
	return dataclasses.field(metadata={"wire": name})


def wire_optional(name: str) -> Any:
	return dataclasses.field(default=None, metadata={"wire": name})


class Model:
	# Set on the base of a polymorphic family: the tag field and tag -> class.
	__tag__: ClassVar[str | None] = None
	__subtypes__: ClassVar[dict[str, type[Model]]] = {}

	@classmethod
	def from_dict(cls, data: dict[str, Any]) -> Self:
		if cls.__tag__ is not None:
			sub = cls.__subtypes__.get(data.get(cls.__tag__))  # type: ignore[arg-type]
			if sub is not None and sub is not cls and issubclass(sub, cls):
				return sub.from_dict(data)  # type: ignore[return-value]
		kwargs: dict[str, Any] = {}
		for name, wire_name, tp, required in _fields(cls):
			if wire_name in data:
				kwargs[name] = decode(tp, data[wire_name])
			elif required:
				if (cls, name) not in _warned:
					_warned.add((cls, name))
					logger.debug("MAX sent %s without required field %r", cls.__name__, wire_name)
				kwargs[name] = None
		obj = cls(**kwargs)
		obj.__dict__["_raw"] = data
		return obj

	@property
	def raw(self) -> dict[str, Any] | None:
		"""The JSON this object was decoded from (None if built in code)."""
		return self.__dict__.get("_raw")

	def to_dict(self) -> dict[str, Any]:
		result: dict[str, Any] = {}
		for name, wire_name, _, _ in _fields(type(self)):
			value = getattr(self, name)
			if value is not None:
				result[wire_name] = encode(value)
		return result


def _fields(cls: type) -> tuple[_FieldSpec, ...]:
	spec = _specs.get(cls)
	if spec is None:
		hints = typing.get_type_hints(cls)
		spec = tuple(
			(
				f.name,
				f.metadata.get("wire", f.name),
				hints[f.name],
				f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING,
			)
			for f in dataclasses.fields(cls)
		)
		_specs[cls] = spec
	return spec


def decode(tp: Any, value: Any) -> Any:
	if value is None or tp is Any:
		return value
	origin = typing.get_origin(tp)
	if origin is Union or origin is types.UnionType:
		args = [a for a in typing.get_args(tp) if a is not type(None)]
		return decode(args[0], value) if len(args) == 1 else value
	if origin is list:
		(item,) = typing.get_args(tp)
		return [decode(item, v) for v in value] if isinstance(value, list) else value
	if origin is dict:
		_, item = typing.get_args(tp)
		return {k: decode(item, v) for k, v in value.items()} if isinstance(value, dict) else value
	if origin is Literal:
		return value
	if isinstance(tp, type) and issubclass(tp, Model):
		return tp.from_dict(value) if isinstance(value, dict) else value
	return value


def encode(value: Any) -> Any:
	if isinstance(value, Model):
		return value.to_dict()
	if isinstance(value, list | tuple | set | frozenset):
		return [encode(v) for v in value]
	if isinstance(value, dict):
		return {k: encode(v) for k, v in value.items() if v is not None}
	return value
