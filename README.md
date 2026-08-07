# webpty

**v0.0.1** · [GitHub kellyson520/webpty](https://github.com/kellyson520/webpty)

> **English · 中文** — this README is bilingual. 本 README 为双语。

A multi-session web terminal that supervises long-running CLI agents
(`claude`, `codex`, `reasonix`, `opencode`, `aider`, `pwsh`, …) per project
folder. Open any browser on your trusted network — desktop or phone — and
switch between live sessions with a swipe.

一个多会话网页终端：按项目文件夹监督常驻运行的 CLI 智能体（`claude`、
`codex`、`reasonix`、`opencode`、`aider`、`pwsh` 等）。在可信网络内用任意
浏览器（桌面或手机）打开，滑动即可切换实时会话。

> Built for the workflow: *"My PC has an AI agent CLI running in each of my
> project folders. I want to reach whichever one I need from my phone."*

---

## What it gives you / 功能

- **Per-project supervisors** — one PTY per registered folder, kept alive
  across browser disconnects.
  **按项目监督** — 每个注册文件夹一个 PTY，浏览器断开仍保持运行。
- **Broad agent-tool support** — preconfigured profiles for `reasonix`,
  `codex`, `opencode`, `aider`, `gemini`, `qwen-code`, `cursor-agent`,
  `copilot`, `claude` and more. Opening a folder with no session
  auto-selects `reasonix`.
  **主流 agent 工具** — 预置 `reasonix`、`codex`、`opencode`、`aider`、
  `gemini`、`qwen-code`、`cursor-agent`、`copilot`、`claude` 等配置；
  打开无会话的项目默认选择 `reasonix`。
- **Create projects from the UI** — the drawer's *新建* field makes a folder
  under the projects root (optionally `git init`ed).
  **界面创建项目** — 抽屉的“新建”输入框在项目根下创建文件夹（可选
  `git init`）。
- **Optional access gate** — set `authToken` in config and every
  non-localhost request must present it; the Tailscale identity gate is
  still supported. See [Security](#security).
  **可选访问门禁** — 在配置中设置 `authToken` 后，所有非 localhost 请求
  必须携带令牌；仍支持 Tailscale 身份门禁。见[安全](#security)。
- **Mobile-first UI** — full-screen-per-session, horizontal swipe to switch,
  kebab menu (`退出` / `清屏` / `压缩上下文`).
  **移动优先 UI** — 每会话全屏、左右滑动切换、菜单（退出/清屏/压缩上下文）。
- **Pure Python, zero runtime deps** — stdlib-only (asyncio + pty); no npm,
  no node_modules.
  **纯 Python、零运行时依赖** — 仅用标准库（asyncio + pty）；无 npm、
  无 node_modules。

---

## Install & deploy / 安装与部署

webpty is deployed from the git repo — the script always syncs to the latest
code, so the running version is never frozen.

webpty 从 git 仓库部署——脚本始终同步最新代码，运行版本不会锁死。

### One-command deploy / 一条命令部署

```sh
git clone https://github.com/kellyson520/webpty.git
cd webpty
./install.sh            # creates venv → writes a systemd unit → starts the service
```

Tuning knobs (flags or env vars) / 调优参数（参数或环境变量）：

```sh
./install.sh \
  --port=8080 \                    # or WEBPTY_PORT=8080
  --bind=0.0.0.0 \                 # or WEBPTY_BIND_HOST=0.0.0.0
  --projects-root=/srv/projects \  # or WEBPTY_PROJECTS_ROOT=/srv/projects
  --user=webpty                    # or WEBPTY_USER=webpty
```

Remote one-shot / 远程一键：

```sh
curl -sL https://github.com/kellyson520/webpty/archive/refs/heads/main.tar.gz \
  | tar xz && cd webpty-main && ./install.sh
```

### Keeping everything in sync / 保持同步

```sh
cd webpty
./install.sh                # pull latest webpty code + restart
./install.sh --update-cli   # update installed agent CLIs via npm -g
```

`--update-cli` only touches CLIs already installed globally — it never
auto-installs new ones. Remove everything with `./install.sh --uninstall`.

`--update-cli` 只更新已全局安装的 CLI——绝不自动安装新的。卸载用
`./install.sh --uninstall`。

Service management / 服务管理：`journalctl -u webpty.service -f`、
`systemctl restart webpty`.

### Requirements / 环境要求

- **Python ≥ 3.10** (stdlib only — no pip packages required)
- **Linux / macOS** (PTY via stdlib `pty`; Windows not supported by the
  stdlib PTY host)

---

## Run / 运行

```sh
./install.sh          # via systemd (recommended)
# or directly:
python3 src/server.py
# → [webpty] listening on http://0.0.0.0:4789
# → [webpty] config:    ~/.config/webpty/config.json
```

Open `http://<host>:4789/` from a browser on the same trusted network.
Port can be overridden at boot: `WEBPTY_PORT=8080 python3 src/server.py`
(or `webpty --port 8080`).

---

## Configuration / 配置

`config.json` is generated on first launch under the data dir
(`~/.config/webpty/` on POSIX). Key fields / 关键字段:

```json
{
  "bindHost": "0.0.0.0",
  "port": 4789,
  "roots": ["/path/to/projects"],
  "authToken": "",
  "tools": {
    "codex":    { "command": "codex",    "defaultArgs": "" },
    "reasonix": { "command": "reasonix", "defaultArgs": "" },
    "claude":   { "command": "claude",   "defaultArgs": "--remote-control", "nameFlag": "-n" }
  }
}
```

Environment variables / 环境变量:

| Var | Purpose / 用途 |
|---|---|
| `WEBPTY_DATA_DIR` | Override data/config directory / 覆盖数据与配置目录 |
| `WEBPTY_PROJECTS_ROOT` | Folder whose subfolders appear in the drawer / 抽屉中显示的子文件夹根目录 |
| `WEBPTY_PORT` | Override the listen port / 覆盖监听端口 |
| `WEBPTY_BIND_HOST` | Override the bind host / 覆盖绑定地址 |

### Tools are fully yours to configure / 工具配置完全由你掌控

The `tools` map is **not locked** — webpty merges your `config.json` over the
built-in defaults on every boot, and your edits always win:

`tools` 映射**不会被锁死**——每次启动 webpty 都会把你的 `config.json` 合并到
内置默认值之上，你的修改永远生效：

```json
// 1. Tune a built-in tool / 调整内置工具
{ "tools": { "codex": { "defaultArgs": "--full-auto" } } }

// 2. Add a custom tool / 新增自定义工具
{ "tools": { "my-agent": { "command": "myagent", "defaultArgs": "--watch" } } }

// 3. Disable a built-in tool / 禁用内置工具
{ "tools": { "gemini": null } }
```

webpty spawns `command` from your `PATH` — it never pins a CLI version — so
updating a tool (e.g. `./install.sh --update-cli`) automatically applies to
new sessions.

webpty 从你的 `PATH` 启动 `command`——从不固定 CLI 版本——因此更新工具
（如 `./install.sh --update-cli`）后新会话自动生效。

---

## Security / 安全

webpty ships **no auth by default** — it binds `0.0.0.0` so anyone who can
reach the port can spawn shells. Localhost always bypasses the gate.

webpty 默认**无认证**——绑定 `0.0.0.0`，能访问该端口的人即可启动 shell。
localhost 始终绕过门禁。

### 1. Token gate / 令牌门禁 (recommended / 推荐)

```json
{ "authToken": "pick-a-long-random-string" }
```

Every non-localhost request must then present the token via
`Authorization: Bearer …` header, `?token=…` query param, or the
`webpty_token` cookie. The UI shows a one-time unlock screen.

此后每个非 localhost 请求必须通过 `Authorization: Bearer …` 请求头、
`?token=…` 查询参数或 `webpty_token` cookie 携带令牌。UI 显示一次性解锁界面。

### 2. Tailscale identity gate / 身份门禁

```json
{ "allowedLogins": ["you@example.com"] }
```

`tailscale whois` maps each peer IP back to its tailnet login; only whitelisted
logins are allowed. With both `authToken` unset and `allowedLogins` empty the
gate is fully disabled (legacy behavior).

`tailscale whois` 把每个对端 IP 映射回其 tailnet 登录名，只允许白名单登录。
`authToken` 未设置且 `allowedLogins` 为空时门禁完全禁用（旧行为）。

### Network-layer options / 网络层选项

- **[Tailscale](https://tailscale.com/)** — expose the port only to your tailnet
- **WireGuard / VPN** — private subnet
- **SSH local-forward** — remote host
- `bindHost: "127.0.0.1"` — loopback only

---

## Architecture / 架构

```
browser                              webpty server                          child
─────────                            ──────────────                          ─────
xterm.js  ───── WebSocket (binary) ──→  pty-host daemon (stdlib pty)  ──→  claude / codex / reasonix / pwsh
   ▲                                          │
   └─── /api/{config,projects,sessions} ──────┘
```

- `src/server.py` — asyncio HTTP + WebSocket server (stdlib only), REST API,
  token-gate middleware, static SPA.
- `src/ws.py` — minimal RFC 6455 WebSocket implementation.
- `src/session_manager.py` — session lifecycle; agent engine (stream-json)
  and PTY delegation.
- `src/pty_host.py` — detached PTY daemon (`pty.fork` + selectors) so PTYs
  survive webpty restarts.
- `src/config.py` — JSON config persistence (user tools/roots preserved).
- `src/auth.py` — token gate + Tailscale `whois` identity gate.
- `public/` — single-page UI: per-session full-screen xterm pages in a swipe
  carousel; create-project and token-unlock flows.

---

## Testing / 测试

```sh
python3 -m unittest discover -s test
```

108 tests cover paths, args parsing, ring buffer, auth, config merging,
session manager, and end-to-end HTTP/WebSocket behavior.

108 个测试覆盖路径、参数解析、环形缓冲、认证、配置合并、会话管理以及
端到端 HTTP/WebSocket 行为。

---

## License / 许可证

[MIT](./LICENSE)
