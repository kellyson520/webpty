#!/usr/bin/env bash
#
# webpty — script deploy (no npm publish needed)
#
#   install.sh                 deploy/update webpty from this checkout
#   install.sh --update        pull latest code from git, reinstall deps, restart
#   install.sh --update-cli    update installed agent CLI tools (reasonix,
#                              codex, claude-code, opencode, …) to latest
#   install.sh --uninstall     stop and remove the webpty service
#
# Tuning (flags or env):
#   --port=N  / WEBPTY_PORT              listen port (default 4789)
#   --bind=H  / WEBPTY_BIND_HOST         bind host (default 0.0.0.0)
#   --projects-root=DIR / WEBPTY_PROJECTS_ROOT
#   --user=U  / WEBPTY_USER              service user (default root)
#   --source=GIT_URL / WEBPTY_GIT_REPO   clone from git instead of local dir
#
# Idempotent: re-running updates code + deps + restarts the service, so
# webpty always tracks the repo (never a frozen npm release).
#
set -euo pipefail

# ---------- defaults -------------------------------------------------------
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="${WEBPTY_SRC_DIR:-$SELF_DIR}"
GIT_REPO="${WEBPTY_GIT_REPO:-}"
BIND_HOST="${WEBPTY_BIND_HOST:-0.0.0.0}"
PORT="${WEBPTY_PORT:-4789}"
PROJECTS_ROOT="${WEBPTY_PROJECTS_ROOT:-}"
RUN_USER="${WEBPTY_USER:-root}"
NODE_BIN="$(command -v node || true)"
NPM_BIN="$(command -v npm || true)"
DO_UNINSTALL=0
DO_UPDATE_CLI=0
# npm package names for the agent CLIs webpty supervises; only packages that
# are already installed globally get updated (never auto-installs new ones).
AGENT_CLI_PACKAGES=(
  reasonix
  "@openai/codex"
  "@anthropic-ai/claude-code"
  "opencode-ai"
  "aider-chat"
  "@google/gemini-cli"
)

usage() {
  sed -n '2,11p' "$0"
  echo
  echo "Options:"
  echo "  --update            git pull + npm ci + restart webpty"
  echo "  --update-cli        npm update -g installed agent CLIs (reasonix, codex, claude-code, …)"
  echo "  --uninstall         stop & remove webpty.service"
  echo "  --port=N --bind=H --projects-root=DIR --user=U --source=GIT_URL"
  exit "${1:-0}"
}

for arg in "$@"; do
  case "$arg" in
    --uninstall)  DO_UNINSTALL=1 ;;
    --update)     : ;; # default behaviour; kept for clarity
    --update-cli) DO_UPDATE_CLI=1 ;;
    -h|--help)    usage ;;
    --port=*)     PORT="${arg#--port=}" ;;
    --host=*)     BIND_HOST="${arg#--host=}" ;;
    --bind=*)     BIND_HOST="${arg#--bind=}" ;;
    --projects-root=*) PROJECTS_ROOT="${arg#--projects-root=}" ;;
    --source=*)   GIT_REPO="${arg#--source=}" ;;
    --git-repo=*) GIT_REPO="${arg#--git-repo=}" ;;
    --user=*)     RUN_USER="${arg#--user=}" ;;
    *)
      echo "Unknown option: $arg (see --help)" >&2
      exit 2
      ;;
  esac
done

need() { command -v "$1" >/dev/null 2>&1 || { echo "ERROR: $1 not found (required)" >&2; exit 1; }; }

# ---------- uninstall ------------------------------------------------------
if [ "$DO_UNINSTALL" = 1 ]; then
  if systemctl list-unit-files | grep -q '^webpty.service'; then
    systemctl disable --now webpty.service
    rm -f /etc/systemd/system/webpty.service
    systemctl daemon-reload
    echo "webpty.service removed."
  else
    echo "webpty.service not installed — nothing to do."
  fi
  exit 0
fi

