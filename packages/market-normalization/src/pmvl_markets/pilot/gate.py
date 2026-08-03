"""The gate that decides whether a Snapshot may back *current* paid research.

This is the safety boundary of the Founding Pilot, and it is deliberately
stricter than the public site's.

The public site serves a frozen Snapshot behind a banner that says so; a reader
can see the age and discount it. A paid daily digest cannot rely on that, because
its whole proposition is "here is what is actionable **today**". A two-day-old
ask presented as a candidate is not a stale page, it is a false claim someone
paid for. So the pilot applies its own SLA on top of the shared freshness
policies and takes whichever is tighter:

    Snapshot age                <=  4 hours     (report level - blocks publication)
    freshest market quote       <=  2 hours     (report level - blocks publication)
    quote behind a candidate    <= 30 minutes   (candidate level - demotes to watchlist)

**The two levels are separate, and only the first can refuse a report.**

A stale book invalidates the candidate resting on it. It does not invalidate the
report saying so. Conflating the two made the pilot publishable for roughly
thirty minutes after each publication: the shared `TOP_OF_BOOK` policy hard-stales
at 30 minutes, this module measured it against the Snapshot's freshest quote, and
a refusal there withheld the entire digest - including a zero-actionable no-trade
report, which carries no recommendation for a stale book to invalidate. The
subscriber's honest "nothing cleared the bar today" was replaced by silence.

It also quietly overrode the published SLA. A 2-hour quote limit that the sales
page and the runbook both quote does not mean 2 hours if a 30-minute threshold
refuses the report first.

So per-instrument freshness is now *recorded* by this module and *enforced* by
`digest.py`, per market, against each candidate's own observation time rather
than the newest quote in the file - which is strictly tighter than what happened
here. Markets failing it are demoted to the watchlist carrying the reason.

Those numbers are not the shared `pmvl_shared.freshness` thresholds and must not
be pushed into them: relaxing the shared policy to make a paid product pass would
loosen the public site as a side effect, and tightening it would start rejecting
markets the site is right to show. Two products, two bars, one direction of
travel - the paid one is never looser.

Everything else is fail-closed and independent:

- **both** hashes verified, compressed and uncompressed, because the gzip is what
  ships and the SQLite bytes are what is read;
- SQLite integrity checked, because a file can match its hash and still be a
  corrupt database;
- all nine pipeline jobs successful, not just the ones this report reads - a
  partial pipeline is a Snapshot nobody should be selling research from;
- validation `passed` and release `published`, because an artefact held back was
  held back for a reason.

A refusal is a successful outcome, not an exception. It carries its reasons, the
renderers show them to the subscriber, and the CLI exits non-zero so a scheduler
can tell the difference without suppressing the send.
"""

from __future__ import annotations

import gzip
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from pmvl_shared.freshness import DataType, assess
from pmvl_shared.manifest import (
    ReleaseStatus,
    SnapshotManifest,
    ValidationStatus,
    sha256_of,
)
from pmvl_shared.timeutil import ensure_utc, utcnow

#: Every job the pipeline runs. The digest reads the output of four of them, but
#: a Snapshot produced while any job failed is not one to sell research from:
#: a failed `settle` corrupts the weekly review, a failed `prune` means the
#: Snapshot may carry rows the scan should never have seen.
REQUIRED_JOBS = (
    "arbitrage",
    "backtest",
    "ingest",
    "orderbooks",
    "prune",
    "rank",
    "score",
    "settle",
    "snapshot",
)

#: Freshness classes consulted from the shared policy, in addition to the SLA
#: below. Whichever is stricter wins.
REQUIRED_FRESHNESS = (
    DataType.TOP_OF_BOOK,
    DataType.FULL_ORDERBOOK,
    DataType.MODEL_PREDICTION,
)


@dataclass(frozen=True)
class PilotSLA:
    """The published service level for paid current research.

    Stated as a dataclass rather than constants so a test can tighten it without
    monkeypatching, and so the numbers appear in one place that the sales page
    and the runbook can both quote.
    """

    #: Age of the Snapshot itself, measured from its data cutoff.
    snapshot_max_age_seconds: int = 4 * 3600
    #: Age of the newest quote anywhere in the Snapshot.
    quote_max_age_seconds: int = 2 * 3600
    #: Age of the specific order book behind a candidate. Enforced per market in
    #: `digest.py`; a market failing this can still appear on the watchlist, but
    #: it can never be actionable.
    candidate_quote_max_age_seconds: int = 30 * 60

    def as_dict(self) -> dict[str, int]:
        return {
            "snapshot_max_age_seconds": self.snapshot_max_age_seconds,
            "quote_max_age_seconds": self.quote_max_age_seconds,
            "candidate_quote_max_age_seconds": self.candidate_quote_max_age_seconds,
        }


