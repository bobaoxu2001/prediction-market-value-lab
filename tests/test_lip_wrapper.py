"""The cron wrapper: every run leaves a start and an end record, the end record carries
the recorder's exact exit code and one status, and the wrapper exits with that code.

The recorder is replaced by a fake ``python`` that behaves as told (exit with a code,
wait to be signalled, die by SIGKILL), so the real script's control flow is what is
tested - traps, the wait loop, the exit code - not a model of it.
"""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
WRAPPER = REPO / "scripts" / "record_lip_day.sh"

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="bash wrapper")

FAKE_PYTHON = r"""#!/bin/bash
# $1=-m $2=pmvl_worker.lip_cli $3=command
case "$3" in
  record-day)
    echo "$PMVL_LIP_RUN_ID" > "$FAKE_DIR/run_id"
    case "$FAKE_MODE" in
      exit:*) exit "${FAKE_MODE#exit:}" ;;
      wait)
        trap 'echo terminated > "$FAKE_DIR/recorder_got_term"; exit 143' TERM
        echo $$ > "$FAKE_DIR/recorder_pid"
        for _ in $(seq 1 200); do sleep 0.05; done
        exit 0 ;;
      kill9)
        echo $$ > "$FAKE_DIR/recorder_pid"
        kill -9 $$ ;;
    esac ;;
  run-status) echo "run-status $*" >> "$FAKE_DIR/calls" ;;
  manifest) echo "manifest $4" >> "$FAKE_DIR/calls" ;;
esac
exit 0
"""


def _setup(tmp_path: Path, mode: str) -> tuple[Path, dict[str, str]]:
    repo = tmp_path / "repo"
    (repo / "artifacts" / "forward" / "lip-daily").mkdir(parents=True)
    fake = tmp_path / "python"
    fake.write_text(FAKE_PYTHON)
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    env = {**os.environ, "PMVL_LIP_REPO": str(repo), "PMVL_LIP_PYTHON": str(fake),
           "FAKE_MODE": mode, "FAKE_DIR": str(tmp_path)}
    return repo, env


def _records(repo: Path) -> list[dict]:
    lines = (repo / "artifacts" / "forward" / "lip-daily" / "runs.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


@pytest.mark.parametrize("code,status,reason", [
    (0, "SUCCESS", "recorded"),
    (6, "INTENTIONAL_SKIP", "skipped_day"),
    (1, "RECORDER_FAILURE", "uncaught_exception"),
    (3, "RECORDER_FAILURE", "refused_inside_maker_window"),
    (4, "RECORDER_FAILURE", "lock_held"),
    (9, "UNKNOWN_FAILURE", "exit_9"),
])
def test_every_exit_is_recorded_with_its_code_and_the_code_is_preserved(tmp_path, code, status, reason):
    repo, env = _setup(tmp_path, f"exit:{code}")
    done = subprocess.run(["bash", str(WRAPPER)], env=env, timeout=30)
    assert done.returncode == code, "the wrapper exits with the recorder's own code - 6 stays 6"
    start, end = _records(repo)
    assert start["event"] == "start" and end["event"] == "end"
    assert start["run_id"] == end["run_id"] == (tmp_path / "run_id").read_text().strip()
    assert (end["exit_code"], end["status"], end["reason"]) == (code, status, reason)
    skipped = repo / "artifacts" / "forward" / "lip-daily" / "skipped.log"
    assert skipped.exists() == (code != 0)
    assert "run-status" in (tmp_path / "calls").read_text(), "every run records the missed days"


def test_a_signal_to_the_wrapper_is_passed_to_the_recorder_and_recorded(tmp_path):
    repo, env = _setup(tmp_path, "wait")
    proc = subprocess.Popen(["bash", str(WRAPPER)], env=env)
    for _ in range(100):
        if (tmp_path / "recorder_pid").exists():
            break
        time.sleep(0.05)
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=30) == 143
    assert (tmp_path / "recorder_got_term").exists(), "the recorder was told, so it can checkpoint"
    end = _records(repo)[-1]
    assert (end["status"], end["reason"], end["exit_code"]) == ("SIGNALLED", "signal_15", 143)


def test_a_recorder_killed_outright_is_recorded_as_signalled(tmp_path):
    repo, env = _setup(tmp_path, "kill9")
    done = subprocess.run(["bash", str(WRAPPER)], env=env, timeout=30)
    assert done.returncode == 137
    end = _records(repo)[-1]
    assert (end["status"], end["reason"]) == ("SIGNALLED", "signal_9")


def test_a_wrapper_that_fails_before_the_recorder_still_leaves_both_records(tmp_path):
    repo, env = _setup(tmp_path, "exit:0")
    env["PMVL_LIP_PYTHON"] = str(tmp_path / "missing-python")
    done = subprocess.run(["bash", str(WRAPPER)], env=env, timeout=30)
    assert done.returncode != 0
    start, end = _records(repo)
    assert start["event"] == "start" and end["event"] == "end"
    assert end["status"] in ("UNKNOWN_FAILURE", "RECORDER_FAILURE") and end["exit_code"] == done.returncode


@pytest.mark.parametrize("sig", [signal.SIGQUIT, signal.SIGUSR1, signal.SIGHUP])
def test_any_catchable_signal_still_leaves_an_end_record(tmp_path, sig):
    repo, env = _setup(tmp_path, "wait")
    proc = subprocess.Popen(["bash", str(WRAPPER)], env=env)
    for _ in range(100):
        if (tmp_path / "recorder_pid").exists():
            break
        time.sleep(0.05)
    proc.send_signal(sig)
    assert proc.wait(timeout=30) == 143, "the recorder was stopped with TERM and exited 128+15"
    events = [r["event"] for r in _records(repo)]
    assert events == ["start", "end"]
    assert _records(repo)[-1]["status"] == "SIGNALLED"
