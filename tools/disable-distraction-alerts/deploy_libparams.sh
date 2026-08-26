#!/bin/bash
# Rebuild libparams_c.so with the DisableDriverDistraction key on-device.
#
# WHY THIS EXISTS:
#   openpilot/common/params_keys.h is the source of truth for the key, but param
#   validation at runtime comes from the compiled libparams_c.so. The committed
#   .so (md5 e138ba3e) is STOCK and does NOT contain DisableDriverDistraction,
#   so a fresh checkout of a branch that reads that key (dmonitoringd.py:19,
#   selfdrived.py:122, controlsd.py:229) raises UnknownKeyName and boot-loops
#   all three daemons. Rebuilding the .so is MANDATORY on every fresh deploy,
#   not just recovery from a wiped /data.
#
#   The original custom .so (md5 f16509d2) was built with throwaway shims under
#   /tmp that died on reboot. Those shims are now committed in this repo under
#   tools/disable-distraction-alerts/shim/ so the build is reproducible.
#
# USAGE (on the comma device, user 'comma', cwd any):
#   bash /data/openpilot/tools/disable-distraction-alerts/deploy_libparams.sh
#
# It must run from the repo checkout because it compiles openpilot/common sources.

set -euo pipefail

REPO="${REPO:-/data/openpilot}"
TOOL_DIR="$REPO/tools/disable-distraction-alerts"
OUT="$(mktemp -d)"
trap 'rm -rf "$OUT"' EXIT

if [ ! -d "$REPO/.git" ]; then
  echo "ERROR: $REPO is not a git checkout (set REPO if the device path differs)" >&2
  exit 1
fi
if [ ! -f "$TOOL_DIR/shim/openpilot/cereal/gen/cpp/log.capnp.h" ] || [ ! -f "$TOOL_DIR/shim/swaglog_stub.cc" ]; then
  echo "ERROR: shim files missing under $TOOL_DIR/shim/ — is the repo up to date?" >&2
  exit 1
fi

echo "[1/6] compiling custom libparams_c.so (with DisableDriverDistraction)"
g++ -O2 -shared -fPIC -std=c++17 -D__TICI__ \
  -I"$TOOL_DIR/shim" -I"$REPO" -I"$REPO/openpilot" -I/usr/include \
  "$REPO/openpilot/common/params_c.cc" \
  "$REPO/openpilot/common/params.cc" \
  "$REPO/openpilot/common/util.cc" \
  "$TOOL_DIR/shim/swaglog_stub.cc" \
  -lzmq -lpthread -o "$OUT/libparams_c.so.new"

echo "[2/6] correctness gate 1: key present in .so (must pass)"
if ! strings "$OUT/libparams_c.so.new" | grep -q DisableDriverDistraction; then
  echo "ERROR: DisableDriverDistraction key missing from rebuilt .so" >&2
  exit 1
fi

echo "[3/6] byte-identity check vs known-good custom build (soft gate)"
NEW_MD5="$(md5sum "$OUT/libparams_c.so.new" | awk '{print $1}')"
if [ "$NEW_MD5" = "f16509d2" ]; then
  echo "OK: byte-identical to the known-good custom .so (f16509d2)"
else
  echo "NOTE: md5=$NEW_MD5 differs from f16509d2."
  echo "  Expected: the ARM64 toolchain/flags differ from the original build."
  echo "  NOT aborting — correctness is gated by the key-presence check + Params round-trip below."
fi

echo "[4/6] installing .so (backing up current)"
cp "$REPO/openpilot/common/libparams_c.so" "$REPO/openpilot/common/libparams_c.so.orig" 2>/dev/null || true
cp "$OUT/libparams_c.so.new" "$REPO/openpilot/common/libparams_c.so"

echo "[5/6] correctness gate 2: Params put/get round-trip (must pass)"
/usr/local/venv/bin/python - <<'PY'
from openpilot.common.params import Params
p = Params()
p.put_bool('DisableDriverDistraction', True)
assert p.get_bool('DisableDriverDistraction') is True, 'put/get round-trip failed'
p.put_bool('DisableDriverDistraction', False)
assert p.get_bool('DisableDriverDistraction') is False, 'reset to False failed'
print('OK: DisableDriverDistraction reads back correctly')
PY

echo "[6/6] reminder — next steps (not automated):"
echo "  cd /data/openpilot && git update-index --assume-unchanged prebuilt"
echo "  then reboot; then verify tmux boot / tmux capture-pane for 'manager'"
echo "DONE: libparams_c.so rebuilt with DisableDriverDistraction."
