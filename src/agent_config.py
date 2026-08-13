"""Read & precisely edit each agent CLI's own local config file.

This is separate from webpty's config.json — it manages the agent's
native configuration (codex ~/.codex/config.toml, claude
~/.claude/settings.json, reasonix ~/.reasonix/config.toml, ...).

Safety:
- Only whitelisted paths under $HOME are touched (realpath check).
- Files are read/written with size caps (read ≤ 256KB).
- TOML edits are line-level: the target key's line(s) are replaced and
  everything else (comments, ordering, inline notes) is preserved.
- JSON edits parse + rewrite with indent=2 (JSON has no comments).
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

# Python 3.11 ships tomllib in the stdlib; on 3.10 (Ubuntu 22.04 default
# python3) fall back to the vendored tomli backport (src/tomli).
try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover — Python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]

_HOME = os.path.expanduser("~")

# tool -> list of candidate config paths (first existing one wins)
AGENT_CONFIG_PATHS: dict[str, list[str]] = {
    "codex": [os.path.join(_HOME, ".codex", "config.toml")],
    "reasonix": [os.path.join(_HOME, ".reasonix", "config.toml")],
    "claude": [os.path.join(_HOME, ".claude", "settings.json")],
    "opencode": [
        os.path.join(_HOME, ".config", "opencode", "opencode.json"),
        os.path.join(_HOME, ".opencode.json"),
    ],
    "aider": [
        os.path.join(_HOME, ".aider.conf.yml"),
        os.path.join(_HOME, ".config", "aider", "conf.yml"),
    ],
    "gemini": [
        os.path.join(_HOME, ".gemini", "settings.json"),
        os.path.join(_HOME, ".config", "gemini", "settings.json"),
    ],
    "copilot": [os.path.join(_HOME, ".config", "github-copilot", "hosts.json")],
    "cursor-agent": [os.path.join(_HOME, ".config", "cursor-agent", "config.toml")],
    "agy": [os.path.join(_HOME, ".config", "agy", "config.toml")],
}

MAX_READ_BYTES = 256 * 1024

# ---- editable key maps ---------------------------------------------------
# TOML tools: key name in the file -> how to match & replace its line(s).
# Each entry: (regex that matches the whole key line, value formatter).
# The regex must anchor ^ and match the value part as group 1.
def _SECTION_KEYS(section_re: str, keys: dict[str, tuple[str, str]]) -> tuple:
    """Build a section-scoped key spec: (section_re, {key: (line_re, fmt)}).
    Used for dotted keys like `model_providers.<id>.base_url` whose target
    lines live inside a named [section] rather than at top level.
    """
    return (section_re, keys)

TOML_KEYS: dict[str, dict[str, tuple[str, str]]] = {
    "codex": {
        "model": (r'^model\s*=\s*".*?"', 'model = "{}"'),
        "base_url": (r'^openai_base_url\s*=\s*".*?"', 'openai_base_url = "{}"'),
        "api_key": (r'^api_key\s*=\s*".*?"', 'api_key = "{}"'),
        "model_provider": (r'^model_provider\s*=\s*".*?"', 'model_provider = "{}"'),
        "temperature": (r'^temperature\s*=\s*"?[^"]*"?', 'temperature = {}'),
        "proxy": (r'^proxy\s*=\s*".*?"', 'proxy = "{}"'),
        # Section-scoped provider keys: model_providers.<id>.base_url / api_key
        # rewrite inside [model_providers."<id>"] (see _set_toml_section_key).
        "model_providers": _SECTION_KEYS(
            r'^\[model_providers\.(?:"([^"]+)"|([^\]]+))\]',
            {"base_url": (r'^\s*base_url\s*=\s*".*?"', 'base_url = "{}"'),
             "api_key": (r'^\s*api_key\s*=\s*".*?"', 'api_key = "{}"')}),
    },
    "reasonix": {
        "model": (r'^default_model\s*=\s*".*?"', 'default_model = "{}"'),
        "language": (r'^language\s*=\s*".*?"', 'language = "{}"'),
        "effort": (r'^effort\s*=\s*"[a-z]+"', 'effort = "{}"'),
        "api_key": (r'^api_key\s*=\s*".*?"', 'api_key = "{}"'),
        "base_url": (r'^base_url\s*=\s*".*?"', 'base_url = "{}"'),
        "provider": (r'^provider\s*=\s*".*?"', 'provider = "{}"'),
    },
    "cursor-agent": {
        "model": (r'^model\s*=\s*".*?"', 'model = "{}"'),
        "base_url": (r'^base_url\s*=\s*".*?"', 'base_url = "{}"'),
        "api_key": (r'^api_key\s*=\s*".*?"', 'api_key = "{}"'),
    },
    "agy": {
        "model": (r'^model\s*=\s*".*?"', 'model = "{}"'),
        "base_url": (r'^base_url\s*=\s*".*?"', 'base_url = "{}"'),
        "api_key": (r'^api_key\s*=\s*".*?"', 'api_key = "{}"'),
    },
}

# JSON tools: key name -> dotted path inside the JSON object.
JSON_KEYS: dict[str, dict[str, str]] = {
    "claude": {
        "base_url": "env.ANTHROPIC_BASE_URL",
        "api_key": "env.ANTHROPIC_AUTH_TOKEN",
        "theme": "theme",
    },
    "gemini": {
        "api_key": "apiKey",
        "base_url": "baseUrl",
    },
    "copilot": {
        "api_key": "github.com.oauth_token",
    },
    "opencode": {
        # provider is a string (built-in id) or an object; options.baseURL
        # is where custom endpoints live. Setting base_url/api_key converts
        # a string provider into the object form (see _set_json_path).
        "model": "model",
        "base_url": "provider.options.baseURL",
        "api_key": "provider.options.apiKey",
    },
}

# YAML tools (read-only for now: no stdlib yaml writer; edits rejected)
YAML_TOOLS = {"aider"}


def _real_home() -> str:
    return os.path.realpath(_HOME)


def extra_roots() -> list[str]:
    """Additional allowed config roots from WEBPTY_AGENT_CONFIG_ROOTS
    (os.pathsep-separated). Lets webpty touch agent configs that live
    outside $HOME (custom CODEX_HOME, shared configs on other mounts, …)
    without pip-deps — direct external contact, opt-in per deployment."""
    raw = os.environ.get("WEBPTY_AGENT_CONFIG_ROOTS") or ""
    out: list[str] = []
    for part in raw.split(os.pathsep):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(os.path.realpath(part))
        except OSError:
            continue
    return out


def _allowed_roots() -> list[str]:
    roots = [_real_home()]
    for r in extra_roots():
        if r not in roots:
            roots.append(r)
    return roots


def _under_allowed_roots(p: str) -> bool:
    return any(p == r or p.startswith(r + os.sep) for r in _allowed_roots())


def config_path(tool: str) -> str | None:
    """Return the existing config path for a tool, or None.

    Candidates must live under $HOME or a WEBPTY_AGENT_CONFIG_ROOTS entry.
    """
    for cand in AGENT_CONFIG_PATHS.get(tool, []):
        p = os.path.realpath(cand)
        if not _under_allowed_roots(p):
            continue  # outside every allowed root — skip
        if os.path.isfile(p):
            return p
    return None


def list_configs() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for tool, cands in AGENT_CONFIG_PATHS.items():
        path = config_path(tool)
        out[tool] = {
            "exists": path is not None,
            "path": path,
            "format": "toml" if tool in TOML_KEYS and path else (
                "json" if tool in JSON_KEYS and path else (
                    "yaml" if tool in YAML_TOOLS and path else None)),
            "editable": bool(path) and (tool in TOML_KEYS or tool in JSON_KEYS),
        }
    return out


def _read_capped(path: str) -> tuple[str | None, str | None]:
    """Read a config file with the size cap; returns (content, error)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            size = os.fstat(f.fileno()).st_size
            if size > MAX_READ_BYTES:
                return None, "config too large"
            return f.read(), None
    except OSError as err:
        return None, str(err)