DEFAULT_SLA = PilotSLA()


class GateFailure:
    """Stable reason codes. Rendered to the reader, so they read as sentences too."""

    MANIFEST_MISSING = "manifest_missing"
    MANIFEST_UNREADABLE = "manifest_unreadable"
    ARTEFACT_MISSING = "artefact_missing"
    COMPRESSED_HASH_MISMATCH = "compressed_hash_mismatch"
    UNCOMPRESSED_HASH_MISMATCH = "uncompressed_hash_mismatch"
    SQLITE_INTEGRITY = "sqlite_integrity_failed"
    NOT_VALIDATED = "not_validated"
    NOT_PUBLISHED = "not_published"
    JOB_FAILED = "job_failed"
    JOB_MISSING = "job_missing"
    STALE_SNAPSHOT = "stale_snapshot"
    STALE_QUOTE = "stale_quote"
    NO_REFERENCE_TIME = "no_reference_time"

    #: Retired. This once refused a whole report because a per-instrument policy
    #: said no *recommendation* could rest on the data - which is a candidate
    #: judgement, and is now made per market in `digest.py`. Kept as a name so
    #: that a reader meeting it in an archived report can find this explanation,
    #: and deliberately never emitted: nothing may reintroduce a report-level
    #: refusal on candidate-level grounds without changing this comment.
    STALE_INPUT = "stale_input"


@dataclass(frozen=True)
class FreshnessFinding:
    """How one class of input is ageing, measured against the shared policy.

    Advisory at the report level. ``blocks`` means "a RECOMMENDATION cannot rest
    on this input", which is a candidate-level judgement: it is why a market gets
    demoted to the watchlist, not a reason to refuse the whole report. A
    zero-actionable report contains no recommendation for a stale book to
    invalidate, and refusing to publish one because the books aged is how the
    pilot ended up usable for only half an hour after each publication.
    """

    data_type: str
    state: str
    age_seconds: float | None
    blocks: bool

    @property
    def age_hours(self) -> float | None:
        return None if self.age_seconds is None else self.age_seconds / 3600.0


@dataclass
class GateResult:
    """Whether current actionable research may be published, and why not."""

    #: The single question this object exists to answer: may this back research
    #: presented as CURRENT and actionable? Always false in historical mode -
    #: see `checks_passed` for whether the artefact itself was sound.
    publication_allowed: bool
    as_of: datetime
    sla: PilotSLA = field(default_factory=PilotSLA)
    #: True when the caller asked for a historical sample. Publication is still
    #: refused for *current* research; the renderers stamp the warning.
    historical_mode: bool = False
    snapshot_id: str = ""
    code_commit_sha: str = ""
    model_version: str = ""
    source_data_cutoff: datetime | None = None
    freshest_quote_observed_at: datetime | None = None
    snapshot_age_seconds: float | None = None
    quote_age_seconds: float | None = None
    failures: list[tuple[str, str]] = field(default_factory=list)
    freshness: list[FreshnessFinding] = field(default_factory=list)
    job_statuses: dict[str, str] = field(default_factory=dict)
    integrity_ok: bool = False
    hashes_verified: bool = False
    #: Whether every check passed at ``as_of``. Identical to
    #: ``publication_allowed`` outside historical mode; inside it, this is the
    #: honest "the artefact was sound at its cutoff" while publication stays
    #: refused. Kept separate so neither answer has to be inferred from the other.
    checks_passed: bool = False

    # Kept so existing call sites and tests read naturally.
    @property
    def ok(self) -> bool:
        return self.publication_allowed

    @property
    def reasons(self) -> list[str]:
        return [sentence for _code, sentence in self.failures]

    @property
    def blocked_reason(self) -> str:
        """One sentence naming why publication was refused, for logs and JSON."""
        if self.publication_allowed:
            return ""
        return "; ".join(self.reasons)

    @property
    def codes(self) -> list[str]:
        return [code for code, _sentence in self.failures]

    @property
    def actionable_inputs_blocked(self) -> list[str]:
        """Input classes too old to support a recommendation right now.

        Reported, never a publication blocker. A non-empty list means the report
        should be expected to carry few or no actionable candidates - which is a
        finding worth printing, not a reason to withhold the finding.
        """
        return [f.data_type for f in self.freshness if f.blocks]

    def as_dict(self) -> dict[str, object]:
        return {
            "publication_allowed": self.publication_allowed,
            "checks_passed": self.checks_passed,
            "historical_mode": self.historical_mode,
            "blocked_reason": self.blocked_reason,
            "as_of": self.as_of.isoformat(),
            "sla": self.sla.as_dict(),
            "snapshot_id": self.snapshot_id,
            "code_commit_sha": self.code_commit_sha,
            "model_version": self.model_version,
            "source_data_cutoff": (
                self.source_data_cutoff.isoformat() if self.source_data_cutoff else None
            ),
            "freshest_quote_observed_at": (
                self.freshest_quote_observed_at.isoformat()
                if self.freshest_quote_observed_at
                else None
            ),
            "snapshot_age_seconds": self.snapshot_age_seconds,
            "quote_age_seconds": self.quote_age_seconds,
            "integrity_ok": self.integrity_ok,
            "hashes_verified": self.hashes_verified,
            "failures": [{"code": c, "detail": d} for c, d in self.failures],
            # Named so a reader cannot mistake it for a publication blocker: these
            # are the inputs too old to support an ACTIONABLE candidate.
            "actionable_inputs_blocked": self.actionable_inputs_blocked,
            "freshness": [
                {
                    "data_type": f.data_type,
                    "state": f.state,
                    "age_seconds": f.age_seconds,
                    # Blocks a recommendation resting on this input, not the report.
                    "blocks_actionable": f.blocks,
                    # Retained under the old key so existing consumers keep working.
                    "blocks": f.blocks,
                }
                for f in self.freshness
            ],
            "job_statuses": self.job_statuses,
        }


