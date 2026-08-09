"""Configuration load/persist for webpty.

Behaviour parity with the previous JS implementation:
  * config lives under WEBPTY_DATA_DIR (default ~/.config/webpty on POSIX).
  * `roots` from disk are preserved — even an explicit [] (deny all) is kept;
    only configs that predate the field fall back to the default.
  * `tools` merge: user entries win over built-ins, user-added tools survive,
    and a tool set to null/false in the file is *disabled* (removed from the
    merged list; the marker is persisted so the disable survives restarts).
  * A corrupt config.json (bad JSON, or JSON that isn't an object — null,
    string, array) is backed up as config.json.broken-<ts> and defaults are
    used so the server always boots.
"""
from __future__ import annotations

import copy
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))


def _default_data_dir() -> str:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
        return os.path.join(base, "webpty")
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "webpty")


data_dir = (
    os.environ.get("WEBPTY_DATA_DIR")
    or os.environ.get("PTYHUB_DATA_DIR")
    or _default_data_dir()
)
logs_dir = os.path.join(data_dir, "logs")
config_path = os.path.join(data_dir, "config.json")

projects_root = (
    os.environ.get("WEBPTY_PROJECTS_ROOT")
    or os.environ.get("PTYHUB_PROJECTS_ROOT")
    or os.environ.get("CSMWEB_PROJECTS_ROOT")
    or os.path.abspath(os.path.join(_HERE, "..", ".."))
)


def effective_port(config_port) -> int:
    """Port resolution: env var > config.json > 4789 default."""
    raw_env = os.environ.get("WEBPTY_PORT") or os.environ.get("PTYHUB_PORT") or ""
    try:
        env_port = int(raw_env)
        if 0 < env_port < 65536:
            return env_port
    except (TypeError, ValueError):
        pass
    try:
        n = int(config_port)
        if 0 < n < 65536:
            return n
    except (TypeError, ValueError):
        pass
    return 4789


DEFAULT_TOOLS = {
    "claude": {"command": "claude", "defaultArgs": "--remote-control", "nameFlag": "-n"},
    "claude-chat": {"command": "claude", "defaultArgs": "", "engine": "agent",
                    "permissionMode": "bypassPermissions", "label": "Claude (chat)"},
    "codex": {"command": "codex", "defaultArgs": "", "nameFlag": None},
    "reasonix": {"command": "reasonix", "defaultArgs": "", "nameFlag": None},
    "opencode": {"command": "opencode", "defaultArgs": "", "nameFlag": None},
    "aider": {"command": "aider", "defaultArgs": "", "nameFlag": None},
    "gemini": {"command": "gemini", "defaultArgs": "", "nameFlag": None},
    "qwen": {"command": "qwen-code", "defaultArgs": "", "nameFlag": None},
    "cursor-agent": {"command": "cursor-agent", "defaultArgs": "", "nameFlag": None},
    "copilot": {"command": "copilot", "defaultArgs": "", "nameFlag": None},
    "agy": {"command": "agy", "defaultArgs": "", "nameFlag": None},
    "powershell": {"command": "powershell", "defaultArgs": "-NoLogo"},
    "bash": {"command": "bash", "defaultArgs": "", "nameFlag": None},
}

