#!/usr/bin/env python3
"""
Live OBD / harness link check for comma three.

Answers "is the OBD connection OK right now?" from the signals the device
actually has:

  * pandaState.harnessStatus  - does the panda detect the OBD harness at all
  * pandaState.voltage        - is 12 V present on the harness (vs ~5 V USB)
  * CAN rx/tx counters        - is the car talking
  * ignitionLine/ignitionCan  - is the car awake on the wire
  * CarParams / fingerprint   - did openpilot recognise the car

Run on the device:

  python3 /data/openpilot/tools/power-debug/obd_check.py
  python3 /data/openpilot/tools/power-debug/obd_check.py --json

It only reads; it does not touch the car or params.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

WINDOW_S = 3.5


def read_text(path: str) -> str:
  try:
    with open(path) as f:
      return f.read().strip()
  except Exception:
    return ""


def power_supply(name: str, field: str) -> str:
  return read_text(f"/sys/class/power_supply/{name}/{field}")


def can_totals(ps) -> tuple[int, int, int]:
  rx = tx = err = 0
  for cs_name in ("canState0", "canState1", "canState2"):
    try:
      cs = getattr(ps, cs_name)
      rx += cs.totalRxCnt
      tx += cs.totalTxCnt
      err += cs.totalErrorCnt
    except Exception:
      pass
  return rx, tx, err


def ensure_device_venv() -> None:
  """Re-exec under the openpilot venv when started with the system python."""
  try:
    import capnp  # noqa: F401
    return
  except Exception:
    pass
  venv = "/usr/local/venv"
  py = os.path.join(venv, "bin", "python")
  # the venv python is a symlink to /usr/bin/python3.12, so compare prefixes
  if os.path.exists(py) and sys.prefix != venv and not os.environ.get("OBD_CHECK_REEXEC"):
    os.environ["OBD_CHECK_REEXEC"] = "1"
    os.execv(py, [py] + sys.argv)


def main() -> int:
  ensure_device_venv()
  ap = argparse.ArgumentParser()
  ap.add_argument("--json", action="store_true", help="machine readable output")
  ap.add_argument("--window", type=float, default=WINDOW_S, help="sample window in seconds")
  args = ap.parse_args()

  sys.path.insert(0, "/data/openpilot")
  try:
    import openpilot.cereal.messaging as messaging
    from openpilot.common.params import Params
  except Exception as e:
    print(f"cannot import openpilot cereal/params: {e!r}", file=sys.stderr)
    return 2

  sm = messaging.SubMaster(["pandaStates"], ignore_alive=["pandaStates"])
  params = Params()

  samples = []
  t0 = time.monotonic()
  while time.monotonic() - t0 < args.window:
    sm.update(100)
    ps_list = sm["pandaStates"]
    if not ps_list:
      time.sleep(0.05)
      continue
    ps = ps_list[0]
    rx, tx, err = can_totals(ps)
    samples.append({
      "t": time.monotonic(),
      "harness": str(ps.harnessStatus),
      "voltage": int(ps.voltage),
      "current": int(ps.current),
      "ign_line": bool(ps.ignitionLine),
      "ign_can": bool(ps.ignitionCan),
      "rx": rx,
      "tx": tx,
      "err": err,
      "bus_off": bool(ps.canState0.busOff),
      "err_warn": bool(ps.canState0.errorWarning),
      "panda_type": str(ps.pandaType),
      "heartbeat_lost": bool(ps.heartbeatLost),
      "sbu1": round(float(ps.sbu1Voltage), 2),
      "sbu2": round(float(ps.sbu2Voltage), 2),
    })
    time.sleep(0.05)

  if not samples:
    print("No pandaStates received: is pandad running? (check `pgrep -af pandad`)")
    return 2

  first, last = samples[0], samples[-1]
  dt = max(last["t"] - first["t"], 1e-6)
  rx_rate = (last["rx"] - first["rx"]) / dt
  harness_seen = sorted({s["harness"] for s in samples})
  max_v = max(s["voltage"] for s in samples)
  any_ign = any(s["ign_line"] or s["ign_can"] for s in samples)

  # CarParams is only written once a car has been fingerprinted; CarParamsPersistent survives wipes.
  recognized = None
  for key in ("CarParams", "CarParamsPersistent"):
    raw = params.get(key)
    if not raw:
      continue
    try:
      from opendbc.car.structs import car
      cp = messaging.log_from_bytes(raw, car.CarParams)
      recognized = f"{cp.carFingerprint} ({cp.brand})"
      break
    except Exception as e:
      recognized = f"{key} present but unparseable ({e.__class__.__name__})"

  usb_online = power_supply("usb", "online")
  batt_status = power_supply("battery", "status")
  batt_mv = power_supply("battery", "voltage_now")

  harness_connected = any(h not in ("notConnected", "", "unknown") for h in harness_seen)
  twelve_volts = max_v >= 9000

  if harness_connected and twelve_volts and rx_rate > 1:
    verdict = "OBD LINK OK: harness detected, 12 V present, CAN traffic flowing."
  elif harness_connected and twelve_volts and rx_rate <= 1:
    verdict = ("HARNESS + 12 V OK, NO CAN: the car is likely asleep (ignition off). "
               "Re-run with the ignition on to confirm the bus.")
  elif harness_connected and not twelve_volts:
    verdict = ("HARNESS DETECTED BUT NO 12 V: OBD pin 16 feed missing - fuse, "
               "adapter wiring, or switched supply.")
  elif not harness_connected and twelve_volts:
    verdict = ("12 V PRESENT BUT HARNESS NOT DETECTED: harness/adapter detection "
               "problem (check the OBD-C connector and the harness orientation).")
  elif not harness_connected and not twelve_volts:
    verdict = ("NOT CONNECTED TO A CAR: no harness detected and only ~5 V on the "
               "panda, i.e. bench/USB power. OBD health cannot be judged from here.")
  else:
    verdict = "unexpected state, see raw values below"

  result = {
    "verdict": verdict,
    "harness_seen": harness_seen,
    "car_voltage_max_mV": max_v,
    "car_voltage_last_mV": last["voltage"],
    "can_rx_per_s": round(rx_rate, 1),
    "can_tx_total": last["tx"],
    "can_errors_total": last["err"],
    "can_bus_off": last["bus_off"],
    "can_error_warning": last["err_warn"],
    "ignition": {"line": any(s["ign_line"] for s in samples),
                 "can": any(s["ign_can"] for s in samples)},
    "panda": {"type": last["panda_type"], "heartbeat_lost": last["heartbeat_lost"],
              "sbu1": last["sbu1"], "sbu2": last["sbu2"], "current_mA": last["current"]},
    "recognized_car": recognized,
    "usb": {"online": usb_online, "battery_status": batt_status, "battery_mV": batt_mv},
    "window_s": round(dt, 2),
    "samples": len(samples),
  }

  if args.json:
    print(json.dumps(result, indent=2))
    return 0

  print(f"OBD / harness check ({result['window_s']}s, {result['samples']} samples)")
  print(f"  verdict      : {verdict}")
  print(f"  harness      : {', '.join(harness_seen)}")
  print(f"  car voltage  : last {last['voltage']} mV, max {max_v} mV "
        f"({'12 V present' if twelve_volts else 'no 12 V on harness'})")
  print(f"  CAN          : {rx_rate:6.1f} msg/s rx, {last['tx']} tx total, "
        f"{last['err']} errors, busOff={last['bus_off']}, errWarn={last['err_warn']}")
  print(f"  ignition     : line={any(s['ign_line'] for s in samples)} "
        f"can={any(s['ign_can'] for s in samples)}")
  print(f"  panda        : {last['panda_type']}, heartbeatLost={last['heartbeat_lost']}, "
        f"SBU1={last['sbu1']}V SBU2={last['sbu2']}V, {last['current']} mA")
  print(f"  car params   : {recognized if recognized else '(none: car never fingerprinted)'}")
  print(f"  usb power    : online={usb_online}, battery={batt_status} ({batt_mv} uV)")
  return 0


if __name__ == "__main__":
  sys.exit(main())
