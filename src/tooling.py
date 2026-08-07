"""Argument splitting for tool spawn profiles.

Direct port of the webpty JS splitter: quotes group whitespace and are
removed; an empty quoted argument is preserved; backslashes are literal on
POSIX (a lone `\\` is a path char, e.g. `dir\\file`), and on Windows a
backslash outside quotes escapes the next char while inside quotes it stays
literal (CommandLineToArgvW semantics — `"C:\\temp\\new"` is kept verbatim).
"""
from __future__ import annotations

import os
import shutil

_BACKSLASH_ESCAPES = os.name == "nt"


def split_args(input_: str) -> list[str]:
    """Split an args string like a shell, preserving empty quoted args.

    Examples:
        'a "" b'            -> ['a', '', 'b']
        '--name "my proj"'  -> ['--name', 'my proj']
        'a\\b c'             -> ['a\\b', 'c']   (backslash literal on POSIX)
        ''                  -> []
    """
    args: list[str] = []
    cur: list[str] = []
    quote: str | None = None
    escaped = False
    saw_quote = False

    for ch in str(input_ or ""):
        if escaped:
            cur.append(ch)
            escaped = False
            continue
        if _BACKSLASH_ESCAPES and quote is None and ch == "\\":
            escaped = True
            continue
        if quote is not None:
            if ch == quote:
                quote = None
                saw_quote = True
            else:
                cur.append(ch)
            continue
        if ch in ('"', "'"):
            quote = ch
            saw_quote = True
            continue
        if ch.isspace():
            if cur or saw_quote:
                args.append("".join(cur))
                cur = []
                saw_quote = False
            continue
        cur.append(ch)

    if cur or saw_quote:
        args.append("".join(cur))
    return args


def resolve_command(command: str | None) -> str | None:
    """Resolve a tool command to an absolute path when possible.

    Absolute paths that exist are returned as-is; bare names are resolved via
    PATH (shutil.which); anything else falls back to the original string
    (spawn will surface the error at runtime).
    """
    if not command:
        return None
    if os.path.isabs(command) and os.path.exists(command):
        return command
    found = shutil.which(command)
    return found or command
