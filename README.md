# webpty

**v1.0.0** · [GitHub kellyson520/webpty](https://github.com/kellyson520/webpty)

A multi-session web terminal that supervises long-running CLI agents
(`claude`, `codex`, `reasonix`, `opencode`, `aider`, `pwsh`, …) per
project folder. Open any browser on your trusted network — desktop or
phone — and switch between live sessions with a swipe.

> Built for the workflow: *"My PC has an AI agent CLI running in each of
> my project folders. I want to reach whichever one I need from my
> phone."*

## What's new in 1.0.0

- **Reasonix & mainstream agent tools** — preconfigured profiles for
  `reasonix`, `opencode`, `aider`, `gemini`, `qwen-code`, `cursor-agent`,
  `copilot` (see [Configuration](#configuration)). Opening a folder with
  no session auto-selects `reasonix` (or your last-used tool for that
  folder).
- **Token gate (access control)** — optional `authToken`: every
  non-localhost request must present it via `Authorization: Bearer …`,
  `?token=…` or cookie; the UI shows a one-time unlock screen. See
  [Security](#security).
- **Create projects from the UI** — new-session drawer creates a folder
  under the projects root (optionally `git init`ed) via
  `POST /api/projects/create`, with path-traversal protection.
- **Faster startup** — initial page load fetches config / projects /
  sessions in parallel and xterm vendor assets are served with
  immutable cache headers.
- **Bug fixes** — user `roots` are preserved on reload (explicit `[]` =
  deny-all is honored), POSIX args no longer lose backslashes,
  case-folded project dedupe is platform-aware.
- **Localized UI** — menu, sort controls, placeholder and quick-key
  labels in Simplified Chinese.

## What it gives you

- **Per-project supervisors** — one PTY per registered folder, kept alive
  across browser disconnects.
- **Auto-resume conversations** — for `claude`, if a project already has
  a JSONL log in `~/.claude/projects/`, `webpty` prepends `-c` on
  respawn so the chat continues instead of starting fresh.
- **Broad agent-tool support** — preconfigured profiles for `reasonix`,
  `codex`, `opencode`, `aider`, `gemini`, `qwen-code`, `cursor-agent`,
  `copilot`, `claude` and more. Install any of them and it appears in the
  new-session picker; opening a folder with no session auto-selects
  `reasonix` (or your last-used tool for that folder).
- **Create projects from the UI** — new-session drawer has a *新建*
  field that makes a folder under the projects root (optionally
  `git init`ed) and drops you straight into it.
- **Mobile-first UI** — full-screen-per-session, horizontal swipe to
  switch, kebab menu (`退出` / `清屏` / `压缩上下文` / other sessions /
  add).
- **Optional access gate** — set `authToken` in config (or run the
  Token Gate flow in the UI) and every non-localhost request must
  present it; the Tailscale identity gate is still supported as an
  alternative. See [Security](#security).
- **Quick PowerShell sessions** — menu shortcut spawns a `pwsh` or
  elevated `pwsh` in the current session's CWD.
- **Built on Microsoft's ConPTY via `node-pty`** — same battle-tested
  PTY layer that VS Code uses.

## Install & deploy (script, no npm release)

webpty is deployed from the git repo, not from an npm package — the script
always syncs to the latest code, so the running version is never frozen.

### One-command deploy

Run from a webpty checkout (or clone + install in one go):

```sh
git clone https://github.com/kellyson520/webpty.git
cd webpty
./install.sh            # npm ci (deps only) → writes a systemd unit → starts the service
```

Tuning knobs (flags or env vars):

```sh
./install.sh \
  --port=8080 \                    # or WEBPTY_PORT=8080
  --bind=0.0.0.0 \                 # or WEBPTY_BIND_HOST=0.0.0.0
  --projects-root=/srv/projects \  # or WEBPTY_PROJECTS_ROOT=/srv/projects
  --user=webpty                    # or WEBPTY_USER=webpty (dedicated account)
```

Remote one-shot:

```sh
curl -sL https://github.com/kellyson520/webpty/archive/refs/heads/main.tar.gz \
  | tar xz && cd webpty-main && ./install.sh
```

### Keeping everything in sync (not locked to a version)

Re-running the script is idempotent: it `git pull`s the latest code,
reinstalls deps and restarts the service — webpty always tracks `main`.

```sh
cd webpty
./install.sh            # pull latest webpty code + restart
./install.sh --update-cli   # update installed agent CLIs (reasonix, codex,
                            # claude-code, opencode, aider, gemini) via npm -g
```

`--update-cli` only touches CLIs that are already installed globally — it
never auto-installs new ones. Remove everything with `./install.sh --uninstall`.

Service management: `journalctl -u webpty.service -f`, `systemctl restart webpty`.

## Run

```sh
webpty
# → [webpty] listening on http://0.0.0.0:4789
# → [webpty] config:    %APPDATA%\webpty\config.json   (Windows)
#                       ~/.config/webpty/config.json   (macOS / Linux)
```

Open `http://<host>:4789/` from a browser on the same trusted network.

Port can be overridden at boot without editing config:

```sh
WEBPTY_PORT=8080 webpty
```

## Configuration

`config.json` is generated on first launch under the data dir above.

```json
{
  "bindHost": "0.0.0.0",
  "port": 4789,
  "tools": {
    "claude":           { "command": "claude",     "defaultArgs": "--remote-control" },
    "claude-chat":      { "command": "claude",     "defaultArgs": "", "engine": "agent" },
    "codex":            { "command": "codex",      "defaultArgs": "" },
    "reasonix":         { "command": "reasonix",   "defaultArgs": "" },
    "opencode":         { "command": "opencode",   "defaultArgs": "" },
    "aider":            { "command": "aider",      "defaultArgs": "" },
    "gemini":           { "command": "gemini",     "defaultArgs": "" },
    "qwen":             { "command": "qwen-code",  "defaultArgs": "" },
    "cursor-agent":     { "command": "cursor-agent", "defaultArgs": "" },
    "copilot":          { "command": "copilot",    "defaultArgs": "" },
    "powershell":       { "command": "powershell", "defaultArgs": "-NoLogo" },
    "bash":             { "command": "bash",       "defaultArgs": "" }
  },
  "sessions": []
}
```

Environment variables:

| Var | Purpose |
|---|---|
| `WEBPTY_DATA_DIR` | Override data/config directory. |
| `WEBPTY_PROJECTS_ROOT` | Folder whose immediate subfolders show up in the **Add Session** dropdown. Defaults to the parent of the install dir. |
| `WEBPTY_PORT` | Override the listen port (takes precedence over `config.port`). |

Static assets (xterm.js etc.) are served with long-lived immutable cache
headers, and the initial page load fetches config / projects / sessions
in parallel, so the app paints fast even on first visit.

The previous `PTYHUB_*` and `CSMWEB_*` env vars are still honoured as
fallbacks, and legacy `%APPDATA%\ptyhub\` / `%APPDATA%\CSMWeb\` data
directories are auto-migrated on first launch.

### Adding more tools

Add an entry to `config.tools`. The string in `defaultArgs` is shell-split
and passed as `argv`.

### PowerShell as Admin

The `powershell-admin` default uses [gsudo](https://github.com/gerardog/gsudo)
to elevate inside the PTY. Install it once:

```sh
winget install gerardog.gsudo
```

Without `gsudo`, the admin shortcut fails with *"File not found: gsudo"*
in the session's terminal. You can either install gsudo, replace the
command in `config.json`, or run `webpty` itself elevated.

## Security

webpty ships **no auth by default** — it binds `0.0.0.0` so anyone who
can reach the port can spawn shells / drive your agent sessions / read
output. Two gates are available (localhost connections always bypass
both):

### 1. Token gate (recommended, no Tailscale needed)

Set `authToken` in `config.json`:

```json
{ "authToken": "pick-a-long-random-string" }
```

On next launch the server prints `token gate ON`. Every non-localhost
request must then present the token via `Authorization: Bearer …`
header, `?token=…` query param, or the `webpty_token` cookie. The web UI
shows a one-time unlock screen and remembers the token in `localStorage`.

### 2. Tailscale identity gate

`allowedLogins` whitelists tailnet login emails; `tailscale whois` maps
each peer IP back to its login. With both `authToken` unset and
`allowedLogins` empty the gate is fully disabled (legacy behavior).

```json
{ "allowedLogins": ["you@example.com"] }
```

### Network-layer options

- **[Tailscale](https://tailscale.com/)** — exposes the port only to your
  tailnet (this is the original author's setup).
- **WireGuard / VPN** to a private subnet.
- **SSH local-forward** to a remote host.
- `bindHost: "127.0.0.1"` if you only want loopback (then reverse-proxy
  in front of it).

## Architecture

```
browser                              webpty server                          child
─────────                            ──────────────                          ─────
xterm.js  ───── WebSocket (binary) ──→  node-pty (ConPTY/forkpty)   ──→  claude / codex / reasonix / pwsh
   ▲                                          │
   └─── /api/{config,projects,sessions} ──────┘
```

- `src/server.js` — Express + `ws`; serves the SPA, REST for session
  lifecycle (including `POST /api/projects/create`), WebSocket per
  session id, token-gate middleware.
- `src/session-manager.js` — owns the `node-pty` children; handles
  spawn / resize / write / kill, plus claude auto-resume.
- `src/config.js` — JSON config persistence (user roots are preserved on
  reload), legacy-dir migration on first run.
- `src/auth.js` — token gate + Tailscale `whois` identity gate.
- `public/` — single-page UI: per-session full-screen xterm pages in a
  swipe carousel; bottom dot indicator; kebab menu; create-project and
  token-unlock flows.

## Differences from other web terminals

| | ttyd | gotty | wetty | **webpty** |
|---|---|---|---|---|
| Windows ConPTY | partial | no | via SSH | yes (node-pty) |
| Multiple persistent sessions | no | no | no | **yes** |
| Auto-resume agent conversations | n/a | n/a | n/a | **yes (claude `-c`)** |
| Mobile-first swipe UX | no | no | no | **yes** |
| Built-in auth | yes (token) | basic-auth | login | **token gate + Tailscale gate** |

If you only need *one* generic shell in a browser, use `ttyd`. webpty's
niche is *many long-running LLM CLI sessions, one per project, reached
from any device*.

## License

[MIT](./LICENSE)
