#!/usr/bin/env bash
# Install the daily / monthly schedule as systemd *user* units.
#
#   daily     03:00  --mode auto        (cheap: only models/tasks not yet done)
#   monthly   1st 04:00 --mode full --force   (everything, cache ignored)
#   webui     long-running control panel on 127.0.0.1:8765
#
# Idempotent: re-run to update the units in place.  Use --uninstall to remove.
set -euo pipefail

MODE="install"
case "${1:-}" in
  --uninstall) MODE="uninstall" ;;
  "") ;;
  -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
  *) echo "unknown option: $1" >&2; exit 2 ;;
esac

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
RUN="$ROOT/scripts/run-eval.sh"
WEBUI="$ROOT/src/webui.py"

if ! command -v systemctl >/dev/null 2>&1; then
  echo "systemctl not found -- this host has no systemd; use cron or run" >&2
  echo "  $RUN --mode auto   from your own scheduler." >&2
  exit 1
fi

if [ "$MODE" = "uninstall" ]; then
  for u in model-eval-daily model-eval-monthly model-eval-webui; do
    systemctl --user disable --now "$u.timer" 2>/dev/null || true
    systemctl --user disable --now "$u.service" 2>/dev/null || true
  done
  rm -f "$UNIT_DIR"/model-eval-*.service "$UNIT_DIR"/model-eval-*.timer
  systemctl --user daemon-reload 2>/dev/null || true
  echo "removed model-eval units from $UNIT_DIR"
  exit 0
fi

# systemd user units do not inherit your shell profile, so PATH has to be
# spelled out -- otherwise opencode/agy are not found at 03:00.
EXTRA_PATH="$(dirname "$(command -v opencode 2>/dev/null || echo /usr/bin/false)")"
EXTRA_PATH="$EXTRA_PATH:$(dirname "$(command -v agy 2>/dev/null || echo /usr/bin/false)")"
EXTRA_PATH="$EXTRA_PATH:$HOME/.local/bin:$HOME/.cargo/bin:$HOME/.npm-global/bin"

mkdir -p "$UNIT_DIR"

cat > "$UNIT_DIR/model-eval-daily.service" <<EOF
[Unit]
Description=model_eval -- daily evaluation (minimal profile, cache-friendly)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$ROOT
Environment=PATH=$EXTRA_PATH
Environment=HOME=$HOME
ExecStart=$RUN --mode auto
# A run holds a flock; a 12h ceiling means a hung model cannot pin the unit.
TimeoutStartSec=12h
EOF

cat > "$UNIT_DIR/model-eval-daily.timer" <<'EOF'
[Unit]
Description=model_eval -- daily evaluation at 03:00

[Timer]
OnCalendar=*-*-* 03:00:00
# Catch up if the machine was asleep at 03:00.
Persistent=true
RandomizedDelaySec=5min

[Install]
WantedBy=timers.target
EOF

cat > "$UNIT_DIR/model-eval-monthly.service" <<EOF
[Unit]
Description=model_eval -- monthly full re-evaluation (force, everything)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$ROOT
Environment=PATH=$EXTRA_PATH
Environment=HOME=$HOME
ExecStart=$RUN --mode full --force
TimeoutStartSec=48h
EOF

cat > "$UNIT_DIR/model-eval-monthly.timer" <<'EOF'
[Unit]
Description=model_eval -- monthly full re-evaluation on the 1st at 04:00

[Timer]
# "*-*-01" is the 1st of every month; "*-1-01" would be January 1st only.
OnCalendar=*-*-01 04:00:00
Persistent=true
RandomizedDelaySec=15min

[Install]
WantedBy=timers.target
EOF

cat > "$UNIT_DIR/model-eval-webui.service" <<EOF
[Unit]
Description=model_eval -- local control panel (127.0.0.1:8765)
After=network.target

[Service]
Type=simple
WorkingDirectory=$ROOT
Environment=PATH=$EXTRA_PATH
Environment=HOME=$HOME
ExecStart=/usr/bin/env python3 $WEBUI --host 127.0.0.1 --port 8765
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now model-eval-daily.timer
systemctl --user enable --now model-eval-monthly.timer
systemctl --user enable --now model-eval-webui.service

echo
echo "installed into $UNIT_DIR"
echo
systemctl --user list-timers 'model-eval-*' --all --no-pager || true
echo
echo "control panel: http://127.0.0.1:8765"
echo
echo "notes:"
echo "  * user timers only fire while your session is alive.  To let them run"
echo "    after logout:   loginctl enable-linger $USER"
echo "  * status:         systemctl --user status model-eval-daily.timer"
echo "  * run by hand:    systemctl --user start model-eval-daily.service"
echo "  * follow a run:   tail -f $ROOT/state/live.log"
echo "  * uninstall:      $0 --uninstall"
