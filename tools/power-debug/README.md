# power-debug — why did the device shut down?

Records fsynced evidence so an abrupt power-off does not erase the answer.

## Install (on the device)

```
cd /data/openpilot
sudo bash tools/power-debug/install.sh
```

Runs `power-debug.service` as root from boot. Survives reboots. An AGNOS update
replaces the read-only rootfs and removes the unit — re-run the installer after
one.

## What it writes

| Path | Contents |
|---|---|
| `/data/power-debug/sessions/*.csv` | 2 Hz snapshot, one row per 0.5 s, `fsync` per row |
| `/data/power-debug/events.log` | transitions only: ignition, harness, panda presence/reboot, rail steps, INA alert, USB/battery state, shutdown params |
| `/data/power-debug/LAST_SESSION.txt` | human summary of the previous session, written at boot, with a verdict |

Latest session only (10 kept, ~15 MB each at 2 Hz, pruned automatically).

## Signals that answer the question

| Question | Columns |
|---|---|
| Was external power present? | `usb_online`, `usb_mv`, `usb_power_mode`, `pcp_online`, `batt_status` |
| Did the 12 V / OBD supply drop? | `panda_mv` (harness voltage), `harness`, `can_rx`, `can_tx`, `ign_line`, `ign_can` |
| Did the SoM rail collapse? | `som_mv`, `som_ma`, `ina_alert` (1 = alert deasserted, 0 = undervoltage asserted) |
| Did openpilot decide to shut down? | `do_shutdown`, `force_pd`, `is_offroad`, `uptime_offroad`, `maxtime` |
| Was the internal battery carrying it? | `batt_status=Discharging`, `batt_mv`, `batt_ma` |

## Reading a shutdown

1. `cat /data/power-debug/LAST_SESSION.txt` — the verdict block names the likely
   cause and dumps the last 8 samples plus every event since the session began.
2. `grep -E "INA_ALERT|RAIL_|DO_SHUTDOWN|PARAM LastPowerDropDetected" /data/power-debug/events.log`
   — the exact moment of failure.

Verdicts it distinguishes:

* **power was cut** — `LastPowerDropDetected` PREP/SHUTDOWN written by
  `/usr/comma/power_drop_monitor.py`, or the rail stepping below 4000 mV.
* **openpilot shut down** — `DoShutdown` set by `hardwared` (5 min offroad,
  low car voltage, `MaxTimeOffroad`, battery capacity exhausted).
* **forced** — `ForcePowerDown` param.
* **inconclusive** — logger stopped before the last sample (hard power cut, no
  battery ride-through).

## Uninstall

```
sudo systemctl disable --now power-debug.service power-debug-shutdown.service
sudo mount -o remount,rw / \
  && sudo rm /etc/systemd/system/power-debug.service /etc/systemd/system/power-debug-shutdown.service \
  && sudo systemctl daemon-reload \
  && sudo mount -o remount,ro /
```

A `SYSTEM_SHUTDOWN` line in `events.log` means systemd was asked to halt/reboot
gracefully (openpilot's `DoShutdown`, a manual reboot, or an AGNOS update). No
such line means the device lost power or halted abruptly.
