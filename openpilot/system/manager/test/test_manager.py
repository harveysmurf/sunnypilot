#!/usr/bin/env python3

import os
import unittest
import signal
import time

from opendbc.car.structs import car
from openpilot.common.test import OpenpilotTestCase
from openpilot.common.params import Params
import openpilot.system.manager.manager as manager
from openpilot.system.manager.process import NativeProcess, PythonProcess, ensure_running, join_process
from openpilot.system.manager.process_config import managed_processes, procs
from openpilot.common.hardware import HARDWARE

os.environ['FAKEUPLOAD'] = "1"

MAX_STARTUP_TIME = 3
BLACKLIST_PROCS = ['manage_athenad', 'pandad', 'pigeond']


def crash_on_start(*_args) -> None:
  """launcher stand-in: the daemon dies as soon as it is started"""
  return


def sleep_on_start(*_args) -> None:
  """launcher stand-in: the daemon stays up, so a respawn can be observed"""
  time.sleep(30)


def always_run(*_) -> bool:
  return True


class TestManager(OpenpilotTestCase):
  def setup_method(self):
    HARDWARE.set_power_save(False)

    # ensure clean CarParams
    params = Params()
    params.clear_all()

  def teardown_method(self):
    manager.manager_cleanup()

  def test_duplicate_procs(self):
    assert len(procs) == len(managed_processes), "Duplicate process names"

  def test_blacklisted_procs(self):
    # TODO: ensure there are blacklisted procs until we have a dedicated test
    assert len(BLACKLIST_PROCS), "No blacklisted procs to test not_run"

  def test_set_params_with_default_value(self):
    params = Params()
    params.clear_all()

    os.environ['PREPAREONLY'] = '1'
    manager.main()
    for k in params.all_keys():
      default_value = params.get_default_value(k)
      if default_value is not None:
        assert params.get(k) == default_value
    assert params.get("OpenpilotEnabledToggle")
    assert params.get("RouteCount") == 0

  def test_crashed_daemon_is_respawned(self, subtests):
    # a crashed daemon must come back: start() used to return early on the dead Process
    # object, leaving managerState at shouldBeRunning=True/running=False for the rest of
    # the drive, which is the state selfdrived blocks engagement on
    test_procs = [
      PythonProcess("test_crashd", "openpilot.system.timed", should_run=always_run),
      NativeProcess("test_crashd", "", ["/bin/true"], should_run=always_run),
    ]

    for p in test_procs:
      with subtests.test(proc_type=type(p).__name__):
        p.launcher = crash_on_start
        p.start()
        join_process(p.proc, 5)
        assert p.proc.exitcode == 0, "daemon did not crash as expected"

        state = p.get_process_state_msg()
        assert state.shouldBeRunning and not state.running, "crashed daemon is not reported as down"

        p.launcher = sleep_on_start
        dead_pid = p.proc.pid
        p.start()
        try:
          assert p.proc.pid != dead_pid, "crashed daemon was not respawned"
          assert p.proc.is_alive(), "respawned daemon is not running"

          state = p.get_process_state_msg()
          assert state.running and state.shouldBeRunning
        finally:
          p.stop(retry=False)

  def test_ensure_running_respawns_crashed_daemon(self):
    # the manager loop calls ensure_running() about once a second, so a crash has to
    # clear on the next pass instead of waiting for a manager restart
    p = PythonProcess("test_crashd", "openpilot.system.timed", should_run=always_run)
    CP = car.CarParams.new_message()

    p.launcher = crash_on_start
    ensure_running([p], started=True, params=Params(), CP=CP)
    join_process(p.proc, 5)
    assert p.proc.exitcode == 0, "daemon did not crash as expected"

    p.launcher = sleep_on_start
    dead_pid = p.proc.pid
    ensure_running([p], started=True, params=Params(), CP=CP)
    try:
      assert p.proc.pid != dead_pid, "crashed daemon was not respawned"
      assert p.proc.is_alive(), "respawned daemon is not running"
    finally:
      p.stop(retry=False)

  def test_running_daemon_is_not_restarted(self):
    # the other side of the respawn fix: a healthy daemon keeps its pid across passes
    p = PythonProcess("test_lived", "openpilot.system.timed", should_run=always_run)
    CP = car.CarParams.new_message()
    p.launcher = sleep_on_start

    p.start()
    try:
      pid = p.proc.pid
      ensure_running([p], started=True, params=Params(), CP=CP)
      ensure_running([p], started=True, params=Params(), CP=CP)

      assert p.proc.pid == pid, "healthy daemon was restarted"
      assert p.proc.is_alive(), "healthy daemon died"
    finally:
      p.stop(retry=False)

  @unittest.skip("this test is flaky the way it's currently written, should be moved to test_onroad")
  def test_clean_exit(self, subtests):
    """
      Ensure all processes exit cleanly when stopped.
    """
    HARDWARE.set_power_save(False)
    manager.manager_init()

    CP = car.CarParams.new_message()
    procs = ensure_running(managed_processes.values(), True, Params(), CP, not_run=BLACKLIST_PROCS)

    time.sleep(10)

    for p in procs:
      with subtests.test(proc=p.name):
        state = p.get_process_state_msg()
        assert state.running, f"{p.name} not running"
        exit_code = p.stop(retry=False)

        assert p.name not in BLACKLIST_PROCS, f"{p.name} was started"

        assert exit_code is not None, f"{p.name} failed to exit"

        # TODO: interrupted blocking read exits with 1 in cereal. use a more unique return code
        exit_codes = [0, 1]
        if p.sigkill:
          exit_codes = [-signal.SIGKILL]
        assert exit_code in exit_codes, f"{p.name} died with {exit_code}"


if __name__ == "__main__":
  unittest.main()
