#!/usr/bin/env python3
"""
Durable power/shutdown evidence logger for comma three.

WHY:
  The device shuts down "instantly" when the car is turned off, but we cannot
  tell whether the cause is (a) external power being physically removed,
  (b) the INA231 undervoltage alert halting the device
  (/usr/comma/power_drop_monitor.py), or (c) openpilot deciding to shut down
  (should_shutdown -> DoShutdown). A normal loggerd rlog is not fsynced, so an
  abrupt halt loses the interesting seconds.

WHAT:
  * 2 Hz snapshot of every power-relevant signal, written+fsynced per record to
    /data/power-debug/sessions/<boot>.csv
  * transition-only events (ignition, harness, panda presence, rail voltage
    steps, USB/battery state, shutdown params, INA alert GPIO edge) written to
    /data/power-debug/events.log, also fsynced
  * on startup, a human-readable summary of the previous session in
    /data/power-debug/LAST_SESSION.txt so the cause of the last shutdown is
    readable without reading the CSV

Run as root from systemd (tools/power-debug/power-debug.service).
"""

from __future__ import annotations

import datetime
import glob
import os
import select
import shutil
import sys
import time

BASE = "/data/power-debug"
SESSIONS = os.path.join(BASE, "sessions")
EVENT_LOG = os.path.join(BASE, "events.log")
SUMMARY = os.path.join(BASE, "LAST_SESSION.txt")
ALERT_GPIO = "/sys/class/gpio/gpio1264/value"

SAMPLE_HZ = 2.0
MAX_SESSIONS = 10
EVENT_LOG_MAX_BYTES = 5 * 1024 * 1024

# rail thresholds (mV). INA231 alert in power_drop_monitor.py is 4000 mV.
RAIL_WARN_MV = 4500
RAIL_CRIT_MV = 4000
VOLTAGE_STEP_MV = 300

PARAM_KEYS = (
    "IsOffroad", "IsEngaged", "CarBatteryCapacity", "UptimeOffroad",
    "DoShutdown", "ForcePowerDown", "DisablePowerDown", "MaxTimeOffroad",
    "LastPowerDropDetected", "LastAgnosPowerMonitorShutdown", "LastManagerExitReason",
)

FIELDS = [
    "t", "mono", "boot",
    "som_mv", "som_ma", "som_mw",
    "ina_alert",
    "batt_status", "batt_mv", "batt_ma",
    "usb_online", "usb_mv", "usb_ma", "usb_status",
    "pcp_online", "dc_online",
    "panda_n", "panda_type", "panda_mv", "panda_ma", "panda_uptime",
    "ign_line", "ign_can", "harness", "ctrl_ok", "safety", "usb_power_mode",
    "pwr_save", "sbu1", "sbu2", "can_rx", "can_tx", "can_err",
    "periph_mv", "periph_ma", "fan_rpm",
    "started", "bright", "pwr_w", "som_w",
    "is_offroad", "engaged", "do_shutdown", "force_pd", "disable_pd",
    "maxtime", "uptime_offroad",
]


# AGNOS has no usable RTC at boot: the clock sits at the image build date until
# systemd-timesyncd/NTP syncs it. Wall timestamps before this date are bogus.
CLOCK_SANE_AFTER = datetime.datetime(2026, 10, 1)


def clock_is_synced() -> bool:
  if os.path.exists("/run/systemd/timesync/synchronized"):
    return True
  return datetime.datetime.now() >= CLOCK_SANE_AFTER


def now_iso() -> str:
  return datetime.datetime.now().astimezone().isoformat(timespec="milliseconds")


def read_text(path: str) -> str:
  try:
    with open(path) as f:
      return f.read().strip()
  except Exception:
    return ""


def read_int(path: str):
  v = read_text(path)
  try:
    return int(float(v))
  except Exception:
    return ""


class Sink:
  """Append-only file writer that fsyncs every record."""

  def __init__(self, path: str):
    self.path = path
    self.f = open(path, "a", buffering=1)

  def write(self, line: str) -> None:
    try:
      self.f.write(line + "\n")
      self.f.flush()
      os.fsync(self.f.fileno())
    except Exception:
      pass

  def close(self) -> None:
    try:
      self.f.close()
    except Exception:
      pass


def find_ina231() -> str:
  for hw in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
    if read_text(os.path.join(hw, "name")) == "ina231":
      return hw
  return "/sys/class/hwmon/hwmon1"


def power_supply(name: str, field: str):
  return read_int(f"/sys/class/power_supply/{name}/{field}")


