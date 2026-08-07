#!/usr/bin/env bash
#
# webpty — one-command deploy
#
#   install.sh [--update] [--port N] [--host HOST] [--projects-root DIR]
#               [--bind 0.0.0.0] [--uninstall]
#
# What it does (idempotent):
#   1. locate the webpty source dir (default: this script's dir; or clone from
#      GitHub when --source https://github.com/kellyson520/webpty.git is given)
#   2. install production deps with npm ci (or npm install if no lockfile)
#   3. write /etc/systemd/system/webpty.service
#   4. systemctl daemon-reload + enable --now (starts or restarts the service)
#
# All options can also be given via env (WEBPTY_PORT, WEBPTY_HOST,
# WEBPTY_PROJECTS_ROOT, WEBPTY_BIND_HOST).  Service runs as root by default —
# set WEBPTY_USER to a dedicated account for tighter privilege separation.
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

usage() {
  sed -n '2,14p' "$0"
  exit "${1:-0}"
}

for arg in "$@"; do
  case "$arg" in
    --uninstall) DO_UNINSTALL=1 ;;
    -h|--help)   usage ;;
    --port=*)    PORT="${arg#--port=}" ;;
    --host=*)    BIND_HOST="${arg#--host=}" ;;
    --bind=*)    BIND_HOST="${arg#--bind=}" ;;
    --projects-root=*) PROJECTS_ROOT="${arg#--projects-root=}" ;;
    --source=*)  GIT_REPO="${arg#--source=}" ;;
    --git-repo=*) GIT_REPO="${arg#--git-repo=}" ;;
    --user=*)    RUN_USER="${arg#--user=}" ;;
    --update)    : ;; # idempotent anyway
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
  TMP_CLONE="$(mktemp -d)"
  echo ">> cloning $GIT_REPO"
  need git
  git clone --depth 1 "$GIT_REPO" "$TMP_CLONE/webpty"
  SRC_DIR="$TMP_CLONE/webpty"
elif [ ! -f "$SRC_DIR/package.json" ]; then
  echo "ERROR: no package.json in $SRC_DIR — pass --source/--git-repo or run from a webpty checkout" >&2
  exit 1
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
  echo "  (gate: set authToken in \$(data dir)/config.json to require a token — see README)"
else
  echo "✘ webpty failed to start — check: journalctl -u webpty.service -n 50" >&2
  exit 1
fi