def read_config(tool: str, path: str | None = None) -> dict[str, Any]:
    if path:
        # Explicit path: direct contact with any config file the user
        # points at (size cap + secret masking still apply).
        target = os.path.abspath(str(path))
        if not os.path.isfile(target):
            return {"ok": False, "error": "config file not found"}
    else:
        target = config_path(tool)
        if not target:
            return {"ok": False, "error": "no config file"}
    content, err = _read_capped(target)
    if err is not None:
        return {"ok": False, "error": err}
    # Mask secrets in the raw view (Issue 2.2): keys like
    # api_key / ANTHROPIC_AUTH_TOKEN / password must not be sent to the
    # browser in plaintext. TOML `key = "value"` and JSON "key": "value".
    content = _mask_secret_values(content or "")
    return {"ok": True, "path": target, "content": content}


def read_agent_env_secret(name: str) -> str | None:
    """Look up an agent-named env secret (e.g. CODEX_API_KEY, DEEPSEEK_API_KEY):
    the process environment first (webpty loads the agents' env files), then
    Reasonix's global env file (~/.reasonix/.env). Returns None when absent."""
    if not name:
        return None
    val = os.environ.get(name)
    if val:
        return val
    for cand in (os.path.join(_HOME, ".reasonix", ".env"),
                 os.path.join(_HOME, ".reasonix", "env")):
        try:
            with open(cand, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    if k.strip() == name:
                        return v.strip().strip("'\"") or None
        except OSError:
            continue
    return None


def read_native(tool: str, path: str | None = None) -> dict[str, Any] | None:
    """Parse the agent's native config into a dict (no masking).

    Returns None when the file is missing/unparseable — used by the sync
    import (server-side) to mirror the agent CLI's own settings."""
    target = path or config_path(tool)
    if not target or not os.path.isfile(target):
        return None
    content, err = _read_capped(target)
    if err is not None or content is None:
        return None
    try:
        if tool in TOML_KEYS:
            data = tomllib.loads(content)
        elif tool in JSON_KEYS:
            data = json.loads(content)
        else:
            return None
    except Exception:  # noqa: BLE001 — unparseable native config
        return None
    if not isinstance(data, dict):
        return None
    return {"path": target, "data": data}


_SECRET_KEY_RE = re.compile(
    r'(?im)^(\s*["\']?(?:api_?key|auth_token|password|token|secret)'
    r'["\']?\s*[:=]\s*["\'])([^"\']+)(["\'])')
_SECRET_KEY_RE2 = re.compile(
    r'(?i)("(?:apiKey|authToken|password|token|secret)"\s*:\s*")([^"]+)(")')


def _mask_secret_values(text: str) -> str:
    """Replace secret values with **** (keep first 4 + last 4 if long)."""

    def _mask(m):
        val = m.group(2)
        masked = "****" + val[-4:] if len(val) > 8 else "****"
        return m.group(1) + masked + m.group(3)

    out = _SECRET_KEY_RE.sub(_mask, text)
    return _SECRET_KEY_RE2.sub(_mask, out)


def _esc_toml_str(value: str) -> str:
    """Proper TOML string escaping: backslash, quote, control chars."""
    return (value.replace("\\", "\\\\")
                 .replace('"', '\\"')
                 .replace("\n", "\\n")
                 .replace("\r", "\\r")
                 .replace("\t", "\\t"))


def _replace_toml(content: str, key: str, fmt: tuple[str, str], value: str) -> tuple[str, bool]:
    """Replace a TOP-LEVEL key's line in TOML text; preserve everything else.

    Uses tomllib to find the line where the top-level key is defined, so a
    key inside some [section] (e.g. [projects."/mnt/TG-ONE"] model=...) is
    never mistaken for the top-level one. Escapes the value correctly.
    """
    pattern, template = fmt
    new_line = template.format(_esc_toml_str(value))
    lines = content.splitlines(keepends=True)

    # Refuse to edit files that aren't parseable TOML — never corrupt an
    # agent's native config further.
    try:
        parsed = tomllib.loads(content)
    except Exception:  # noqa: BLE001 — unparseable file: refuse to edit
        return content, False

    # Audit M3: if the target key already exists with a non-single-string
    # value (array, multiline string, table), a line-level rewrite can't
    # replace it safely — the regex would miss and we'd append a duplicate
    # key that the TOML parser resolves to the OLD value (silent no-op).
    # Refuse loudly instead of returning a false "replaced".
    existing = parsed.get(key)
    if existing is not None and not isinstance(existing, str):
        raise ValueError(
            f"key {key!r} 的值是数组/多行/表格类型，行级替换不安全——"
            "请直接编辑配置文件（webpty 已保留 .bak 备份）")

    # Line scan that only touches the top-level scope: track when we're
    # inside a [section] (lines starting with '[' after whitespace) and only
    # replace key lines outside any section.
    in_section = False
    out: list[str] = []
    replaced = False
    for line in lines:
        stripped = line.strip()
        if not replaced and not in_section and re.match(pattern, stripped):
            indent = line[: len(line) - len(line.lstrip())]
            out.append(f"{indent}{new_line}\n")
            replaced = True
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            in_section = True
        out.append(line)
    if not replaced:
        # Key absent at top level: append before the first [section] header
        # (or at EOF if there is none) so it stays a top-level key.
        insert_at = len(out)
        for i, line in enumerate(out):
            if line.strip().startswith("["):
                insert_at = i
                break
        out.insert(insert_at, f"{new_line}\n")
        replaced = True
    return "".join(out), replaced


def _set_toml_section_key(content: str, section_re: str, dotted_key: str,
                          sub_keys: dict[str, tuple[str, str]],
                          raw: str) -> tuple[str, bool]:
    """Rewrite a key inside a named TOML section.

    `dotted_key` is "model_providers.<id>.base_url" — the middle component
    is the provider id that selects which [model_providers."<id>"] section
    to edit. Falls back to the FIRST matching section when the id is not
    given (bare "model_providers.base_url").
    """
    parts = dotted_key.split(".")
    target = parts[-1]
    if target not in sub_keys:
        return content, False
    line_re, fmt = sub_keys[target]

    # Parse section headers to know which block each line belongs to.
    lines = content.splitlines(keepends=True)
    # Collect id -> line range.
    sections: list[tuple[str, int, int]] = []  # (id, start, end-exclusive)
    cur_id: str | None = None
    cur_start = -1
    for i, line in enumerate(lines):
        m = re.match(section_re, line.strip())
        if m:
            if cur_id is not None:
                sections.append((cur_id, cur_start, i))
            cur_id = m.group(1) or m.group(2)
            cur_start = i
    if cur_id is not None:
        sections.append((cur_id, cur_start, len(lines)))
    if not sections:
        return content, False

    want = parts[1] if len(parts) >= 3 else None
    target_id = want if any(s[0] == want for s in sections) else None
    block = next((s for s in sections if target_id is None or s[0] == target_id),
                 None)
    if block is None:
        return content, False
    _, start, end = block

    out = list(lines)
    replaced = False
    # Values must be TOML-escaped like top-level ones (a quote/backslash in
    # the value otherwise corrupts the file), and the replacement must be a
    # function so re.sub treats it literally (no \1 group escapes).
    esc = _esc_toml_str(raw)
    for i in range(start, end):
        stripped = out[i].strip()
        if re.match(line_re, stripped):
            out[i] = re.sub(line_re, lambda _m: fmt.format(esc), out[i])
            replaced = True
            break
    if not replaced:
        # Key absent in the block: append inside it (before next section or EOF).
        out.insert(end, f"{target} = {fmt.format(esc).split(' = ', 1)[1]}\n")
        replaced = True
    return "".join(out), replaced


def _set_json_path(obj: Any, path: str, value: Any) -> bool:
    parts = path.split(".")
    cur = obj
    for i, part in enumerate(parts):
        if not isinstance(cur, dict):
            return False
        if i == len(parts) - 1:
            cur[part] = value
            return True
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            # Convert non-dict intermediates (e.g. opencode's string
            # "provider") into objects so deeper keys can be set.
            nxt = {}
            cur[part] = nxt
        cur = nxt
    return False


def update_config(tool: str, values: dict[str, Any],
                  path: str | None = None) -> dict[str, Any]:
    """Precisely replace the given keys in the tool's config file.

    `path` is an optional explicit target for direct external contact —
    any existing file can be edited (size cap, .bak backup, atomic write
    and TOML/JSON validity checks still apply)."""
    if path:
        target = os.path.abspath(str(path))
        if not os.path.isfile(target):
            return {"ok": False, "error": "config file not found"}
    else:
        target = config_path(tool)
        if not target:
            return {"ok": False, "error": "no config file"}
    fmt = "toml" if tool in TOML_KEYS else ("json" if tool in JSON_KEYS else None)
    if fmt is None:
        return {"ok": False, "error": f"unsupported format for {tool}"}

    try:
        with open(target, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError as err:
        return {"ok": False, "error": str(err)}

    changed: list[str] = []
    if fmt == "toml":
        try:
            for key, raw in values.items():
                # Section-scoped keys arrive dotted (model_providers.<id>.base_url).
                # Resolve against the spec BEFORE the exact-key membership check.
                spec = None
                if key in TOML_KEYS[tool]:
                    spec = TOML_KEYS[tool][key]
                else:
                    prefix = key.split(".", 1)[0]
                    if prefix in TOML_KEYS[tool] \
                            and isinstance(TOML_KEYS[tool][prefix], tuple) \
                            and len(TOML_KEYS[tool][prefix]) == 2 \
                            and isinstance(TOML_KEYS[tool][prefix][1], dict):
                        spec = TOML_KEYS[tool][prefix]
                if spec is None:
                    continue
                # Section-scoped keys: "model_providers.<id>.base_url" — spec is
                # (section_re, {key: (line_re, fmt)}) from _SECTION_KEYS.
                if isinstance(spec, tuple) and len(spec) == 2 \
                        and isinstance(spec[1], dict):
                    sec_re, sub_keys = spec
                    content, replaced = _set_toml_section_key(
                        content, sec_re, key, sub_keys, str(raw))
                    if not replaced:
                        return {"ok": False,
                                "error": f"cannot edit {key}: provider section not found"}
                    changed.append(key)
                    continue
                # temperature 期望 TOML 数值:非数值拒绝写入(避免静默写入
                # 字符串使 codex 配置无效)。
                if key == "temperature":
                    try:
                        float(raw)
                    except (TypeError, ValueError):
                        return {"ok": False, "error": "temperature must be numeric"}
                content, replaced = _replace_toml(
                    content, key, spec, str(raw))
                if not replaced:
                    return {"ok": False,
                            "error": f"cannot edit {key}: file is not valid TOML"}
                changed.append(key)
        except ValueError as err:
            # Audit M3: _replace_toml refuses array/multiline/table values.
            return {"ok": False, "error": str(err)}
    else:  # json
        try:
            obj = json.loads(content)
        except json.JSONDecodeError as err:
            return {"ok": False, "error": f"invalid json: {err}"}
        if not isinstance(obj, dict):
            return {"ok": False, "error": "config root is not an object"}
        for key, raw in values.items():
            if key not in JSON_KEYS[tool]:
                continue
            # strings that look empty are kept as-is (user typed "")
            val: Any = raw
            if _set_json_path(obj, JSON_KEYS[tool][key], val):
                changed.append(key)
        content = json.dumps(obj, indent=2, ensure_ascii=False) + "\n"

    if not changed:
        return {"ok": False, "error": "no supported keys provided"}

    # Atomic write: temp file + rename, keep original permissions.
    # Audit M3: keep a .bak of the pre-edit file so a mistaken key write
    # (bad api_key etc.) can always be reverted by hand.
    try:
        import shutil
        shutil.copy2(target, target + ".bak")
    except OSError:
        pass  # backup is best-effort
    try:
        mode = os.stat(target).st_mode & 0o777
        tmp = f"{target}.webpty-tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except OSError as err:
        return {"ok": False, "error": str(err)}

    return {"ok": True, "changed": changed, "path": target}


def _redact(value: str) -> str:
    """Mask a secret for display (keep first 6 chars)."""
    if len(value) <= 10:
        return "••••"
    return value[:6] + "…" + value[-4:]
