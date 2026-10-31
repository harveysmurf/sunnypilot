#!/bin/bash
# Install the power/shutdown evidence logger on the comma device.
# Must run as root (or via sudo) on the device, repo at /data/openpilot.
#
#   sudo bash /data/openpilot/tools/power-debug/install.sh
#
# The AGNOS rootfs is read-only, so the unit is installed by remounting / rw
# briefly. It is removed again by an AGNOS update; re-run this script after one.
set -euo pipefail

REPO="${REPO:-/data/openpilot}"
UNIT_NAME=power-debug.service
DEST=/etc/systemd/system/$UNIT_NAME
SRC="$REPO/tools/power-debug/power-debug.service"

[ -f "$SRC" ] || { echo "missing $SRC" >&2; exit 1; }

mkdir -p /data/power-debug/sessions
chown -R comma:comma /data/power-debug

echo "[1/4] remount / rw"
mount | grep -q " / .*\(ro,\)" && mount -o remount,rw /

echo "[2/4] install $DEST"
install -m 0644 "$SRC" "$DEST"
install -m 0644 "$REPO/tools/power-debug/power-debug-shutdown.service" /etc/systemd/system/power-debug-shutdown.service
ln -sf "$REPO/tools/power-debug/power_logger.py" /usr/local/bin/power-logger 2>/dev/null || true

echo "[3/4] enable + restart"
systemctl daemon-reload
systemctl enable "$UNIT_NAME" power-debug-shutdown.service
systemctl restart "$UNIT_NAME"
sleep 3
systemctl is-active "$UNIT_NAME"

echo "[4/4] remount / ro"
mount | grep -q " / .*\(rw,\)" && mount -o remount,ro /
sync

systemctl status "$UNIT_NAME" --no-pager -n 5 || true
echo "logs: /data/power-debug/sessions/ and /data/power-debug/events.log"
