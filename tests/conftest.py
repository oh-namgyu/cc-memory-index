from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Callable

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture
def symlink() -> Callable[[Path, Path], None]:
    """Create link -> target, or skip where the OS does not allow it (Windows without privilege)."""

    def make(link: Path, target: Path) -> None:
        try:
            link.symlink_to(target, target_is_directory=target.is_dir())
        except (OSError, NotImplementedError) as e:
            pytest.skip(f"cannot create symlinks here: {e}")

    return make
