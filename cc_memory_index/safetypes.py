"""The refusals and value types of safeio (DESIGN §5.2, §7.3); callers import them from safeio."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Union


class SafeIOError(Exception):
    """Base class of every refusal raised by safeio."""


class UnsafeEntry(SafeIOError):
    """The entry is a link, a reparse point, not a regular file, hard-linked, or a bad name."""


class ConcurrentEdit(SafeIOError):
    """The entry changed after it was fingerprinted."""


class LockTimeout(SafeIOError):
    """Another instance holds the store lock."""


class TooLarge(SafeIOError):
    """The entry is larger than the caller's limit."""


class RootChanged(SafeIOError):
    """The store root path no longer refers to the directory that was opened."""


@dataclass(frozen=True)
class Fingerprint:
    size: int
    sha256: str


class _AnyType:
    def __repr__(self) -> str:
        return "ANY"


ANY = _AnyType()
Expect = Union[Fingerprint, None, _AnyType]
