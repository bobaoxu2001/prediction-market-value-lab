"""The provenance a scheduled run has to leave behind to be auditable.

`JobRun` recorded a name, a status, two timestamps and a free-form details blob.
That is enough to draw a green tick and not enough to answer any question worth
asking after the fact:

* Which code produced this data?
* What input cutoff did it see, so a backtest can avoid look-ahead?
* Did it write 1,400 markets or 3, and was the difference a quiet provider failure?
* Is this the same run that already executed, retried, or a genuinely new one?

Every field here exists because its absence made one of those unanswerable. The
idempotency key is the load-bearing one: without it a retried ingest is
indistinguishable from a second ingest, and the safe thing to do on failure -
retry - becomes the thing that duplicates data.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .timeutil import iso


def code_version() -> str:
    """The commit this process was built from, or "unknown" when unavailable.

    Never a placeholder that looks like a real SHA. A row claiming to come from
    commit 0000000 is worse than one admitting it does not know.
    """
    # PMVL_COMMIT_SHA is an explicit provenance override. Reusable Actions
    # workflows retain the caller's GITHUB_SHA even when they check out a
    # different exact publication commit, so the ambient CI variable must not
    # overwrite the ref the caller deliberately supplied.
    for var in ("PMVL_COMMIT_SHA", "VERCEL_GIT_COMMIT_SHA", "GITHUB_SHA"):
        value = os.environ.get(var)
        if value:
            return value[:12]

    # Outside CI the commit is still knowable, and asking git is not a guess. A
    # local pipeline run that stamped its artefact "unknown" would be rejected by
    # the provenance gate for a fact the machine could have looked up.
    try:
        import subprocess

        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()[:12]
    except Exception:  # noqa: BLE001 - not a git checkout, or git is absent
        pass
    return "unknown"


def idempotency_key(job_name: str, *, cutoff: datetime | None, params: dict[str, Any] | None = None) -> str:
    """A stable identity for "this job, over this input, with these parameters".

    Two runs sharing a key are the same logical work: the second may be skipped or
    may safely overwrite the first. Deliberately excludes wall-clock start time,
    because a retry three minutes later is the *same* work and must hash the same.
    """
    material = "|".join(
        [
            job_name,
            iso(cutoff) or "no-cutoff",
            ";".join(f"{k}={params[k]}" for k in sorted(params or {})),
        ]
    )
    return hashlib.sha256(material.encode()).hexdigest()[:32]


@dataclass
class ProviderStat:
    """Per-source outcome, so one venue failing does not vanish into a total."""

    provider: str
    requests: int = 0
    failures: int = 0
    rate_limited: int = 0
    records: int = 0
    error: str = ""

    @property
    def healthy(self) -> bool:
        return self.failures == 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "requests": self.requests,
            "failures": self.failures,
            "rate_limited": self.rate_limited,
            "records": self.records,
            "error": self.error[:300],
            "healthy": self.healthy,
        }


@dataclass
class RunRecord:
    """Everything one scheduled run must be able to say about itself."""

    job_name: str
    run_id: str
    idempotency_key: str
    code_commit_sha: str = field(default_factory=code_version)
    scheduled_at: datetime | None = None
    #: The newest input this run was allowed to see. A backtest that ignores this
    #: and reads current data is measuring a model it could not have run.
    input_data_cutoff: datetime | None = None
    output_range_start: datetime | None = None
    output_range_end: datetime | None = None
    records_read: int = 0
    records_written: int = 0
    retry_count: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    upstream_dependencies: list[str] = field(default_factory=list)
    downstream_triggered: list[str] = field(default_factory=list)
    provider_stats: dict[str, ProviderStat] = field(default_factory=dict)

    def provider(self, name: str) -> ProviderStat:
        return self.provider_stats.setdefault(name, ProviderStat(provider=name))

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def fail_provider(self, name: str, error: str) -> None:
        """Record a source failure without aborting the run.

        One venue being down must degrade the result, not delete it. Silently
        converting this into a shorter market list is how "the scan found nothing"
        came to mean two different things.
        """
        stat = self.provider(name)
        stat.failures += 1
        stat.error = str(error)[:300]

    @property
    def failed_providers(self) -> list[str]:
        return sorted(n for n, s in self.provider_stats.items() if not s.healthy)

    @property
    def is_partial(self) -> bool:
        """Some sources failed, some succeeded, and output was still produced."""
        if not self.provider_stats:
            return False
        failed = self.failed_providers
        return bool(failed) and len(failed) < len(self.provider_stats)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "idempotency_key": self.idempotency_key,
            "code_commit_sha": self.code_commit_sha,
            "scheduled_at": iso(self.scheduled_at),
            "input_data_cutoff": iso(self.input_data_cutoff),
            "output_range_start": iso(self.output_range_start),
            "output_range_end": iso(self.output_range_end),
            "records_read": self.records_read,
            "records_written": self.records_written,
            "retry_count": self.retry_count,
            "warnings": self.warnings[:50],
            "errors": self.errors[:50],
            "upstream_dependencies": self.upstream_dependencies,
            "downstream_triggered": self.downstream_triggered,
            "provider_stats": {n: s.as_dict() for n, s in self.provider_stats.items()},
            "failed_providers": self.failed_providers,
        }