def power_supply_status(name: str) -> str:
  return read_text(f"/sys/class/power_supply/{name}/status")


def summarize_boot() -> str:
  """One-line identifier for this boot."""
  try:
    with open("/proc/sys/kernel/random/boot_id") as f:
      return f.read().strip()[:8]
  except Exception:
    return "unknown"


def summarize_previous_session() -> str:
  files = sorted(glob.glob(os.path.join(SESSIONS, "*.csv")))
  if not files:
    return "no previous session recorded"

  csv_path = files[-1]
  name = os.path.basename(csv_path)
  try:
    with open(csv_path) as f:
      lines = f.readlines()
  except Exception as e:
    return f"could not read {name}: {e}"

  if len(lines) < 2:
    return f"{name}: empty"

  header = lines[0].strip().split(",")
  rows = [dict(zip(header, ln.strip().split(","))) for ln in lines[1:] if ln.strip()]
  if not rows:
    return f"{name}: no rows"

  def col(row, key):
    try:
      return float(row.get(key, ""))
    except Exception:
      return None

  first, last = rows[0], rows[-1]
  t0 = first.get("t", "?")
  t1 = last.get("t", "?")
  mono0, mono1 = col(first, "mono"), col(last, "mono")
  duration = (mono1 - mono0) if (mono0 is not None and mono1 is not None) else None

  som = [col(r, "som_mv") for r in rows]
  som = [v for v in som if v is not None]
  panda = [col(r, "panda_mv") for r in rows if str(r.get("harness") or "") not in ("", "notConnected")]
  panda = [v for v in panda if v is not None]
  car_str = (f"min {min(panda):.0f} mV, max {max(panda):.0f} mV, last {panda[-1]:.0f} mV"
             if panda else "n/a (harness reported notConnected for this session)")

  # events that happened after this session started, located by the SESSION marker
  events = []
  try:
    with open(EVENT_LOG) as f:
      all_lines = [ln.rstrip() for ln in f]
    marker = f"SESSION {name}"
    start_idx = max((i for i, ln in enumerate(all_lines) if marker in ln), default=-1)
    if start_idx >= 0:
      events = all_lines[start_idx + 1:]
    else:
      # older logs without a marker: fall back to the first row timestamp
      event_start = datetime.datetime.fromisoformat(t0).timestamp()
      for ln in all_lines:
        try:
          if datetime.datetime.fromisoformat(ln.split(" ")[0]).timestamp() >= event_start:
            events.append(ln)
        except Exception:
          continue
  except Exception:
    pass

  tail = []
  for row in rows[-8:]:
    tail.append("  " + ", ".join(f"{k}={row.get(k)}" for k in
                ("t", "som_mv", "som_ma", "batt_status", "usb_online", "panda_mv",
                 "ign_line", "ign_can", "harness", "do_shutdown", "pwr_w")))

  trig = detect_trigger(rows, events)
  try:
    recorded = datetime.datetime.fromtimestamp(os.path.getmtime(csv_path)).astimezone().isoformat(timespec="seconds")
  except Exception:
    recorded = "unknown"
  lines_out = [
    f"session file : {name}",
    f"recorded     : {recorded}",
    f"window       : {t0} -> {t1}"
    + (f"  ({duration/60:.1f} min)" if duration is not None else ""),
    f"samples      : {len(rows)}",
    f"som rail     : min {min(som) if som else '?'} mV, max {max(som) if som else '?'} mV, last {som[-1] if som else '?'} mV",
    f"car voltage  : {car_str}",
    "",
    "VERDICT:",
    trig,
    "",
    "last rows:",
    *tail,
    "",
    "events after session start:",
    *(events[-40:] if events else ["  (none)"]),
  ]
  return "\n".join(lines_out)


