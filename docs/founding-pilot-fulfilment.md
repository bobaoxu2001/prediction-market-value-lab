# Founding Pilot — manual fulfilment runbook

> **SUSPENDED — the pilot was withdrawn on 7 August 2026 and never sold.**
>
> See [ADR 003](adr-003-withdraw-the-pilot.md). In short: `pmvl retrodict` scored
> the independent estimate against the venues' own prices on settled markets and
> it came out marginally *worse*, so the premise the digest was sold on could not
> be demonstrated. The member ledger is still the empty template and no report was
> ever delivered.
>
> **This document is kept deliberately, not left behind by accident.** The
> editorial discipline in it — a job produces a draft, a person approves it, a
> blocked report is still sent, the freshness gates are never relaxed to make a
> Snapshot pass — is the right shape for any future paid delivery, and it would be
> lost if it had to be reconstructed from memory later. Nothing below is currently
> in operation.

**This process is manual. Nothing here is automated, and nothing should be
described to a member as automated.** A scheduled job produces a candidate
report; a person decides whether it is sent. For the first five members that is the
correct trade — it is slower, and it is the only way a human sees every report
before somebody who paid does.

The rule that governs the whole document:

> **No report is ever sent because a scheduled job ran.** A job produces a draft.
> A person approves it. Those are two separate steps and they must stay separate.

---

## The offer

| | |
| --- | --- |
| Price | USD 49, one time, not a subscription |
| Duration | 30 delivery days from first delivery |
| Cap | 5 members (first cohort) |
| Delivery | Email, by hand |
| Account required | None |
| Automatic trading | None. PMVL places no orders. |
| Personalised advice | None. Every member receives an identical report. |

---

## Freshness SLA

Publication of **current actionable research** requires all of:

| Check | Limit |
| --- | --- |
| Snapshot age (from data cutoff) | ≤ 4 hours |
| Freshest quote in the Snapshot | ≤ 2 hours |
| Order book behind any actionable candidate | ≤ 30 minutes |
| Pipeline jobs successful | all 9 |
| Compressed artefact hash | matches manifest |
| Uncompressed SQLite hash | matches manifest |
| SQLite integrity | `PRAGMA quick_check` = ok |
| Validation / release status | `passed` / `published` |
| Required economics fields per candidate | fee rate, ask ladder, resolution time, settlement rule |

Any failure ⇒ `publication_allowed=false`, `actionable_candidates=0`, and the CLI
exits **3**. That is a successful safety outcome, not an error.

**Do not relax these to make a Snapshot pass.** If the data is too old, the
answer is a fresher Snapshot, not a looser bar.

---

## Daily run

### 1. Produce a fresh Snapshot

Publication stays gated and manually approved — see the existing pipeline
documentation. This runbook does not change how Snapshots are published.

### 2. Identify the exact artefact

```bash
python - <<'PY'
import json, pathlib
m = json.loads(pathlib.Path("data/pmvl-snapshot.manifest.json").read_text())
for k in ("snapshot_id", "code_commit_sha", "model_version",
          "source_data_cutoff", "freshest_quote_observed_at",
          "validation_status", "release_status"):
    print(f"{k:32} {m.get(k)}")
PY
```

Record the `snapshot_id` in the delivery log. Every report prints it, so a member
query months later resolves to an exact artefact.

### 3. Verify manifest, hashes and integrity

```bash
python scripts/validate_snapshot.py
```

The digest gate re-checks the compressed hash, the uncompressed hash and SQLite
integrity independently. Both must pass. If they disagree, stop and investigate —
do not send.

### 4. Generate the draft

```bash
python scripts/generate_pilot_digest.py --out out/$(date -u +%F)
echo "exit: $?"
```

| Exit | Meaning | Action |
| --- | --- | --- |
| 0 | A report was produced | Continue to editorial review |
| 3 | **Blocked** — data failed the SLA | Continue to editorial review; a blocked report is still sent |

Read the printed `publication_allowed` and `actionable_candidates`. If
`publication_allowed=false`, `actionable_candidates` must be `0`. If it is ever
non-zero while blocked, **stop and treat it as a P0** — that is the one failure
this product cannot ship.

### 5. Human editorial review

Read the Markdown output end to end. Confirm, for every candidate:

- [ ] The title matches the market on the venue.
- [ ] The executable ask is a VWAP at size, not a top-of-book teaser.
- [ ] The independent probability is present, or its absence is stated.
- [ ] The decision-adjusted probability is the figure the edge rests on.
- [ ] Net edge is positive **after** the full cost stack.
- [ ] Liquidity is real depth, not a single resting lot.
- [ ] The rules-risk section says something specific to this contract.
- [ ] The invalidation conditions are checkable by the reader.
- [ ] The resolution date is right.
- [ ] Nothing reads as instruction or advice.

