"""Host power telemetry, because a dead battery is a collection outage.

On 2026-08-28 the collector host reached 1% charge and entered Low Power Sleep
for 2.45 hours. Three station-days lost their frozen decision instants and the
first post-fix UTC cron never fired. Every software signal was green throughout:
the process was alive, the scheduler was configured correctly, the database was
responsive. Nothing in the system could see the cause.

This module exists so that the next time it happens the report says "battery"
rather than sending someone to read query plans.

It does not prevent anything. `caffeinate -i` blocks idle sleep and cannot
override battery-exhaustion hibernate, and neither can this. Telemetry is
observability, not resilience - the only actual fix is continuous power.

Strictly operational. Nothing here may reach PIT admissibility, cohort
membership, capture timing, or any research feature: a station-day is admissible
because of its evidence, never because of the host's charge state.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

#: Below this, macOS is close enough to emergency hibernate that an upcoming
#: decision instant is genuinely at risk. Chosen from the observed failure: the
#: host slept at 1%, and the descent from 20% to 1% took under two hours of the
#: collector's own load, which is shorter than the gap between many decision
#: instants. It is a warning line, not a guarantee.
BATTERY_AT_RISK_PERCENT = 20

_SOURCE = re.compile(r"Now drawing from '([^']+)'")
#: " -InternalBattery-0 (id=...)\t52%; charging; 2:00 remaining present: true"
_BATTERY = re.compile(r"(\d{1,3})%;\s*([a-z ]+?);", re.I)


class HostPower(str, Enum):
    OK = "HOST_POWER_OK"
    AT_RISK = "HOST_POWER_AT_RISK"
    UNKNOWN = "HOST_POWER_UNKNOWN"


@dataclass(frozen=True)
class PowerState:
    """What the host's power looked like at one instant."""

    source: str
    battery_percent: int | None
    charging: bool | None
    status: HostPower
    observed_at: datetime
    raw: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "power_source": self.source,
            "battery_percent": self.battery_percent,
            "charging": self.charging,
            "host_power_status": self.status.value,
            "observed_at": self.observed_at.isoformat(),
        }


def parse_power(text: str, *, now: datetime | None = None) -> PowerState:
    """Read `pmset -g batt` output.

    Fails to UNKNOWN rather than to OK. An unreadable power state is exactly the
    situation where a confident "fine" is most misleading, and the failure this
    module was written for produced no software symptom at all.
    """
    observed = now or datetime.now(timezone.utc)
    if not text or not text.strip():
        return PowerState("unknown", None, None, HostPower.UNKNOWN, observed)

    source_match = _SOURCE.search(text)
    source = source_match.group(1) if source_match else "unknown"

    battery_match = _BATTERY.search(text)
    percent: int | None = None
    charging: bool | None = None
    if battery_match:
        try:
            percent = int(battery_match.group(1))
        except ValueError:
            percent = None
        charging = "charging" in battery_match.group(2).lower()

    if source_match is None:
        status = HostPower.UNKNOWN
    elif "AC" in source:
        # Plugged in. Still worth recording the charge: an AC host on a failing
        # adapter drains, and that reads as AC right up until it sleeps.
        status = HostPower.OK
    elif percent is None:
        status = HostPower.UNKNOWN
    elif percent <= BATTERY_AT_RISK_PERCENT:
        status = HostPower.AT_RISK
    else:
        status = HostPower.OK

    return PowerState(source, percent, charging, status, observed, text.strip()[:200])


def read_power(*, now: datetime | None = None) -> PowerState:
    """Current host power, or UNKNOWN when it cannot be read.

    Never raises. A telemetry probe that can take down the health check it
    reports on is worse than no probe.
    """
    try:
        result = subprocess.run(
            ["pmset", "-g", "batt"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        return parse_power(result.stdout, now=now)
    except (OSError, subprocess.SubprocessError):
        return parse_power("", now=now)
