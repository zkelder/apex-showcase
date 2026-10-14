"""Reconstruct a frozen dataclass instance from its canonical JSON payload.

``apex.domain.values.canonical_json_bytes`` deterministically serializes any
dataclass graph: dataclasses become field-name dicts, ``Enum`` members become
their ``.value``, ``Decimal`` becomes its exact string, and an aware
``datetime`` becomes a canonical UTC ISO-8601 string.  ``rehydrate`` is the
exact inverse of that encoding, driven by the target dataclass's own type
hints, so every new durable repository can store one ``payload`` JSON column
and restore an exact original instance without hand-writing a bespoke
reconstruction function per nested type.

This module holds no financial or evidence semantics of its own; it is a
narrow, stdlib-only serialization utility shared by the durable repositories
that use the ``payload``-column pattern established by
``apex.adapters.research_evidence_postgres``.
"""

from __future__ import annotations

import types
import typing
from dataclasses import fields as dataclass_fields
from dataclasses import is_dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum


class RehydrationError(ValueError):
    """A stored payload no longer matches the target dataclass shape."""


def rehydrate[T](cls: type[T], value: object) -> T:
    if not is_dataclass(cls):
        raise RehydrationError(f"{cls!r} is not a dataclass")
    if not isinstance(value, dict):
        raise RehydrationError("rehydration source must be a JSON object")
    hints = typing.get_type_hints(cls)
    try:
        kwargs = {
            field.name: _value(hints[field.name], value[field.name])
            for field in dataclass_fields(cls)
        }
    except KeyError as error:
        raise RehydrationError(f"payload is missing field {error}") from error
    return typing.cast(T, cls(**kwargs))


def _value(annotation: object, raw: object) -> object:
    origin = typing.get_origin(annotation)
    if origin is typing.Union or origin is types.UnionType:
        args = tuple(item for item in typing.get_args(annotation) if item is not type(None))
        if raw is None:
            if len(args) == len(typing.get_args(annotation)):
                raise RehydrationError("payload field is null but annotation is not optional")
            return None
        return _value(args[0], raw)
    if origin is tuple:
        if not isinstance(raw, list):
            raise RehydrationError("payload tuple field must be a JSON array")
        args = typing.get_args(annotation)
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_value(args[0], item) for item in raw)
        if len(args) != len(raw):
            raise RehydrationError("payload tuple arity does not match its annotation")
        return tuple(_value(arg, item) for arg, item in zip(args, raw, strict=True))
    if is_dataclass(annotation):
        return rehydrate(typing.cast(type, annotation), raw)
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return annotation(raw)
    if annotation is Decimal:
        if not isinstance(raw, str):
            raise RehydrationError("payload Decimal field must be a canonical string")
        return Decimal(raw)
    if annotation is datetime:
        if not isinstance(raw, str):
            raise RehydrationError("payload datetime field must be a canonical string")
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)
    if annotation in (bool, int, str) or annotation is object:
        return raw
    return raw