def detect_trigger(rows: list[dict], events: list[str]) -> str:
  def fnum(row, key):
    try:
      return float(row.get(key, ""))
    except Exception:
      return None

  last = rows[-1]
  do_shutdown = str(last.get("do_shutdown")).lower() in ("true", "1")
  force_pd = str(last.get("force_pd")).lower() in ("true", "1")
  harness = str(last.get("harness") or "")

  som = [fnum(r, "som_mv") for r in rows]
  som = [v for v in som if v is not None]
  # panda voltage is only the car-side voltage while the harness is connected
  car = [fnum(r, "panda_mv") for r in rows if str(r.get("harness") or "") not in ("", "notConnected")]
  car = [v for v in car if v is not None]
  usb_online = [fnum(r, "usb_online") for r in rows]
  batt = [str(r.get("batt_status") or "") for r in rows]

  if any("LastPowerDropDetected" in e for e in events):
    return ("  EXTERNAL POWER WAS CUT: /usr/comma/power_drop_monitor.py recorded a drop\n"
            "  and halted the device (see PREP/SHUTDOWN lines above). This is the power\n"
            "  path, not openpilot: 12V disappeared before/while the rail collapsed.")
  if do_shutdown:
    detail = "openpilot requested shutdown (DoShutdown=True in the last sample)."
    if car and car[-1] < 11800:
      detail += f"\n  car voltage was low ({car[-1]:.0f} mV), which triggers low-voltage shutdown."
    try:
      uptime_offroad = float(str(last.get("uptime_offroad") or ""))
      maxtime = float(str(last.get("maxtime") or ""))
      if maxtime and uptime_offroad >= maxtime * 60 - 5:
        detail += f"\n  offroad for {uptime_offroad/60:.0f} min, MaxTimeOffroad is {maxtime:.0f} min."
    except Exception:
      pass
    if str(last.get("ign_line")).lower() in ("false", "0") and str(last.get("ign_can")).lower() in ("false", "0"):
      detail += "\n  ignition was already off; check how long it had been offroad."
    return "  SOFTWARE SHUTDOWN: " + detail
  if force_pd:
    return "  FORCED: ForcePowerDown was set by a user/tool, not by the car."
  if som and som[-1] < RAIL_CRIT_MV:
    return (f"  POWER COLLAPSE: SoM rail fell to {som[-1]:.0f} mV (< {RAIL_CRIT_MV} mV),\n"
            "  i.e. external supply was removed and the internal battery could not hold it.")
  if usb_online and usb_online[0] == 1 and usb_online[-1] == 0:
    return ("  EXTERNAL SUPPLY LOST: usb_online went 1 -> 0 during the session, so the\n"
            "  device switched to its internal battery; it then stopped before writing\n"
            "  a software shutdown request (battery exhausted or the INA alert fired).")
  if "Charging" in batt and batt[-1] != "Charging":
    return ("  EXTERNAL SUPPLY LOST: battery went from Charging to "
            f"{batt[-1]}; the 12V feed dropped.")
  if som and len(som) > 4 and (som[-1] - min(som[-4:])) < -500:
    return "  POWER COLLAPSE: rail dropped more than 500 mV just before the end."
  if harness == "notConnected":
    return ("  INCONCLUSIVE: harness was not connected, so there was no car voltage to\n"
            "  read. The device was running on internal battery. Check whether the OBD\n"
            "  connector was plugged in and whether 12V is present on it.\n"
            "  If the car was on, this means the harness/OBD link itself was down.")
  return ("  INCONCLUSIVE: the log ends without a software shutdown request or a rail\n"
          "  collapse in the last sample (last sample may predate a hard power cut).")


def rotate_event_log() -> None:
  try:
    if os.path.exists(EVENT_LOG) and os.path.getsize(EVENT_LOG) > EVENT_LOG_MAX_BYTES:
      shutil.move(EVENT_LOG, EVENT_LOG + ".1")
  except Exception:
    pass


def prune_sessions() -> None:
  files = sorted(glob.glob(os.path.join(SESSIONS, "*.csv")))
  for path in files[:-MAX_SESSIONS]:
    try:
      os.remove(path)
    except Exception:
      pass


