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
#
# FULL DEPLOY SEQUENCE (from the device shell):
#   cd /data/openpilot
#   git remote -v                         # resolve the fork remote; do not assume "origin"
#   git fetch <fork-remote> sync/2026-08-25
#   git checkout -B sync/2026-08-25 <fork-remote>/sync/2026-08-25
#   bash tools/disable-distraction-alerts/deploy_libparams.sh
#   git update-index --assume-unchanged prebuilt
#   sudo reboot

set -euo pipefail

REPO="${REPO:-/data/openpilot}"
TOOL_DIR="${TOOL_DIR:-$REPO/tools/disable-distraction-alerts}"
INSTALL_LIBPARAMS="${INSTALL_LIBPARAMS:-1}"
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
# common/hardware/hw.h selects HardwareComma with __COMMA_HARDWARE__.
# __TICI__ is an obsolete selector here and silently defaults Params to
# $HOME/.comma/params instead of the persistent /data/params store.
clang++ -std=c++1z -O2 -fPIC -pipe -D__COMMA_HARDWARE__ -mcpu=cortex-a57 \
  -I"$TOOL_DIR/shim" -I"$REPO/openpilot" -I"$REPO" \
  "$REPO/openpilot/common/params_c.cc" \
  "$REPO/openpilot/common/params.cc" \
  "$REPO/openpilot/common/util.cc" \
  "$TOOL_DIR/shim/swaglog_stub.cc" \
  -shared -pthread -Wl,--as-needed -Wl,--no-undefined \
  -o "$OUT/libparams_c.so.new"

echo "[2/6] correctness gate 1: candidate key, type, params root, and isolated writes"
if [ ! -d /data/params ] || [ ! -d /data/params/d ]; then
  echo "ERROR: refusing candidate probe because /data/params is incomplete" >&2
  exit 1
fi
env -u PARAMS_ROOT -u OPENPILOT_PREFIX \
  CANDIDATE="$OUT/libparams_c.so.new" PROBE_ROOT="$OUT/params-probe" \
  /usr/local/venv/bin/python - <<'PY'
import ctypes
import os


class ParamsBuffer(ctypes.Structure):
  _fields_ = [("data", ctypes.c_void_p), ("size", ctypes.c_size_t)]


lib = ctypes.CDLL(os.environ["CANDIDATE"])
lib.params_create.argtypes = [ctypes.c_char_p, ctypes.c_size_t]
lib.params_create.restype = ctypes.c_void_p
lib.params_destroy.argtypes = [ctypes.c_void_p]
lib.params_last_error.restype = ctypes.c_char_p
lib.params_check_key.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
lib.params_check_key.restype = ctypes.c_bool
lib.params_get_key_type.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
lib.params_get_key_type.restype = ctypes.c_int
lib.params_get_bool.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_bool]
lib.params_get_bool.restype = ctypes.c_bool
lib.params_put_bool.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_bool, ctypes.c_bool]
lib.params_put_bool.restype = ctypes.c_int
lib.params_get_path.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t]
lib.params_get_path.restype = ParamsBuffer


def copy_buffer(value):
  return None if value.data is None else ctypes.string_at(value.data, value.size)


def check_error(context):
  if error := lib.params_last_error():
    raise RuntimeError(f"{context}: {error.decode()}")


key = b"DisableDriverDistraction"
handle = lib.params_create(b"", 0)
try:
  assert handle, "candidate params_create failed"
  check_error("params_create")
  assert lib.params_check_key(handle, key), "candidate does not recognize custom key"
  check_error("params_check_key")
  assert lib.params_get_key_type(handle, key) == 1, "custom key is not BOOL"
  check_error("params_get_key_type")

  actual_path = copy_buffer(lib.params_get_path(handle, b"", 0)).decode()
  check_error("params_get_path")
  expected_path = "/data/params/d"
  assert actual_path == expected_path, f"wrong default params path: {actual_path}"
finally:
  if handle:
    lib.params_destroy(handle)

# Exercise writes only in an isolated temporary store, never in /data/params.
probe_root = os.environ["PROBE_ROOT"].encode()
probe = lib.params_create(probe_root, len(probe_root))
try:
  assert probe, "isolated params_create failed"
  check_error("isolated params_create")
  assert lib.params_put_bool(probe, key, True, True) == 0, "isolated put_bool failed"
  check_error("isolated put_bool true")
  assert bool(lib.params_get_bool(probe, key, False)) is True, "isolated get_bool failed"
  check_error("isolated get_bool true")
  assert lib.params_put_bool(probe, key, False, True) == 0, "isolated reset failed"
  check_error("isolated put_bool false")
  assert bool(lib.params_get_bool(probe, key, False)) is False, "isolated reset read failed"
  check_error("isolated get_bool false")
finally:
  if probe:
    lib.params_destroy(probe)

print("OK: candidate key/type/default params root and isolated round-trip validated")
PY

echo "[3/6] build fingerprint"
md5sum "$OUT/libparams_c.so.new"

if [ "$INSTALL_LIBPARAMS" != "1" ]; then
  echo "DRY RUN: candidate validated; INSTALL_LIBPARAMS=$INSTALL_LIBPARAMS, so nothing was installed"
  exit 0
fi

echo "[4/6] offroad gate and unique backup"
PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}" /usr/local/venv/bin/python - <<'PY'
from openpilot.common.params import Params
assert Params("/data/params").get_bool("IsOffroad") is True, "device is not offroad"
print("OK: device is offroad")
PY
BACKUP_DIR="${BACKUP_DIR:-$REPO/.recovery/libparams_c}"
BACKUP_STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$BACKUP_DIR"
cp -p "$REPO/openpilot/common/libparams_c.so" "$BACKUP_DIR/libparams_c.so.$BACKUP_STAMP"

echo "[5/6] atomically installing candidate"
TARGET="$REPO/openpilot/common/libparams_c.so"
STAGED="$TARGET.new.$BACKUP_STAMP"
install -m 0755 "$OUT/libparams_c.so.new" "$STAGED"
mv -f "$STAGED" "$TARGET"

echo "[6/6] runtime verification (automatic rollback on failure)"
if ! PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}" /usr/local/venv/bin/python - <<'PY'
from openpilot.common.params import Params
p = Params()
assert p.get_param_path() == "/data/params/d"
assert p.check_key("DisableDriverDistraction")
print("OK: runtime wrapper uses /data/params and recognizes DisableDriverDistraction")
PY
then
  echo "ERROR: runtime verification failed; restoring previous libparams_c.so" >&2
  ROLLBACK_STAGED="$TARGET.rollback.$BACKUP_STAMP"
  install -m 0755 "$BACKUP_DIR/libparams_c.so.$BACKUP_STAMP" "$ROLLBACK_STAGED"
  mv -f "$ROLLBACK_STAGED" "$TARGET"
  exit 1
fi

echo "Reminder — next steps (not automated):"
echo "  cd /data/openpilot && git update-index --assume-unchanged prebuilt"
echo "  then reboot; then verify tmux boot / tmux capture-pane for 'manager'"
echo "DONE: libparams_c.so rebuilt with DisableDriverDistraction."
