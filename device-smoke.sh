#!/bin/bash
# Wait (bounded) for an adb-visible phone, then run a real Airtest smoke test.
# Usage: device-smoke.sh [max_wait_seconds]
set -u
export ANDROID_HOME=/usr/lib/android-sdk
export PATH="$ANDROID_HOME/platform-tools:$PATH"
cd "$(dirname "$0")" || exit 1
LOG=logs/device-smoke.log
mkdir -p logs

MAXWAIT=${1:-900}
DEADLINE=$(( $(date +%s) + MAXWAIT ))
echo "[$(date -u +%FT%TZ)] waiting up to ${MAXWAIT}s for an adb device..." >> "$LOG"

device_serial() {
    adb devices 2>/dev/null | awk 'NR>1 && $2=="device"{print $1; exit}'
}

while [ "$(date +%s)" -lt "$DEADLINE" ]; do
    SERIAL=$(device_serial)
    if [ -n "${SERIAL:-}" ]; then
        echo "[$(date -u +%FT%TZ)] device appeared: $SERIAL" >> "$LOG"
        .venv/bin/python - "$SERIAL" >> "$LOG" 2>&1 <<'PY'
import sys, time
from airtest.core.api import connect_device, snapshot, shell, touch, swipe
serial = sys.argv[1]
dev = connect_device("Android:///%s" % serial)
print("CONNECTED", serial)
print("MODEL", shell("getprop ro.product.model").strip())
print("RES", dev.get_current_resolution())
snap = snapshot(filename="logs/device-shot.png")
print("SNAPSHOT", snap)
w, h = dev.get_current_resolution()
swipe((int(w*0.5), int(h*0.75)), (int(w*0.5), int(h*0.45)), duration=0.4)
time.sleep(1)
print("SWIPE_OK")
print("SMOKE_DONE")
PY
        echo "[$(date -u +%FT%TZ)] smoke finished rc=$?" >> "$LOG"
        exit 0
    fi
    sleep 5
done
echo "[$(date -u +%FT%TZ)] timeout: no adb device in ${MAXWAIT}s" >> "$LOG"
exit 1