def _parse_time(value: object) -> datetime | None:
    if value in (None, "", "None"):
        return None
    if isinstance(value, datetime):
        return ensure_utc(value)
    try:
        return ensure_utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except ValueError:
        return None


def _load_manifest(manifest_path: Path) -> tuple[dict | None, str]:
    if not manifest_path.exists():
        return None, f"No manifest at {manifest_path.name}."
    try:
        payload = SnapshotManifest.load(manifest_path)
    except Exception as exc:  # noqa: BLE001 - any parse failure is the same answer
        return None, f"Manifest could not be read ({type(exc).__name__})."
    if not isinstance(payload, dict):
        return None, "Manifest is not an object."
    return payload, ""


def _materialise_db(manifest: dict, manifest_path: Path) -> Path | None:
    """The raw SQLite bytes on disk, decompressing first when published as gzip.

    Deliberately does NOT go through `resolve_snapshot_path`: that helper verifies
    as it resolves and raises on mismatch, which would turn "the hash is wrong"
    into an exception before this module could report *which* hash was wrong.
    Verification is this function's caller's job.
    """
    data_dir = manifest_path.parent
    if manifest.get("artifact_encoding") == "gzip" and manifest.get("compressed_path"):
        compressed = data_dir.parent / str(manifest["compressed_path"])
        if not compressed.exists():
            return None
        target = Path(tempfile.gettempdir()) / "pmvl-pilot-gate" / (
            f"{manifest.get('uncompressed_sha256') or manifest.get('sha256') or 'snapshot'}.db"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            with gzip.open(compressed, "rb") as source, target.open("wb") as sink:
                shutil.copyfileobj(source, sink)
        return target
    raw = data_dir / "pmvl-snapshot.db"
    return raw if raw.exists() else None


def _verify_hashes(
    manifest: dict, manifest_path: Path
) -> tuple[list[tuple[str, str]], Path | None]:
    """Verify the compressed transport bytes AND the SQLite bytes.

    Both, because they answer different questions: the compressed hash proves the
    committed artefact is intact, and the uncompressed hash proves the database
    that gets read is the one that was validated. A truncated decompression can
    satisfy the first and fail the second.
    """
    failures: list[tuple[str, str]] = []
    root = manifest_path.parent.parent

    if manifest.get("artifact_encoding") == "gzip" and manifest.get("compressed_path"):
        compressed = root / str(manifest["compressed_path"])
        declared = manifest.get("compressed_sha256")
        if not compressed.exists():
            failures.append(
                (
                    GateFailure.ARTEFACT_MISSING,
                    "The compressed Snapshot named by the manifest is missing.",
                )
            )
        elif not declared:
            failures.append(
                (
                    GateFailure.COMPRESSED_HASH_MISMATCH,
                    "The manifest declares no checksum for the compressed Snapshot.",
                )
            )
        elif sha256_of(compressed) != declared:
            failures.append(
                (
                    GateFailure.COMPRESSED_HASH_MISMATCH,
                    "The compressed Snapshot does not match the checksum in its manifest.",
                )
            )

    db_path = _materialise_db(manifest, manifest_path)
    if db_path is None:
        failures.append(
            (GateFailure.ARTEFACT_MISSING, "The Snapshot database could not be located.")
        )
        return failures, None

    expected = manifest.get("uncompressed_sha256") or manifest.get("sha256")
    if not expected:
        failures.append(
            (
                GateFailure.UNCOMPRESSED_HASH_MISMATCH,
                "The manifest declares no checksum for the SQLite database.",
            )
        )
    elif sha256_of(db_path) != expected:
        failures.append(
            (
                GateFailure.UNCOMPRESSED_HASH_MISMATCH,
                "The SQLite database does not match the checksum in its manifest.",
            )
        )

    return failures, db_path


def _check_integrity(db_path: Path) -> tuple[list[tuple[str, str]], bool]:
    """Ask SQLite whether the database is sound.

    A file can match its checksum and still be unusable: the hash proves nobody
    changed the bytes, not that the bytes were a valid database when written.
    """
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            result = conn.execute("PRAGMA quick_check").fetchone()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        return (
            [
                (
                    GateFailure.SQLITE_INTEGRITY,
                    f"The Snapshot database could not be opened ({type(exc).__name__}).",
                )
            ],
            False,
        )

    if not result or str(result[0]).lower() != "ok":
        detail = str(result[0]) if result else "no result"
        return (
            [
                (
                    GateFailure.SQLITE_INTEGRITY,
                    f"SQLite reported the Snapshot database as damaged: {detail}.",
                )
            ],
            False,
        )
    return [], True


def evaluate(
    manifest_path: Path,
    *,
    as_of: datetime | None = None,
    sla: PilotSLA | None = None,
    historical_mode: bool = False,
) -> GateResult:
    """Decide whether this Snapshot may back current paid research at ``as_of``.

    ``historical_mode`` does not relax a single check - it *removes the
    permission entirely*. A retrospective is generated by evaluating at the
    Snapshot's own cutoff, which is precisely the manoeuvre that would make a
    three-day-old artefact look fresh, so the one thing that must not follow from
    it is permission to publish current research. In historical mode
    ``publication_allowed`` is therefore false unconditionally, whatever the
    checks say, and ``checks_passed`` carries the diagnostic instead.
    """
    as_of = ensure_utc(as_of) or utcnow()
    sla = sla or DEFAULT_SLA

    manifest, error = _load_manifest(manifest_path)
    if manifest is None:
        code = (
            GateFailure.MANIFEST_MISSING
            if not manifest_path.exists()
            else GateFailure.MANIFEST_UNREADABLE
        )
        return GateResult(
            publication_allowed=False,
            as_of=as_of,
            sla=sla,
            historical_mode=historical_mode,
            failures=[(code, error)],
        )

    failures: list[tuple[str, str]] = []

    hash_failures, db_path = _verify_hashes(manifest, manifest_path)
    failures.extend(hash_failures)
    hashes_ok = not hash_failures

    integrity_ok = False
    if db_path is not None:
        # Integrity is checked even when a hash mismatched: knowing whether the
        # file is also structurally damaged is useful, and cheap.
        integrity_failures, integrity_ok = _check_integrity(db_path)
        failures.extend(integrity_failures)

    validation_status = manifest.get("validation_status")
    if validation_status != ValidationStatus.PASSED:
        failures.append(
            (
                GateFailure.NOT_VALIDATED,
                f"The Snapshot's validation status is '{validation_status}', not 'passed'.",
            )
        )
    release_status = manifest.get("release_status")
    if release_status != ReleaseStatus.PUBLISHED:
        failures.append(
            (
                GateFailure.NOT_PUBLISHED,
                f"The Snapshot's release status is '{release_status}'. "
                "A held Snapshot was held for a reason.",
            )
        )

    job_statuses = manifest.get("job_statuses") or {}
    if not isinstance(job_statuses, dict):
        job_statuses = {}
    for job in REQUIRED_JOBS:
        status = job_statuses.get(job)
        if status is None:
            failures.append(
                (GateFailure.JOB_MISSING, f"The pipeline recorded no run of the '{job}' job.")
            )
        elif status != "success":
            failures.append(
                (
                    GateFailure.JOB_FAILED,
                    f"The '{job}' pipeline job finished as '{status}', not 'success'.",
                )
            )

    quote_at = _parse_time(manifest.get("freshest_quote_observed_at"))
    cutoff = _parse_time(manifest.get("source_data_cutoff"))

    def age_of(moment: datetime | None) -> float | None:
        if moment is None:
            return None
        seconds = (as_of - moment).total_seconds()
        # A negative age means the Snapshot claims to be from the future. Treat it
        # as unusable rather than maximally fresh: a clock disagreement is not a
        # reason to trust data more.
        return None if seconds < 0 else seconds

    snapshot_age = age_of(cutoff)
    quote_age = age_of(quote_at)

    if snapshot_age is None:
        failures.append(
            (
                GateFailure.NO_REFERENCE_TIME,
                "The Snapshot declares no usable data cutoff, so its age cannot be established.",
            )
        )
    elif snapshot_age > sla.snapshot_max_age_seconds:
        failures.append(
            (
                GateFailure.STALE_SNAPSHOT,
                f"Stale Snapshot: it was cut {snapshot_age / 3600:.1f} hours ago, past the "
                f"{sla.snapshot_max_age_seconds / 3600:.0f}-hour limit for current research.",
            )
        )

    if quote_age is None:
        failures.append(
            (
                GateFailure.NO_REFERENCE_TIME,
                "The Snapshot declares no usable quote observation time.",
            )
        )
    elif quote_age > sla.quote_max_age_seconds:
        failures.append(
            (
                GateFailure.STALE_QUOTE,
                f"Stale quote: the freshest quote in the Snapshot is "
                f"{quote_age / 3600:.1f} hours old, past the "
                f"{sla.quote_max_age_seconds / 3600:.0f}-hour limit for current research.",
            )
        )

    # The shared per-instrument policies are consulted too, but they answer a
    # DIFFERENT question and are recorded rather than added to `failures`.
    #
    # `blocks_eligibility` means "no RECOMMENDATION can rest on this input". That
    # is a statement about a candidate, not about a report. Promoting it to a
    # report-level failure is what made the pilot publishable for only about
    # thirty minutes after each publication: TOP_OF_BOOK hard-stales at 30
    # minutes and FULL_ORDERBOOK at an hour, both measured here against the
    # Snapshot's single freshest quote, so a report whose Snapshot and freshest
    # quote were comfortably inside the 4-hour and 2-hour SLA was refused
    # anyway - including a zero-actionable no-trade report, which contains no
    # recommendation for a stale book to invalidate.
    #
    # It also silently overrode the published SLA: a 2-hour quote limit the
    # sales page and runbook both quote cannot mean 2 hours if a 30-minute
    # threshold refuses the report first.
    #
    # The 30-minute rule is not being relaxed - it is enforced per market in
    # `digest.py`, where the book behind a specific candidate is compared with
    # that candidate's own timestamp rather than with the Snapshot's newest one.
    # A market failing it is demoted to the watchlist carrying the reason. That
    # is strictly tighter than what this loop did, because it uses each market's
    # real observation time instead of the freshest quote in the file.
    #
    # Nothing is lost by not failing here: an absent timestamp is already a
    # report-level NO_REFERENCE_TIME failure above.
    reference = {
        DataType.TOP_OF_BOOK: quote_at,
        DataType.FULL_ORDERBOOK: quote_at,
        DataType.MODEL_PREDICTION: cutoff or quote_at,
    }
    findings: list[FreshnessFinding] = []
    for data_type in REQUIRED_FRESHNESS:
        age = age_of(reference.get(data_type))
        finding = assess(data_type, age)
        findings.append(
            FreshnessFinding(
                data_type=data_type.value,
                state=finding.state.value,
                age_seconds=age,
                blocks=finding.blocks_eligibility,
            )
        )

    return GateResult(
        # `not failures` is whether the artefact is sound; publication needs
        # that AND a caller who is asking about the present.
        publication_allowed=not failures and not historical_mode,
        checks_passed=not failures,
        as_of=as_of,
        sla=sla,
        historical_mode=historical_mode,
        snapshot_id=str(manifest.get("snapshot_id") or ""),
        code_commit_sha=str(manifest.get("code_commit_sha") or ""),
        model_version=str(manifest.get("model_version") or ""),
        source_data_cutoff=cutoff,
        freshest_quote_observed_at=quote_at,
        snapshot_age_seconds=snapshot_age,
        quote_age_seconds=quote_age,
        failures=failures,
        freshness=findings,
        job_statuses=dict(job_statuses),
        integrity_ok=integrity_ok,
        hashes_verified=hashes_ok,
    )
