#!/usr/bin/bash
# Enable wyoming-tts on one Hermes voice body (runs ON CT116).
#   rollout.sh <profile> <unit>              e.g. rollout.sh wheatley-voice hermes-wheatley-voice
#   rollout.sh <profile> <unit> --rollback   restore the pre-rollout config and restart
# Expects the plugin folder at /tmp/hwt/hermes-wyoming-tts/wyoming-tts (copied up by the caller).
# The config edit and the restart run back to back: the gateway re-reads tts config every turn,
# so a turn between them would find `wyoming` unregistered and use whole-file TTS (slower, not silent).
set -euo pipefail
PROFILE=${1:?profile}; UNIT=${2:?unit}; MODE=${3:-}
HOME_DIR=/root/.hermes/profiles/$PROFILE
V=/usr/local/lib/hermes-agent/venv/bin/python3
BACKUP=$HOME_DIR/config.yaml.bak-pre-wyoming-tts
WYOMING_HOST=192.168.1.14

if [[ $MODE == --rollback ]]; then
  [[ -f $BACKUP ]] || { echo "no backup at $BACKUP"; exit 1; }
  cp -a "$BACKUP" "$HOME_DIR/config.yaml"
  systemctl restart "$UNIT"
  echo "rolled back $PROFILE (plugin files left in place, now disabled)"
  exit 0
fi

mkdir -p "$HOME_DIR/plugins"
rm -rf "$HOME_DIR/plugins/wyoming-tts"
cp -r /tmp/hwt/hermes-wyoming-tts/wyoming-tts "$HOME_DIR/plugins/"
find "$HOME_DIR/plugins/wyoming-tts" -name __pycache__ -prune -exec rm -rf {} +
[[ -f $BACKUP ]] || cp -a "$HOME_DIR/config.yaml" "$BACKUP"

$V - "$HOME_DIR/config.yaml" "$WYOMING_HOST" <<'P'
import sys, yaml
path, host = sys.argv[1], sys.argv[2]
c = yaml.safe_load(open(path))
enabled = c.setdefault("plugins", {}).setdefault("enabled", [])
if "wyoming-tts" not in enabled:
    enabled.append("wyoming-tts")
tts = c.setdefault("tts", {})
tts.setdefault("streaming", {})["provider"] = "wyoming"
tts.setdefault("wyoming", {})["host"] = host
yaml.safe_dump(c, open(path, "w"), sort_keys=False, allow_unicode=True)
print("config updated:", path)
P
systemctl restart "$UNIT"
sleep 8
journalctl -u "$UNIT" --since -30s --no-pager | grep -E "wyoming-tts|Traceback|ERROR" || echo "WARN: no wyoming-tts line yet; check: journalctl -u $UNIT | grep wyoming-tts"