# ---------- update agent CLIs ----------------------------------------------
if [ "$DO_UPDATE_CLI" = 1 ]; then
  need "$NPM_BIN"
  echo ">> updating installed agent CLIs (keeps your tools in sync, not frozen)"
  for pkg in "${AGENT_CLI_PACKAGES[@]}"; do
    if npm ls -g --depth=0 "$pkg" >/dev/null 2>&1; then
      before="$(npm ls -g --depth=0 "$pkg" 2>/dev/null | sed -n '2p')"
      echo ">> updating $pkg ($before)"
      npm update -g "$pkg" --no-audit --no-fund 2>&1 | tail -1 || echo "   (update failed for $pkg, continuing)"
    else
      echo "   $pkg not installed globally — skipping"
    fi
  done
  echo "✔ CLI tools updated. Restart webpty to pick up new versions: ./install.sh"
  exit 0
fi

# ---------- prerequisites --------------------------------------------------
need systemctl
need "$NODE_BIN" 2>/dev/null || need node
need "$NPM_BIN" 2>/dev/null || need npm

NODE_MAJOR="$("$NODE_BIN" -p 'process.versions.node.split(".")[0]')"
if [ "$NODE_MAJOR" -lt 20 ]; then
  echo "ERROR: webpty needs Node >= 20 (found $("$NODE_BIN" -v))" >&2
  exit 1
fi

# ---------- source dir -----------------------------------------------------
if [ -n "$GIT_REPO" ]; then
  need git
  TMP_CLONE="$(mktemp -d)"
  echo ">> cloning $GIT_REPO"
  git clone --depth 1 "$GIT_REPO" "$TMP_CLONE/webpty"
  SRC_DIR="$TMP_CLONE/webpty"
elif [ ! -f "$SRC_DIR/package.json" ]; then
  echo "ERROR: no package.json in $SRC_DIR — run from a webpty checkout or pass --source=GIT_URL" >&2
  exit 1
fi

# Sync to the latest code when this is a git checkout (the whole point:
# webpty tracks the repo instead of a frozen npm release).
if [ -d "$SRC_DIR/.git" ] && git -C "$SRC_DIR" remote >/dev/null 2>&1; then
  echo ">> pulling latest code"
  git -C "$SRC_DIR" pull --ff-only --quiet || {
    echo "!! git pull failed — deploying with the code already on disk" >&2
  }
fi

# ---------- install deps ---------------------------------------------------
echo ">> installing dependencies in $SRC_DIR"
cd "$SRC_DIR"
if [ -f package-lock.json ]; then
  "$NPM_BIN" ci --omit=dev --no-audit --no-fund
else
  "$NPM_BIN" install --omit=dev --no-audit --no-fund
fi

# ---------- systemd unit ---------------------------------------------------
mkdir -p /etc/systemd/system
UNIT=/etc/systemd/system/webpty.service

if [ "$RUN_USER" != root ]; then
  id "$RUN_USER" >/dev/null 2>&1 || { echo "ERROR: user $RUN_USER does not exist" >&2; exit 1; }
  chown -R "$RUN_USER":"$RUN_USER" "$SRC_DIR" 2>/dev/null || true
fi

ENV_LINES="Environment=WEBPTY_BIND_HOST=$BIND_HOST"
ENV_LINES+=$'\n'"Environment=WEBPTY_PORT=$PORT"
[ -n "$PROJECTS_ROOT" ] && ENV_LINES+=$'\n'"Environment=WEBPTY_PROJECTS_ROOT=$PROJECTS_ROOT"

cat > "$UNIT" <<EOF
[Unit]
Description=webpty — multi-session web terminal (AI agent supervisor)
After=network.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$SRC_DIR
$ENV_LINES
ExecStart=$NODE_BIN $SRC_DIR/src/server.js
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable webpty.service >/dev/null 2>&1 || true
systemctl restart webpty.service

sleep 1
if systemctl is-active --quiet webpty.service; then
  echo
  echo "✔ webpty is running:  http://${BIND_HOST}:${PORT}/"
  echo "  (log: journalctl -u webpty.service -f)"
  echo "  (keep tools in sync: ./install.sh --update-cli)"
  echo "  (gate: set authToken in \$(data dir)/config.json to require a token — see README)"
else
  echo "✘ webpty failed to start — check: journalctl -u webpty.service -n 50" >&2
  exit 1
fi