def main() -> int:
  os.makedirs(SESSIONS, exist_ok=True)
  rotate_event_log()
  prune_sessions()

  summary = summarize_previous_session()
  try:
    with open(SUMMARY, "w") as f:
      f.write(f"previous session summary\n\n{summary}\n")
  except Exception:
    pass

  boot = summarize_boot()
  session_path = os.path.join(SESSIONS, f"{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}-{boot}.csv")
  events = Sink(EVENT_LOG)
  session = Sink(session_path)
  session.write(",".join(FIELDS))
  events.write(f"{now_iso()} START boot={boot} session={os.path.basename(session_path)}")
  events.write(f"{now_iso()} SESSION {os.path.basename(session_path)}")
  events.write(f"{now_iso()} PREVIOUS_SESSION_SUMMARY_FILE {SUMMARY}")
  if not clock_is_synced():
    events.write(f"{now_iso()} CLOCK_UNSYNCED wall clock has not been set by NTP yet; "
                 "use the mono column for durations until it syncs")

  print(summary, flush=True)

  # cereal imports are deferred so a broken checkout still leaves a clear error
  sys.path.insert(0, "/data/openpilot")
  try:
    import openpilot.cereal.messaging as messaging
    from openpilot.common.params import Params
  except Exception as e:
    events.write(f"{now_iso()} FATAL cereal/params import failed: {e!r}")
    return 1

  params = Params()
  sm = messaging.SubMaster(
    ["pandaStates", "peripheralState", "deviceState"],
    ignore_alive=["pandaStates", "peripheralState", "deviceState"],
  )
  ina = find_ina231()
  som_mv_path = os.path.join(ina, "in1_input")
  som_ma_path = os.path.join(ina, "curr1_input")

  # INA231 undervoltage alert edge watcher (the GPIO power_drop_monitor polls)
  alert_state = {"value": read_int(ALERT_GPIO), "seen": 0}

  def watch_alert() -> None:
    try:
      f = open(ALERT_GPIO)
    except Exception:
      return
    po = select.poll()
    po.register(f, select.POLLPRI)
    f.read()
    while True:
      try:
        if not po.poll(60000):
          continue
        f.seek(0)
        val = f.read().strip()
        alert_state["value"] = int(val) if val else alert_state["value"]
        alert_state["seen"] += 1
        if alert_state["value"] == 0:
          events.write(
            f"{now_iso()} INA_ALERT_ASSERTED som_mv={read_int(som_mv_path)} "
            f"som_ma={read_int(som_ma_path)} batt_status={power_supply_status('battery')} "
            f"batt_mv={power_supply('battery', 'voltage_now')}")
      except Exception:
        time.sleep(1)

  import threading
  threading.Thread(target=watch_alert, daemon=True).start()

  def param_bool(key: str) -> bool:
    try:
      v = params.get(key)
      if v is None:
        return False
      return v in (b"1", "1", 1, True)
    except Exception:
      return False

  def param_num(key: str):
    try:
      v = params.get(key)
      return "" if v is None else v.decode(errors="replace") if isinstance(v, bytes) else v
    except Exception:
      return ""

  prev: dict = {}
  last_heartbeat = 0.0
  last_som = None
  last_param_seen = {k: param_num(k) for k in PARAM_KEYS}

  def emit_event(reason: str, row: dict) -> None:
    keys = ("t", "som_mv", "som_ma", "batt_status", "batt_mv", "usb_online", "usb_mv",
            "panda_n", "panda_mv", "panda_ma", "ign_line", "ign_can", "harness",
            "started", "is_offroad", "do_shutdown", "pwr_w")
    events.write(f"{now_iso()} {reason} " + " ".join(f"{k}={row.get(k)}" for k in keys))

  clock_fixed = clock_is_synced()
  session_start_wall = datetime.datetime.now()

  while True:
    try:
      sm.update(0)

      if not clock_fixed:
        wall = datetime.datetime.now()
        if clock_is_synced() and abs((wall - session_start_wall).total_seconds()) > 3600:
          clock_fixed = True
          session.close()
          new_path = os.path.join(SESSIONS, f"{wall.strftime('%Y%m%d-%H%M%S')}-{boot}.csv")
          try:
            os.rename(session_path, new_path)
            session_path = new_path
            session = Sink(session_path)
            events.write(f"{now_iso()} CLOCK_SYNCED renamed session to {os.path.basename(session_path)}")
            events.write(f"{now_iso()} SESSION {os.path.basename(session_path)}")
          except Exception as e:
            events.write(f"{now_iso()} CLOCK_RENAME_FAILED {e!r}")
            session = Sink(session_path)

      ps_list = sm["pandaStates"]
      periph = sm["peripheralState"]
      dev = sm["deviceState"]

      ps = ps_list[0] if len(ps_list) else None
      som_mv = read_int(som_mv_path)
      som_ma = read_int(som_ma_path)
      prev_som = last_som
      last_som = som_mv

      row = {
        "t": now_iso(),
        "mono": f"{time.monotonic():.3f}",
        "boot": boot,
        "som_mv": som_mv,
        "som_ma": som_ma,
        "som_mw": (som_mv * som_ma // 1000) if isinstance(som_mv, int) and isinstance(som_ma, int) else "",
        "ina_alert": read_int(ALERT_GPIO),
        "batt_status": power_supply_status("battery") or "",
        "batt_mv": power_supply("battery", "voltage_now"),
        "batt_ma": power_supply("battery", "current_now"),
        "usb_online": power_supply("usb", "online"),
        "usb_mv": power_supply("usb", "voltage_now"),
        "usb_ma": power_supply("usb", "current_now"),
        "usb_status": power_supply_status("usb") or "",
        "pcp_online": power_supply("pc_port", "online"),
        "dc_online": power_supply("dc", "online"),
        "panda_n": len(ps_list),
        "panda_type": str(ps.pandaType) if ps is not None else "",
        "panda_mv": ps.voltage if ps is not None else "",
        "panda_ma": ps.current if ps is not None else "",
        "panda_uptime": ps.uptime if ps is not None else "",
        "ign_line": ps.ignitionLine if ps is not None else "",
        "ign_can": ps.ignitionCan if ps is not None else "",
        "harness": str(ps.harnessStatus) if ps is not None else "",
        "ctrl_ok": ps.controlsAllowed if ps is not None else "",
        "safety": str(ps.safetyModel) if ps is not None else "",
        "usb_power_mode": str(ps.deprecated.usbPowerMode) if ps is not None else "",
        "pwr_save": ps.powerSaveEnabled if ps is not None else "",
        "sbu1": f"{ps.sbu1Voltage:.2f}" if ps is not None else "",
        "sbu2": f"{ps.sbu2Voltage:.2f}" if ps is not None else "",
        "can_rx": ps.canState0.totalRxCnt if ps is not None else "",
        "can_tx": ps.canState0.totalTxCnt if ps is not None else "",
        "can_err": ps.canState0.totalErrorCnt if ps is not None else "",
        "periph_mv": periph.voltage,
        "periph_ma": periph.current,
        "fan_rpm": periph.fanSpeedRpm,
        "started": dev.started,
        "bright": f"{dev.screenBrightnessPercent:.2f}",
        "pwr_w": f"{dev.powerDrawW:.2f}",
        "som_w": f"{dev.somPowerDrawW:.2f}",
        "is_offroad": param_bool("IsOffroad"),
        "engaged": param_bool("IsEngaged"),
        "do_shutdown": param_bool("DoShutdown"),
        "force_pd": param_bool("ForcePowerDown"),
        "disable_pd": param_bool("DisablePowerDown"),
        "maxtime": param_num("MaxTimeOffroad"),
        "uptime_offroad": param_num("UptimeOffroad"),
      }
      session.write(",".join(str(row.get(k, "")) for k in FIELDS))

      # ---- transition events ----
      if not prev:
        emit_event("FIRST_SAMPLE", row)
      else:
        if row["panda_n"] != prev.get("panda_n"):
          emit_event(f"PANDA_COUNT {prev.get('panda_n')}->{row['panda_n']}", row)
        if row["panda_uptime"] != prev.get("panda_uptime") and row["panda_n"]:
          pu, ppu = row["panda_uptime"], prev.get("panda_uptime")
          if isinstance(pu, int) and isinstance(ppu, int) and pu < ppu:
            emit_event(f"PANDA_REBOOTED uptime {ppu}->{pu}", row)
        for key in ("ign_line", "ign_can", "harness", "batt_status", "usb_online",
                    "ina_alert", "started", "pwr_save", "usb_power_mode"):
          if row[key] != prev.get(key):
            emit_event(f"{key.upper()} {prev.get(key)}->{row[key]}", row)
        if isinstance(som_mv, int) and isinstance(prev_som, int):
          if abs(som_mv - prev_som) >= VOLTAGE_STEP_MV:
            emit_event(f"RAIL_STEP {prev_som}->{som_mv}mV", row)
          if som_mv < RAIL_WARN_MV <= prev_som:
            emit_event("RAIL_WARN", row)
          if som_mv < RAIL_CRIT_MV <= prev_som:
            emit_event("RAIL_CRIT", row)
        if row["do_shutdown"] and not prev.get("do_shutdown"):
          emit_event("DO_SHUTDOWN_SET", row)

      for key in PARAM_KEYS:
        val = param_num(key)
        if val != last_param_seen.get(key):
          events.write(f"{now_iso()} PARAM {key} {last_param_seen.get(key)!r}->{val!r}")
          last_param_seen[key] = val

      if time.monotonic() - last_heartbeat > 60:
        last_heartbeat = time.monotonic()
        emit_event("HEARTBEAT", row)

      prev = row
      time.sleep(1.0 / SAMPLE_HZ)
    except Exception as e:
      events.write(f"{now_iso()} LOOP_ERROR {e!r}")
      time.sleep(1)


if __name__ == "__main__":
  sys.exit(main())