# Provider presets: each entry is {baseUrl, apiKey, models}. Tools reference
# a preset by name via their `provider` field; per-tool apiBaseUrl/apiKey
# override the preset. apiKey is plaintext here (local single-user tool);
# it is redacted in backup/migrate exports.
DEFAULT_PROVIDERS: dict[str, dict] = {
    "anthropic": {
        "baseUrl": "https://api.anthropic.com/v1",
        "apiKey": "",
        "models": ["claude-opus-4-8", "claude-sonnet-4-5", "claude-haiku-4-5"],
    },
    "openai": {
        "baseUrl": "https://api.openai.com/v1",
        "apiKey": "",
        "models": ["gpt-5.4", "gpt-5.2", "o4-mini"],
    },
    "deepseek": {
        "baseUrl": "https://api.deepseek.com/v1",
        "apiKey": "",
        "models": ["deepseek-v4-flash", "deepseek-v4-pro"],
    },
    "zai": {
        "baseUrl": "https://api.z.ai/api/coding/paas/v4",
        "apiKey": "",
        "models": ["zai-opencode-1", "zai-deepseek-v3"],
    },
    "opencode": {
        "baseUrl": "https://opencode.ai/zen/go/v1",
        "apiKey": "",
        "models": ["deepseek-v4-flash", "deepseek-v4-pro"],
    },
    "local": {
        "baseUrl": "http://127.0.0.1:11434/v1",
        "apiKey": "ollama",
        "models": [],
    },
}


def default_config() -> dict:
    return {
        # Secure-by-default: bind loopback only. Opening to 0.0.0.0 without
        # an authToken (or allowedLogins) is a zero-credential RCE surface —
        # any LAN/internet client could rewrite tools.command and spawn
        # arbitrary processes. Users who want remote access must explicitly
        # set bindHost AND enable a gate (see README).
        "bindHost": "127.0.0.1",
        "port": 4789,
        "roots": [projects_root],
        "extraFolders": [],
        "allowedLogins": [],
        "authToken": "",
        "tools": copy.deepcopy(DEFAULT_TOOLS),
        "providers": copy.deepcopy(DEFAULT_PROVIDERS),
        "restart": {"max_restarts": 3, "backoff_s": 10,
                    "stall_timeout_s": 900},
        "sessions": [],
    }


def ensure_data_dirs() -> None:
    os.makedirs(logs_dir, exist_ok=True)


def _migrate_legacy_data_dir() -> None:
    # Only meaningful on Windows (legacy ptyhub/CSMWeb configs).
    if os.path.exists(config_path) or sys.platform != "win32":
        return
    app_data = os.environ.get("APPDATA") or ""
    for legacy_name in ("ptyhub", "CSMWeb"):
        legacy_config = os.path.join(app_data, legacy_name, "config.json")
        if not os.path.exists(legacy_config):
            continue
        ensure_data_dirs()
        with open(legacy_config, "r", encoding="utf-8") as f:
            data = f.read()
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(data)
        legacy_logs = os.path.join(app_data, legacy_name, "logs")
        if os.path.isdir(legacy_logs):
            import shutil
            try:
                shutil.copytree(legacy_logs, logs_dir, dirs_exist_ok=True)
            except OSError:
                pass
        print(f"[webpty] migrated legacy {legacy_name} config → {config_path}")
        return


def _backup_broken_config(reason: str) -> None:
    try:
        backup = f"{config_path}.broken-{int(time.time() * 1000)}"
        with open(config_path, "rb") as src, open(backup, "wb") as dst:
            dst.write(src.read())
        print(f"[webpty] {reason} — backed up to {backup} and starting with defaults")
    except OSError:
        print(f"[webpty] {reason} — starting with defaults")