And for the cost section, which is present on every unblocked report:

- [ ] The order size the figures are priced at is stated. Fee rounding makes the
      premium size-dependent, so a premium without its size is not usable.
- [ ] Categories priced mostly from venue summaries are labelled as a **floor**.
      Missing depth understates the premium; it must never read as precision.
- [ ] The break-even reading is present — cost per contract *is* the probability
      needed to break even, and that is the reason the figure matters.
- [ ] The "widest gap" list is not eight near-identical strikes off one board.
      It is capped at two per category; if it still reads as one contract
      repeated, say so rather than sending it.
- [ ] No cost figure is phrased as an opportunity. This section measures what a
      trade costs; it does not say a trade is worth making.

And for every report:

- [ ] No language promising or implying a return.
- [ ] No personalised phrasing ("you should", "your position").
- [ ] Snapshot ID, cutoff and freshest quote are present.
- [ ] The disclaimer block is present in all three formats.
- [ ] If historical, the historical warning is present in all three formats.

**If any box is unchecked, the report is not sent.** Fix the defect or send a
blocked report explaining the delay.

### 6. Manual approval

Record in the delivery log, before sending:

```
date, snapshot_id, publication_allowed, actionable_candidates,
watchlist_count, reviewer, approved (y/n), notes
```

The reviewer's name is recorded because approval is a human act with a human
attached to it.

### 7. Deliver

Send the HTML body with the plain-text part as the alternative. Use **Bcc** for
the member list — a pilot member must never see another member's address.

Subject is the generated one, e.g.
`PMVL Pilot — no actionable opportunity, 31 Jul 2026`.

### 8. Log the delivery

Append to `delivery-log.csv` (kept outside this repository, since it holds member
addresses):

```
date, snapshot_id, subject, recipients_count, sent_at_utc, sender, exit_code
```

---

## Weekly run

```bash
python scripts/generate_pilot_digest.py --weekly --out out/$(date -u +%F)
```

Same editorial review. Additionally confirm:

- [ ] Independent-prior estimates and market-derived estimates are reported
      **separately**. Never merge them: a market-derived estimate scored against
      the market is an echo, and averaging the two flatters the model.
- [ ] Small samples are disclosed as such.
- [ ] A week with no published recommendations says so plainly rather than
      padding the scorecard.

---

## Failure handling

### Stale run

The gate blocks. Send the blocked report — it names which input was stale and by
how much. Then:

1. Log the blocked day in the delivery log with `exit_code=3`.
2. **Extend the member's 30 days by one day per blocked day.** Days a member paid
   for and did not receive research on are owed back.
3. If blocked three days running, email members directly with a plain explanation
   and an offer of a refund.

### Failed pipeline run

No Snapshot, or validation failed. Do **not** fall back to an older Snapshot to
have something to send — an older Snapshot is exactly what the freshness gate
exists to reject. Send a short manual note stating the pipeline did not produce a
validated Snapshot, and extend as above.

### Missed delivery

A day where a report existed and was not sent is an operator failure, not a data
one. Extend by one day and record the cause in the delivery log.

### Refunds

Refund on request where the pilot did not deliver reports for the days paid for.
Judging the research unhelpful is a legitimate reason to ask, and members are
told to read the free samples first precisely so that judgement can be made
before paying.

### Corrections

If a sent report contained an error:

1. Do not silently regenerate. The original was received and cannot be unsent.
2. Send a correction with subject `CORRECTION — <original subject>`, stating what
   was wrong, what the corrected figure is, and how the error arose.
3. Append to `correction-log.csv`:
   `original_date, snapshot_id, field, sent_value, corrected_value, cause, corrected_at`
4. If the error caused a candidate to appear actionable when it was not, say so
   in the first sentence.

---

## Member tracking

Kept outside this repository (it holds personal data). One row per member:

```
member_id, email, paid_at, payment_reference, start_date, end_date,
days_extended, extension_reason, status, refunded_at
```

- `end_date` = `start_date` + 30 days + `days_extended`.
- Stop at **5 active members**. The cap is what makes manual review possible; a
  sixth member makes the process worse for the first five. The Stripe Payment
  Link is capped at five completed payments, so this is enforced at checkout too.

---

## What must never happen

- A report sent automatically because a scheduled job ran.
- Current actionable research generated from a Snapshot that failed the SLA.
- A historical sample sent without its historical warning.
- Personalised advice, sizing guidance, or anything phrased as an instruction.
- Any language promising, implying or projecting a return.
- A member address exposed to another member.
- Freshness thresholds relaxed so a stale Snapshot passes.
