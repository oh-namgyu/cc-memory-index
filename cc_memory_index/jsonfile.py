"""Strict JSON objects from the store's bookkeeping files: configuration, data files, render
record and migration manifest (DESIGN §6, §8, §9).

Strict means: UTF-8 with at most one leading byte order mark, one JSON object, no duplicate key,
no NaN or infinite number, and not nested deeper than the parser can follow. Anything else raises
JSONFileError, which each caller maps to its own exit code.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from .safeio import Fingerprint, SafeIOError, StoreDir
from .text import quote, strip_bom


class JSONFileError(Exception):
    """A store JSON file cannot be read, is not strict JSON, or does not hold one object."""


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    obj: dict[str, object] = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f"duplicate key {quote(key)}")
        obj[key] = value
    return obj


def _finite(token: str) -> float:
    value = float(token)
    if abs(value) == float("inf"):
        raise ValueError(f"number {quote(token)} is out of range")
    return value


def _no_constant(token: str) -> object:
    raise ValueError(f"{token} is not a JSON number")


def parse_object(data: bytes, name: str) -> dict[str, Any]:
    """The JSON object data holds, read strictly; name says which file it is in messages."""
    try:
        obj = json.loads(strip_bom(data.decode("utf-8")), object_pairs_hook=_unique, parse_float=_finite,
                         parse_constant=_no_constant)
    except (ValueError, RecursionError) as e:  # RecursionError: nested too deeply
        raise JSONFileError(f"{name} is not valid JSON: {e}") from e
    if not isinstance(obj, dict):
        raise JSONFileError(f"{name} must contain a JSON object")
    return obj


def load_object(sd: StoreDir, name: str, max_bytes: int) -> tuple[Optional[dict[str, Any]], Optional[Fingerprint]]:
    """The object in a store JSON file and its fingerprint; (None, None) when the file is absent."""
    if sd.kind(name) == "missing":
        return None, None
    try:
        data, fp = sd.read(name, max_bytes)
    except SafeIOError as e:
        raise JSONFileError(f"cannot read {name}: {e}") from e
    return parse_object(data, name), fp