def load_config() -> dict:
    ensure_data_dirs()
    _migrate_legacy_data_dir()

    if not os.path.exists(config_path):
        cfg = default_config()
        save_config(cfg)
        return cfg

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError) as err:
        _backup_broken_config(f"config.json is corrupt ({err})")
        raw = {}

    # JSON may parse fine yet not be a usable config object (null/string/array).
    if not isinstance(raw, dict):
        _backup_broken_config("config.json is not a config object")
        raw = {}

    # --- tools merge -------------------------------------------------------
    raw_tools = raw.get("tools") if isinstance(raw.get("tools"), dict) else {}
    merged_tools: dict = {}
    for key in set(DEFAULT_TOOLS.keys()) | set(raw_tools.keys()):
        if key not in raw_tools:
            # Built-in tool, untouched by the user — take the default as-is.
            merged_tools[key] = copy.deepcopy(DEFAULT_TOOLS[key])
            continue
        user_val = raw_tools[key]
        if user_val is None or user_val is False:
            continue  # disabled by user
        base = copy.deepcopy(DEFAULT_TOOLS.get(key, {}))
        if isinstance(user_val, dict):
            base.update(user_val)
        # Audit I2: command must be a non-empty string — a number/null would
        # crash resolve_command (os.path.isabs(123)) with a confusing error.
        if not isinstance(base.get("command"), str) or not base["command"].strip():
            base["command"] = (DEFAULT_TOOLS.get(key) or {}).get("command", "")
        merged_tools[key] = base
    # Persist disable markers so a disabled tool stays disabled across restarts.
    for key, val in raw_tools.items():
        if val is None or val is False:
            merged_tools[key] = val

    merged = default_config()
    merged.update({k: v for k, v in raw.items() if k != "tools"})
    merged["tools"] = merged_tools
    merged["sessions"] = raw.get("sessions", []) if isinstance(raw.get("sessions"), list) else []
    # Audit I1: restart/budget must be dicts — a hand-edited string would
    # make _maybe_restart / CostTracker crash with AttributeError and
    # silently disable auto-restart + budget alerts.
    for seg in ("restart", "budget"):
        if not isinstance(merged.get(seg), dict):
            merged[seg] = default_config().get(seg)

    # providers: user presets override built-ins; non-dict → defaults.
    raw_providers = raw.get("providers") if isinstance(raw.get("providers"), dict) else {}
    merged_providers = copy.deepcopy(DEFAULT_PROVIDERS)
    for name, p in raw_providers.items():
        if isinstance(p, dict):
            base = dict(merged_providers.get(name, {}))
            base.update({k: v for k, v in p.items()
                         if k in ("baseUrl", "apiKey", "models")})
            merged_providers[name] = base
    merged["providers"] = merged_providers

    # roots: keep user-configured roots (even explicit []) — only fall back
    # when the field is absent.
    if isinstance(raw.get("roots"), list):
        merged["roots"] = [os.path.abspath(str(p)) for p in raw["roots"]]
    else:
        merged["roots"] = [projects_root]

    if isinstance(raw.get("extraFolders"), list):
        merged["extraFolders"] = [
            os.path.abspath(str(p)) for p in raw["extraFolders"] if isinstance(p, str) and p
        ]
    else:
        merged["extraFolders"] = []

    if isinstance(raw.get("allowedLogins"), list):
        merged["allowedLogins"] = [
            str(s).lower() for s in raw["allowedLogins"] if isinstance(s, str) and s
        ]
    else:
        merged["allowedLogins"] = []

    if isinstance(raw.get("authToken"), str):
        merged["authToken"] = raw["authToken"]
    else:
        merged["authToken"] = ""

    # Persist merged form so newly added defaults (e.g. new tools) appear on disk.
    save_config(merged)
    return merged


def save_config(config: dict) -> None:
    try:
        ensure_data_dirs()
        # Atomic write: unique tmp (pid suffix) so a concurrent restore's
        # _atomic_write_json can never truncate the same file mid-write.
        # Same-process safety relies on the single-threaded event loop (no
        # concurrent save within one process); pid keeps cross-process writers
        # apart.
        tmp = f"{config_path}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, config_path)
        # Audit M3: config holds authToken — never leave it world-readable
        # even if a previous version created it with looser perms.
        try:
            os.chmod(config_path, 0o600)
        except OSError:
            pass
    except OSError as err:
        # Audit M2: a full disk / permission error must not 500 a session
        # create/remove (memory is already updated). Log it — the error
        # ring buffer surfaces it in the UI; on restart the on-disk state
        # simply wins, which is predictable.
        from logging_util import log_error
        log_error("config-save", err)


def safe_name(value: str) -> str:
    import re

    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value or "session"))
    return cleaned[:80]
