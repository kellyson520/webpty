"""Path helpers — normalization and root containment.

Windows paths are case-insensitive; POSIX paths are case-sensitive. We only
fold case on win32 so `/root/Projects` and `/root/projects` stay distinct on
Linux/macOS.
"""
from __future__ import annotations

import os
import sys


def case_fold(p: str) -> str:
    return p.lower() if sys.platform == "win32" else p


def normalize_fs_path(input_: str) -> str:
    """Resolve a path and strip trailing separators.

    The filesystem root must stay itself ('/' or 'C:\\') — never reduce to ''.
    """
    p = os.path.abspath(os.path.expanduser(str(input_ or "")))
    while len(p) > 1 and (p.endswith("/") or p.endswith("\\")):
        p = p[:-1]
    return case_fold(p)


def _is_root(p: str) -> bool:
    return p == os.path.dirname(p)


def is_path_under_roots(candidate: str, roots: list[str]) -> bool:
    """True when candidate is `root` itself or lives inside one of `roots`.

    The filesystem root ('/') contains everything; the usual `base + sep`
    prefix check would produce '//' and never match, so it is special-cased.
    """
    resolved = normalize_fs_path(candidate)
    for root in roots:
        base = normalize_fs_path(root)
        if resolved == base:
            return True
        if _is_root(base):
            # '/' or 'C:\\' contains every path.
            return True
        if resolved.startswith(base + os.sep):
            return True
    return False


def public_dir() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "public"))


def package_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
