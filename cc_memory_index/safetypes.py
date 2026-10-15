"""The refusals and value types of safeio (DESIGN §5.2, §7.3); callers import them from safeio."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Iterable, Literal, Union

EntryKind = Literal["file", "link", "dir", "other"]
Kind = Literal["file", "link", "dir", "other", "missing"]
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


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
    """Size and SHA-256 (lowercase hex) of a file's content: the only hash this tool computes."""

    size: int
    sha256: str

    @classmethod
    def of_pieces(cls, pieces: Iterable[bytes]) -> Fingerprint:
        """The fingerprint of the content that pieces make up, read once."""
        h = hashlib.sha256()
        size = 0
        for piece in pieces:
            h.update(piece)
            size += len(piece)
        return cls(size, h.hexdigest())

    @classmethod
    def of(cls, data: bytes) -> Fingerprint:
        return cls.of_pieces((data,))


def is_sha256(value: object) -> bool:
    """True for a SHA-256 written as Fingerprint.sha256 is (64 lowercase hex digits)."""
    return isinstance(value, str) and _SHA256.match(value) is not None


class _AnyType:
    def __repr__(self) -> str:
        return "ANY"


ANY = _AnyType()
Expect = Union[Fingerprint, None, _AnyType]
