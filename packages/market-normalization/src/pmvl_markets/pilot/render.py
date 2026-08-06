"""Render a report as Markdown, HTML email, or plain text.

One report model, three renderings, and the rule that governs all of them: the
three outputs must state the same facts. A subscriber whose mail client strips
HTML has to be able to reach the same decision as one whose client does not, so
the plain-text version is not a degraded courtesy copy - it carries every number
and every caveat the HTML does.

The compliance block is appended by `_disclaimer` in each format rather than
being written into any template. That is deliberate: it makes it impossible to
add a new report type that forgets it, and it means the wording is changed in one
place when it needs to change.

HTML email is table-based with inline styles because that is what mail clients
render. There is no stylesheet, no web font, no external image and no tracking
pixel - partly because they do not work, and partly because a research product
that quietly reports whether you opened it is not the product being sold.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from html import escape

from .digest import Candidate, DigestReport, WatchlistEntry
from .outcomes import OutcomeReview

Report = DigestReport | OutcomeReview

#: Stated in every report, in every format. Not a footer nobody reads: it is the
#: difference between research and advice, and it is why this product is legal to
#: sell without a licence.
#: Stamped on every historical sample, in every format, above the fold. The
#: sentence is fixed and asserted by tests: a retrospective that a reader mistakes
#: for today's research is the single worst failure this product can have.
HISTORICAL_WARNING = (
    "Historical sample — generated from the validated Snapshot dated {date}. "
    "Not current market research."
)

#: Said alongside it, because the first sentence explains what the document is
#: and this one explains what it is not for.
HISTORICAL_SUBWARNING = (
    "Do not act on any price, probability or edge in this document. Every figure "
    "was true at the Snapshot's cutoff and is now historical. No current research "
    "is being issued here."
)

DISCLAIMER_LINES = (
    "Research and information only. This is not investment, legal, tax or financial "
    "advice, not a solicitation, and not a recommendation to trade.",
    "Nothing here is personalised. It is the same report sent to every pilot member, "
    "written without knowledge of your circumstances, capital or risk tolerance.",
    "No return is promised or implied. Prediction-market contracts can settle worthless "
    "and the entire amount paid for one can be lost.",
    "Every figure comes from a frozen Snapshot and does not update. Prices move; a quote "
    "shown here may no longer exist. Verify on the venue before acting.",
    "PMVL places no orders, holds no funds and has no execution access to any venue. Any "
    "position you take is your own decision.",
)


def _pct(value: Decimal | float | None, digits: int = 1) -> str:
    if value is None:
        return "not available"
    return f"{float(value) * 100:.{digits}f}%"


def _cents(value: Decimal | float | None) -> str:
    if value is None:
        return "—"
    return f"{float(value) * 100:.2f}¢"


def _usd(value: Decimal | float | None) -> str:
    if value is None:
        return "—"
    return f"${float(value):,.2f}"


def _when(value: datetime | None) -> str:
    return "—" if value is None else value.strftime("%Y-%m-%d %H:%M UTC")


def _snapshot_date(report: Report) -> str:
    moment = report.gate.source_data_cutoff or report.gate.freshest_quote_observed_at
    return moment.strftime("%Y-%m-%d") if moment else "unknown"


def _historical_lines(report: Report) -> tuple[str, str] | None:
    """The two warning sentences, or None when this is not a historical sample."""
    if not report.gate.historical_mode:
        return None
    return HISTORICAL_WARNING.format(date=_snapshot_date(report)), HISTORICAL_SUBWARNING


def _subject(report: Report) -> str:
    date = report.generated_at.strftime("%d %b %Y")
    if report.gate.historical_mode:
        return f"PMVL Pilot — HISTORICAL SAMPLE, Snapshot {_snapshot_date(report)}"
    if isinstance(report, OutcomeReview):
        return f"PMVL Pilot — weekly outcome review, {date}"
    if not report.actionable_allowed:
        return f"PMVL Pilot — no research issued, {date}"
    if not report.candidates:
        return f"PMVL Pilot — no actionable opportunity, {date}"
    n = len(report.candidates)
    return f"PMVL Pilot — {n} candidate{'s' if n != 1 else ''}, {date}"


def subject_line(report: Report) -> str:
    """The email subject. Exported because the send script needs it separately."""
    return _subject(report)


def _candidate_freshness_sentence(report: Report) -> str:
    """Why the actionable list may be short, when the report itself is sound.

    Every format prints this, because the distinction it draws is the one a
    subscriber is most likely to get wrong: an input too old to support a
    *candidate* is not a reason the *report* is unreliable. Without the sentence,
    a reader who notices a 2-hour-old order book in the provenance table can only
    conclude that the empty actionable list is a malfunction.
    """
    blocked = report.gate.actionable_inputs_blocked
    if not blocked:
        return ""
    names = ", ".join(name.replace("_", " ") for name in blocked)
    return (
        f"Candidate-level note: {names} data is currently too old to support an "
        "actionable candidate, so any market resting on it appears on the "
        "watchlist rather than in the actionable list. This does not affect "
        "whether this report may be published - the Snapshot and the freshest "
        "quote are both inside their limits, which is what that decision rests on."
    )


# ---------------------------------------------------------------- markdown --


def _md_gate(report: Report) -> list[str]:
    g = report.gate
    out = [
        "## Snapshot provenance",
        "",
        f"- Snapshot: `{g.snapshot_id or 'unknown'}`",
        f"- Pipeline commit: `{g.code_commit_sha or 'unknown'}`",
        f"- Model version: `{g.model_version or 'unknown'}`",
        f"- Data cutoff: {_when(g.source_data_cutoff)}",
        f"- Freshest observed quote: {_when(g.freshest_quote_observed_at)}",
        f"- Report generated for: {_when(g.as_of)}",
        "",
    ]
    if g.freshness:
        out.append("| Input | State | Age | Can support a candidate |")
        out.append("| --- | --- | --- | --- |")
        for f in g.freshness:
            age = "unknown" if f.age_hours is None else f"{f.age_hours:.1f} h"
            supports = "no" if f.blocks else "yes"
            out.append(
                f"| {f.data_type.replace('_', ' ')} | {f.state} | {age} | {supports} |"
            )
        out.append("")
    note = _candidate_freshness_sentence(report)
    if note:
        out += [note, ""]
    return out


def _md_candidate(index: int, c: Candidate) -> list[str]:
    out = [
        f"### {index}. {c.title}",
        "",
        f"**{c.side.upper()}** on {c.platform} · `{c.platform_market_id}` · {c.category}",
        "",
        "| | |",
        "| --- | --- |",
        f"| Executable ask (VWAP at size) | **{_cents(c.executable_ask)}** |",
        f"| All-in cost per contract | **{_cents(c.all_in_cost)}** |",
        f"| Size the ask was measured at | {float(c.executable_size):.0f} contracts |",
        f"| Market-implied probability | {_pct(c.market_implied_probability)} |",
        f"| Independent probability | {_pct(c.independent_probability)} |",
        f"| Decision-adjusted probability | **{_pct(c.decision_adjusted_probability)}** |",
        f"| Model interval | {_pct(c.probability_interval[0])} – {_pct(c.probability_interval[1])} |",
        f"| Model confidence | {_pct(c.model_confidence)} |",
        f"| **Net edge after costs** | **{_cents(c.net_edge_after_costs)} per contract** |",
        f"| Net ROI | {_pct(c.net_roi)} |",
        f"| Resting liquidity on this side | {_usd(c.liquidity_usd)} |",
        f"| Spread | {_cents(c.spread)} |",
        f"| Size cap where marginal EV stays positive | {float(c.position_cap_contracts):.0f} contracts |",
        f"| Resolution date | {_when(c.resolution_date)} |",
        f"| Settlement source | {c.settlement_source or 'not stated'} |",
        "",
        "**Cost stack**",
        "",
    ]
    for name, value in c.cost_components:
        out.append(f"- {name}: {_cents(value)}")
    out.append("")

    out.append("**Rules risk**")
    out.append("")
    out.extend(f"- {r}" for r in c.rules_risk)
    out.append("")

    out.append("**What would invalidate this**")
    out.append("")
    out.extend(f"- {r}" for r in c.invalidation_conditions)
    out.append("")

    if c.risk_flags:
        out.append(f"**Risk flags:** {', '.join(c.risk_flags)}")
        out.append("")
    return out


def _md_disclaimer() -> list[str]:
    return ["---", "", "### Terms of this report", ""] + [
        f"- {line}" for line in DISCLAIMER_LINES
    ] + [""]


def to_markdown(report: Report) -> str:
    if isinstance(report, OutcomeReview):
        return _md_weekly(report)
    return _md_daily(report)


def _md_banner(report: Report) -> list[str]:
    lines = _historical_lines(report)
    if lines is None:
        return []
    return ["> ⚠️ **" + lines[0] + "**", ">", "> " + lines[1], ""]


def _md_cost(cost) -> list[str]:  # noqa: ANN001
    """What it costs to trade. The section that is never empty.

    Every other part of this report is downstream of a probability estimate, and
    the independence rule declines to produce one for most markets - so a report
    built only from candidates says "nothing today" on most days. Execution cost
    needs no estimate, so this is here whether or not anything cleared the bar.
    """
    if cost is None or not cost.has_content:
        return []

    size = int(cost.priced_at_size)
    out = [
        f"## What it costs to trade today — {cost.markets_priced:,} contracts priced",
        "",
        f"Every figure below is the cost of buying **{size} contracts**, above the "
        "price on the venue's screen. It is computed from observed depth and the "
        "venues' published fee schedules — no probability estimate is involved, "
        "which is why this section has content on a day when nothing is actionable.",
        "",
        "Because a binary contract pays exactly $1, cost per contract is also the "
        "probability you need just to break even.",
        "",
    ]

    if cost.sectors:
        out += [
            "### By category",
            "",
            "| Category | Contracts | Median premium | Priced from a book |",
            "| --- | ---: | ---: | ---: |",
        ]
        for sector in cost.sectors:
            out.append(
                f"| {sector.category} | {sector.n:,} | "
                f"{_pct(sector.median_premium_ratio)} | {_pct(sector.depth_coverage)} |"
            )
        out += [
            "",
            "Where *priced from a book* is low the premium excludes order-book "
            "depth entirely, which makes it a **floor** on the true cost rather "
            "than an estimate of it — those categories are understated here, not "
            "flattered.",
            "",
        ]

    if cost.costliest:
        out += [
            "### Widest gap between the quoted price and the real one",
            "",
            "Ranked by premium as a share of the quoted price, not by dollars: a "
            "1c contract carrying a 1c fee is the finding, and it would never "
            "surface in a ranking led by absolute cost.",
            "",
            "| Contract | Quoted | True cost | Premium | Break-even |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
        for row in cost.costliest:
            breakeven = (
                "impossible"
                if row.breakeven_probability is None
                else _pct(row.breakeven_probability)
            )
            out.append(
                f"| {row.title} | {_cents(row.nominal_price)} | "
                f"{_cents(row.measured_cost)} | {_pct(row.measured_premium_ratio)} | "
                f"{breakeven} |"
            )
        out.append("")

    return out


def _md_watchlist(entries: list[WatchlistEntry]) -> list[str]:
    if not entries:
        return []
    out = [
        "## Watchlist — examined and explicitly NOT actionable",
        "",
        "These are near misses, shown so the reasoning is visible. **None of them is a "
        "recommendation**, and no net edge is quoted for any of them: an edge printed "
        "next to something that failed the gate is how a diagnostic list turns into a "
        "second recommendation list.",
        "",
        "| Market | Side | Resolves | Why it is not actionable |",
        "| --- | --- | --- | --- |",
    ]
    for e in entries:
        reasons = " ".join(e.blocking_reasons)
        out.append(
            f"| {e.title} | {e.side.upper()} | {_when(e.resolution_date)} | {reasons} |"
        )
    out.append("")
    return out


def _md_daily(report: DigestReport) -> str:
    out = [
        f"# {_subject(report)}",
        "",
    ]
    out += _md_banner(report)
    out += [
        f"**{report.headline}**",
        "",
    ]

    if not report.actionable_allowed:
        out += [
            "No candidates are published in this report, and none were computed.",
            "",
            "The Snapshot did not meet the bar required to issue actionable research, so "
            "the generator stopped before reading it. This is the intended behaviour: a "
            "stale quote presented as a candidate would be worse than no report at all.",
            "",
            "**Why the data was refused**",
            "",
        ]
        out += [f"- {reason}" for reason in report.gate.reasons]
        out += [
            "",
            "**The service level this report is held to**",
            "",
            f"- Snapshot no older than {report.gate.sla.snapshot_max_age_seconds // 3600} hours",
            f"- Freshest quote no older than {report.gate.sla.quote_max_age_seconds // 3600} hours",
            f"- The order book behind any actionable candidate no older than "
            f"{report.gate.sla.candidate_quote_max_age_seconds // 60} minutes",
        ]
        out += [
            "",
            "Nothing is wrong with your membership, and no action is required. The next "
            "report resumes automatically once a fresh Snapshot is published and validated.",
            "",
        ]
        out += _md_gate(report)
        out += _md_disclaimer()
        return "\n".join(out)

    if report.candidates:
        out += [
            f"Horizon: markets resolving within {report.horizon}.",
            "",
        ]
        for i, candidate in enumerate(report.candidates, start=1):
            out += _md_candidate(i, candidate)
    else:
        out += [
            f"Every open market resolving within {report.horizon} was priced against its "
            "own ask ladder, and none of them cleared the admission gate after fees, "
            "slippage, transfer and capital costs.",
            "",
            "This is the ordinary result on efficiently priced venues, and reporting it is "
            "the product working rather than failing. A scanner that finds something every "
            "day has set its bar to guarantee that.",
            "",
        ]

    out += _md_cost(report.cost)
    out += _md_watchlist(report.watchlist)

    out += ["## How today's markets were filtered", "", "| Stage | Count | |", "| --- | ---: | --- |"]
    for stage in report.funnel:
        out.append(f"| {stage.label} | {stage.count:,} | {stage.note} |")
    out.append("")

    if report.top_rejections:
        out += ["## Where candidates stopped", "", "| Reason | Count |", "| --- | ---: |"]
        for rejection in report.top_rejections:
            out.append(f"| {rejection.reason} | {rejection.count:,} |")
        out.append("")

    out += _md_gate(report)
    out += _md_disclaimer()
    return "\n".join(out)


def _md_weekly(report: OutcomeReview) -> str:
    out = [f"# {_subject(report)}", ""]
    out += _md_banner(report)
    out += [
        f"**{report.headline}**",
        "",
        f"Window: {report.window_start.strftime('%Y-%m-%d')} to "
        f"{report.window_end.strftime('%Y-%m-%d')} (UTC).",
        "",
        "## Recommendation scorecard",
        "",
        f"- Recommendations published in the window: **{report.recommendations_published}**",
        f"- Resolved in the window: **{len(report.resolved)}**",
        "",
    ]
    if report.resolved:
        out += [
            "| Market | Side | Settled | Result | Entry all-in | Realised / contract |",
            "| --- | --- | --- | --- | ---: | ---: |",
        ]
        for r in report.resolved:
            out.append(
                f"| {r.title} | {r.side.upper()} | {_when(r.settled_at)} | {r.result} | "
                f"{_cents(r.entry_all_in)} | {_cents(r.realized_profit_per_contract)} |"
            )
        out.append("")

    out += [
        "## Forecast accuracy on everything that settled",
        "",
        f"{report.markets_settled_in_window:,} scored markets resolved inside the window. "
        "These are graded whether or not they were ever recommended, which is what keeps "
        "this section meaningful in a week with no recommendations.",
        "",
        "Brier score: mean squared error of the probability forecast. **Lower is better**, "
        "0 is perfect, 0.25 is a coin flip.",
        "",
        "| Set | n | Model Brier | Market Brier | Model advantage |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for bucket in report.accuracy:
        adv = "—" if bucket.improvement is None else f"{bucket.improvement:+.5f}"
        model = "—" if bucket.model_brier is None else f"{bucket.model_brier:.5f}"
        market = "—" if bucket.market_brier is None else f"{bucket.market_brier:.5f}"
        out.append(f"| {bucket.label} | {bucket.n:,} | {model} | {market} | {adv} |")
    out.append("")
    for bucket in report.accuracy:
        if bucket.note:
            out.append(f"- *{bucket.label}*: {bucket.note}")
    out.append("")

    out += ["## What this week's numbers cannot tell you", ""]
    out += [f"- {limitation}" for limitation in report.limitations]
    out.append("")

    out += _md_gate(report)
    out += _md_disclaimer()
    return "\n".join(out)


# -------------------------------------------------------------- plain text --


def _wrap(text: str, width: int = 78, indent: str = "", hanging: str | None = None) -> list[str]:
    """Wrap to ``width``. ``hanging`` indents continuation lines.

    Bullets read badly without it: a wrapped second line starting at the same
    column as the "-" looks like a new item rather than a continuation.
    """
    continuation = indent if hanging is None else hanging
    words = text.split()
    if not words:
        return []
    lines: list[str] = []
    current = indent + words[0]
    for word in words[1:]:
        if len(current) + 1 + len(word) <= width:
            current = f"{current} {word}"
        else:
            lines.append(current)
            current = continuation + word
    lines.append(current)
    return lines


def _bullet(text: str, indent: str = "  ") -> list[str]:
    """A '- ' bullet whose wrapped lines align under the text, not the dash."""
    return _wrap(f"- {text}", indent=indent, hanging=indent + "  ")


def _rule(char: str = "=") -> str:
    return char * 78


def to_text(report: Report) -> str:
    """Plain text carrying every figure the HTML does, not a courtesy summary."""
    out = [_rule(), _subject(report), _rule(), ""]
    lines = _historical_lines(report)
    if lines is not None:
        # Not uppercased: the sentence must appear verbatim in all three formats
        # so it reads the same everywhere and can be asserted exactly.
        out += ["!!! " + _rule("!")[:74] + " !!!"]
        out += _wrap(lines[0], indent="  ")
        out += [""]
        out += _wrap(lines[1], indent="  ")
        out += ["!!! " + _rule("!")[:74] + " !!!", ""]
    out += [report.headline, ""]

    if isinstance(report, OutcomeReview):
        out += _wrap(
            f"Window: {report.window_start.strftime('%Y-%m-%d')} to "
            f"{report.window_end.strftime('%Y-%m-%d')} UTC."
        )
        out += ["", "RECOMMENDATION SCORECARD", _rule("-"), ""]
        out.append(f"  Published in window : {report.recommendations_published}")
        out.append(f"  Resolved in window  : {len(report.resolved)}")
        for r in report.resolved:
            out.append(
                f"  - {r.title} [{r.side.upper()}] {r.result} "
                f"realised {_cents(r.realized_profit_per_contract)}"
            )
        out += ["", "FORECAST ACCURACY (Brier — lower is better)", _rule("-"), ""]
        out.append(f"  {report.markets_settled_in_window:,} scored markets settled in the window.")
        out.append("")
        for b in report.accuracy:
            model = "—" if b.model_brier is None else f"{b.model_brier:.5f}"
            market = "—" if b.market_brier is None else f"{b.market_brier:.5f}"
            adv = "—" if b.improvement is None else f"{b.improvement:+.5f}"
            out.append(f"  {b.label}")
            out.append(f"    n={b.n}  model={model}  market={market}  advantage={adv}")
            out += _wrap(b.note, indent="    ")
            out.append("")
        out += ["WHAT THIS CANNOT TELL YOU", _rule("-"), ""]
        for limitation in report.limitations:
            out += _bullet(limitation)
            out.append("")
    elif not report.actionable_allowed:
        out += _wrap(
            "No candidates are published in this report, and none were computed. The "
            "Snapshot did not meet the bar required to issue actionable research, so the "
            "generator stopped before reading it."
        )
        out += ["", "WHY THE DATA WAS REFUSED", _rule("-"), ""]
        for reason in report.gate.reasons:
            out += _bullet(reason)
        out += ["", "THE SERVICE LEVEL THIS REPORT IS HELD TO", _rule("-"), ""]
        sla = report.gate.sla
        out.append(f"  Snapshot age                 <= {sla.snapshot_max_age_seconds // 3600} hours")
        out.append(f"  Freshest quote age           <= {sla.quote_max_age_seconds // 3600} hours")
        out.append(
            f"  Quote behind a candidate     <= {sla.candidate_quote_max_age_seconds // 60} minutes"
        )
        out += [""]
        out += _wrap(
            "Nothing is wrong with your membership and no action is required. The next "
            "report resumes automatically once a fresh Snapshot is published and validated."
        )
        out.append("")
    else:
        if report.candidates:
            out.append(f"Horizon: markets resolving within {report.horizon}.")
            out.append("")
            for i, c in enumerate(report.candidates, start=1):
                out += [_rule("-"), f"{i}. {c.title}", _rule("-"), ""]
                out.append(f"  {c.side.upper()} on {c.platform}  ({c.platform_market_id})")
                out.append("")
                out.append(f"  Executable ask (VWAP)        : {_cents(c.executable_ask)}")
                out.append(f"  All-in cost per contract     : {_cents(c.all_in_cost)}")
                out.append(f"  Market-implied probability   : {_pct(c.market_implied_probability)}")
                out.append(f"  Independent probability      : {_pct(c.independent_probability)}")
                out.append(f"  Decision-adjusted probability: {_pct(c.decision_adjusted_probability)}")
                out.append(f"  Net edge after costs         : {_cents(c.net_edge_after_costs)}/contract")
                out.append(f"  Net ROI                      : {_pct(c.net_roi)}")
                out.append(f"  Resting liquidity            : {_usd(c.liquidity_usd)}")
                out.append(f"  Size cap (marginal EV > 0)   : {float(c.position_cap_contracts):.0f} contracts")
                out.append(f"  Resolution date              : {_when(c.resolution_date)}")
                out.append(f"  Settlement source            : {c.settlement_source or 'not stated'}")
                out += ["", "  RULES RISK", ""]
                for r in c.rules_risk:
                    out += _bullet(r, indent="    ")
                out += ["", "  WHAT WOULD INVALIDATE THIS", ""]
                for r in c.invalidation_conditions:
                    out += _bullet(r, indent="    ")
                out.append("")
        else:
            out += _wrap(
                f"Every open market resolving within {report.horizon} was priced against its "
                "own ask ladder, and none cleared the admission gate after fees, slippage, "
                "transfer and capital costs. This is the ordinary result on efficiently "
                "priced venues, and reporting it is the product working rather than failing."
            )
            out.append("")

        cost = report.cost
        if cost is not None and cost.has_content:
            size = int(cost.priced_at_size)
            out += [
                f"WHAT IT COSTS TO TRADE TODAY - {cost.markets_priced:,} CONTRACTS PRICED",
                _rule("-"),
                "",
            ]
            out += _wrap(
                f"Every figure below is the cost of buying {size} contracts, above the "
                "price on the venue's screen, computed from observed depth and the "
                "venues' published fee schedules. No probability estimate is involved, "
                "which is why this section has content on a day when nothing is "
                "actionable. A binary contract pays exactly $1, so cost per contract is "
                "also the probability needed just to break even."
            )
            out.append("")
            if cost.sectors:
                out.append("  By category:")
                out.append("")
                for sector in cost.sectors:
                    out.append(
                        f"    {sector.category:<14} {sector.n:>5} contracts   "
                        f"median premium {_pct(sector.median_premium_ratio):>7}   "
                        f"from a book {_pct(sector.depth_coverage):>7}"
                    )
                out.append("")
                out += _wrap(
                    "Where 'from a book' is low the premium excludes order-book depth "
                    "entirely, which makes it a floor on the true cost rather than an "
                    "estimate of it: those categories are understated here.",
                    indent="  ",
                )
                out.append("")
            if cost.costliest:
                out.append("  Widest gap between the quoted price and the real one:")
                out.append("")
                for row in cost.costliest:
                    breakeven = (
                        "impossible"
                        if row.breakeven_probability is None
                        else _pct(row.breakeven_probability)
                    )
                    out.append(f"    {row.title[:60]}")
                    out.append(
                        f"      quoted {_cents(row.nominal_price)} -> true "
                        f"{_cents(row.measured_cost)}  "
                        f"(+{_pct(row.measured_premium_ratio)})  "
                        f"break-even {breakeven}"
                    )
                out.append("")

        if report.watchlist:
            out += ["WATCHLIST - EXAMINED AND EXPLICITLY NOT ACTIONABLE", _rule("-"), ""]
            out += _wrap(
                "Near misses, shown so the reasoning is visible. None of these is a "
                "recommendation, and no net edge is quoted for any of them."
            )
            out.append("")
            for e in report.watchlist:
                out.append(f"  - {e.title} [{e.side.upper()}]")
                out.append(f"    resolves {_when(e.resolution_date)}")
                for reason in e.blocking_reasons:
                    out += _wrap(reason, indent="    ")
                out.append("")

        out += ["HOW TODAY'S MARKETS WERE FILTERED", _rule("-"), ""]
        for stage in report.funnel:
            out.append(f"  {stage.label:<44} {stage.count:>7,}   {stage.note}")
        out.append("")
        if report.top_rejections:
            out += ["WHERE CANDIDATES STOPPED", _rule("-"), ""]
            for rejection in report.top_rejections:
                out.append(f"  {rejection.count:>7,}  {rejection.reason}")
            out.append("")

    g = report.gate
    out += ["SNAPSHOT PROVENANCE", _rule("-"), ""]
    out.append(f"  Snapshot        : {g.snapshot_id or 'unknown'}")
    out.append(f"  Pipeline commit : {g.code_commit_sha or 'unknown'}")
    out.append(f"  Model version   : {g.model_version or 'unknown'}")
    out.append(f"  Data cutoff     : {_when(g.source_data_cutoff)}")
    out.append(f"  Freshest quote  : {_when(g.freshest_quote_observed_at)}")
    out.append(f"  Generated for   : {_when(g.as_of)}")
    for f in g.freshness:
        age = "unknown" if f.age_hours is None else f"{f.age_hours:.1f} h"
        supports = "no" if f.blocks else "yes"
        label = f.data_type.replace("_", " ")
        out.append(
            f"  {label:<16}: {f.state}, {age}, can support a candidate: {supports}"
        )
    note = _candidate_freshness_sentence(report)
    if note:
        out += [""]
        out += _bullet(note)
    out += ["", "TERMS OF THIS REPORT", _rule("-"), ""]
    for line in DISCLAIMER_LINES:
        out += _bullet(line)
        out.append("")
    return "\n".join(out)


# -------------------------------------------------------------- html email --

_FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
_MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"


def _h(text: object) -> str:
    return escape(str(text), quote=True)


def _html_kv(rows: list[tuple[str, str]], *, strong_keys: tuple[str, ...] = ()) -> str:
    cells = []
    for key, value in rows:
        weight = "600" if key in strong_keys else "400"
        cells.append(
            f'<tr>'
            f'<td style="padding:6px 12px 6px 0;color:#5c6470;font-size:14px;'
            f'border-bottom:1px solid #e8eaee;">{_h(key)}</td>'
            f'<td style="padding:6px 0;font-size:14px;font-weight:{weight};'
            f'font-family:{_MONO};border-bottom:1px solid #e8eaee;text-align:right;">'
            f"{_h(value)}</td></tr>"
        )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="border-collapse:collapse;margin:12px 0;">{"".join(cells)}</table>'
    )


def _html_list(items: list[str]) -> str:
    lis = "".join(
        f'<li style="margin:0 0 6px 0;color:#5c6470;font-size:14px;line-height:1.55;">'
        f"{_h(i)}</li>"
        for i in items
    )
    return f'<ul style="margin:8px 0 16px 0;padding-left:20px;">{lis}</ul>'


def _html_h2(text: str) -> str:
    return (
        f'<h2 style="margin:28px 0 8px 0;font-size:17px;font-weight:600;'
        f'color:#14171c;">{_h(text)}</h2>'
    )


def _html_p(text: str) -> str:
    return (
        f'<p style="margin:0 0 14px 0;font-size:14px;line-height:1.6;color:#5c6470;">'
        f"{_h(text)}</p>"
    )


def to_html_email(report: Report) -> str:
    """Table-based, inline-styled, no external assets and no tracking pixel."""
    banner = _historical_lines(report)
    body: list[str] = []
    if banner is not None:
        body.append(
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            'style="border-collapse:collapse;margin:0 0 20px 0;background:#fdf3e3;'
            'border:1px solid #d9a441;border-left:4px solid #8a5a00;border-radius:3px;">'
            '<tr><td style="padding:12px 14px;">'
            '<p style="margin:0 0 6px 0;font-size:13px;font-weight:700;color:#8a5a00;'
            'text-transform:uppercase;letter-spacing:0.06em;">'
            f"{_h(banner[0])}</p>"
            '<p style="margin:0;font-size:13px;line-height:1.5;color:#5c4310;">'
            f"{_h(banner[1])}</p>"
            "</td></tr></table>"
        )
    body += [
        f'<h1 style="margin:0 0 4px 0;font-size:20px;font-weight:600;color:#14171c;">'
        f"{_h(_subject(report))}</h1>",
        f'<p style="margin:0 0 20px 0;font-size:15px;font-weight:600;color:#14171c;">'
        f"{_h(report.headline)}</p>",
    ]

    if isinstance(report, OutcomeReview):
        body.append(
            _html_p(
                f"Window: {report.window_start.strftime('%Y-%m-%d')} to "
                f"{report.window_end.strftime('%Y-%m-%d')} UTC."
            )
        )
        body.append(_html_h2("Recommendation scorecard"))
        body.append(
            _html_kv(
                [
                    ("Recommendations published", f"{report.recommendations_published}"),
                    ("Resolved in window", f"{len(report.resolved)}"),
                ]
            )
        )
        for r in report.resolved:
            body.append(
                _html_kv(
                    [
                        (r.title, r.result),
                        ("Realised per contract", _cents(r.realized_profit_per_contract)),
                    ]
                )
            )
        body.append(_html_h2("Forecast accuracy on everything that settled"))
        body.append(
            _html_p(
                f"{report.markets_settled_in_window:,} scored markets resolved inside the "
                "window, graded whether or not they were ever recommended. Brier score: "
                "lower is better, 0 is perfect, 0.25 is a coin flip."
            )
        )
        for b in report.accuracy:
            body.append(
                _html_kv(
                    [
                        (b.label, f"n = {b.n:,}"),
                        ("Model Brier", "—" if b.model_brier is None else f"{b.model_brier:.5f}"),
                        ("Market Brier", "—" if b.market_brier is None else f"{b.market_brier:.5f}"),
                        (
                            "Model advantage",
                            "—" if b.improvement is None else f"{b.improvement:+.5f}",
                        ),
                    ],
                    strong_keys=("Model advantage",),
                )
            )
            if b.note:
                body.append(_html_p(b.note))
        body.append(_html_h2("What this week's numbers cannot tell you"))
        body.append(_html_list(report.limitations))

    elif not report.actionable_allowed:
        body.append(
            _html_p(
                "No candidates are published in this report, and none were computed. The "
                "Snapshot did not meet the bar required to issue actionable research, so "
                "the generator stopped before reading it. A stale quote presented as a "
                "candidate would be worse than no report at all."
            )
        )
        body.append(_html_h2("Why the data was refused"))
        body.append(_html_list(report.gate.reasons))
        body.append(_html_h2("The service level this report is held to"))
        body.append(
            _html_kv(
                [
                    (
                        "Snapshot age",
                        f"<= {report.gate.sla.snapshot_max_age_seconds // 3600} hours",
                    ),
                    (
                        "Freshest quote age",
                        f"<= {report.gate.sla.quote_max_age_seconds // 3600} hours",
                    ),
                    (
                        "Quote behind a candidate",
                        f"<= {report.gate.sla.candidate_quote_max_age_seconds // 60} minutes",
                    ),
                ]
            )
        )
        body.append(
            _html_p(
                "Nothing is wrong with your membership and no action is required. The next "
                "report resumes automatically once a fresh Snapshot is published and validated."
            )
        )
    else:
        if report.candidates:
            body.append(_html_p(f"Horizon: markets resolving within {report.horizon}."))
            for i, c in enumerate(report.candidates, start=1):
                body.append(
                    f'<h2 style="margin:28px 0 2px 0;font-size:17px;font-weight:600;'
                    f'color:#14171c;">{i}. {_h(c.title)}</h2>'
                )
                body.append(
                    f'<p style="margin:0 0 8px 0;font-size:13px;color:#848c98;">'
                    f"{_h(c.side.upper())} on {_h(c.platform)} · {_h(c.platform_market_id)}</p>"
                )
                body.append(
                    _html_kv(
                        [
                            ("Executable ask (VWAP at size)", _cents(c.executable_ask)),
                            ("All-in cost per contract", _cents(c.all_in_cost)),
                            ("Market-implied probability", _pct(c.market_implied_probability)),
                            ("Independent probability", _pct(c.independent_probability)),
                            (
                                "Decision-adjusted probability",
                                _pct(c.decision_adjusted_probability),
                            ),
                            ("Net edge after costs", f"{_cents(c.net_edge_after_costs)}/contract"),
                            ("Net ROI", _pct(c.net_roi)),
                            ("Resting liquidity", _usd(c.liquidity_usd)),
                            (
                                "Size cap (marginal EV > 0)",
                                f"{float(c.position_cap_contracts):.0f} contracts",
                            ),
                            ("Resolution date", _when(c.resolution_date)),
                            ("Settlement source", c.settlement_source or "not stated"),
                        ],
                        strong_keys=(
                            "Executable ask (VWAP at size)",
                            "Decision-adjusted probability",
                            "Net edge after costs",
                        ),
                    )
                )
                body.append(_html_h2("Rules risk"))
                body.append(_html_list(c.rules_risk))
                body.append(_html_h2("What would invalidate this"))
                body.append(_html_list(c.invalidation_conditions))
        else:
            body.append(
                _html_p(
                    f"Every open market resolving within {report.horizon} was priced against "
                    "its own ask ladder, and none cleared the admission gate after fees, "
                    "slippage, transfer and capital costs. This is the ordinary result on "
                    "efficiently priced venues, and reporting it is the product working "
                    "rather than failing."
                )
            )

        cost = report.cost
        if cost is not None and cost.has_content:
            size = int(cost.priced_at_size)
            body.append(
                _html_h2(
                    f"What it costs to trade today — {cost.markets_priced:,} "
                    "contracts priced"
                )
            )
            body.append(
                _html_p(
                    f"Every figure below is the cost of buying {size} contracts, above "
                    "the price on the venue's screen, computed from observed depth and "
                    "the venues' published fee schedules. No probability estimate is "
                    "involved, which is why this section has content on a day when "
                    "nothing is actionable. A binary contract pays exactly $1, so cost "
                    "per contract is also the probability needed just to break even."
                )
            )
            if cost.sectors:
                body.append(
                    _html_kv(
                        [
                            (
                                _h(sector.category),
                                f"{_pct(sector.median_premium_ratio)} median premium "
                                f"({sector.n:,} contracts, "
                                f"{_pct(sector.depth_coverage)} priced from a book)",
                            )
                            for sector in cost.sectors
                        ]
                    )
                )
                body.append(
                    _html_p(
                        "Where the book share is low the premium excludes order-book "
                        "depth entirely, which makes it a floor on the true cost rather "
                        "than an estimate of it: those categories are understated here."
                    )
                )
            if cost.costliest:
                body.append(
                    _html_h2("Widest gap between the quoted price and the real one")
                )
                body.append(
                    _html_kv(
                        [
                            (
                                _h(row.title),
                                f"quoted {_cents(row.nominal_price)} &rarr; true "
                                f"{_cents(row.measured_cost)} "
                                f"(+{_pct(row.measured_premium_ratio)}), break-even "
                                + (
                                    "impossible"
                                    if row.breakeven_probability is None
                                    else _pct(row.breakeven_probability)
                                ),
                            )
                            for row in cost.costliest
                        ]
                    )
                )

        if report.watchlist:
            body.append(_html_h2("Watchlist — examined and explicitly NOT actionable"))
            body.append(
                _html_p(
                    "Near misses, shown so the reasoning is visible. None of these is a "
                    "recommendation, and no net edge is quoted for any of them."
                )
            )
            for e in report.watchlist:
                body.append(
                    f'<p style="margin:14px 0 2px 0;font-size:14px;font-weight:600;'
                    f'color:#14171c;">{_h(e.title)} <span style="font-weight:400;'
                    f'color:#848c98;">[{_h(e.side.upper())}]</span></p>'
                )
                body.append(
                    f'<p style="margin:0 0 4px 0;font-size:12px;color:#848c98;">'
                    f"Resolves {_h(_when(e.resolution_date))}</p>"
                )
                body.append(_html_list(e.blocking_reasons))

        body.append(_html_h2("How today's markets were filtered"))
        body.append(_html_kv([(s.label, f"{s.count:,}") for s in report.funnel]))
        if report.top_rejections:
            body.append(_html_h2("Where candidates stopped"))
            body.append(
                _html_kv([(r.reason, f"{r.count:,}") for r in report.top_rejections])
            )

    g = report.gate
    body.append(_html_h2("Snapshot provenance"))
    body.append(
        _html_kv(
            [
                ("Snapshot", g.snapshot_id or "unknown"),
                ("Pipeline commit", g.code_commit_sha or "unknown"),
                ("Model version", g.model_version or "unknown"),
                ("Data cutoff", _when(g.source_data_cutoff)),
                ("Freshest observed quote", _when(g.freshest_quote_observed_at)),
                ("Report generated for", _when(g.as_of)),
                *[
                    (
                        f.data_type.replace("_", " ").capitalize(),
                        f"{f.state}, "
                        f"{'unknown' if f.age_hours is None else f'{f.age_hours:.1f} h'}"
                        f", can support a candidate: {'no' if f.blocks else 'yes'}",
                    )
                    for f in g.freshness
                ],
            ]
        )
    )

    _note = _candidate_freshness_sentence(report)
    if _note:
        body.append(
            '<p style="margin:12px 0 0 0;font-size:12px;line-height:1.55;'
            f'color:#848c98;">{_h(_note)}</p>'
        )

    body.append(
        '<div style="margin-top:28px;padding-top:16px;border-top:1px solid #d9dce2;">'
        '<p style="margin:0 0 8px 0;font-size:11px;font-weight:600;letter-spacing:0.07em;'
        'text-transform:uppercase;color:#848c98;">Terms of this report</p>'
        + "".join(
            f'<p style="margin:0 0 8px 0;font-size:12px;line-height:1.55;color:#848c98;">'
            f"{_h(line)}</p>"
            for line in DISCLAIMER_LINES
        )
        + "</div>"
    )

    return (
        "<!doctype html><html><head>"
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{_h(_subject(report))}</title>"
        "</head>"
        '<body style="margin:0;padding:0;background:#f4f5f7;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="background:#f4f5f7;padding:24px 12px;"><tr><td align="center">'
        '<table role="presentation" width="640" cellpadding="0" cellspacing="0" '
        'style="max-width:640px;width:100%;background:#ffffff;border:1px solid #d9dce2;'
        'border-radius:3px;">'
        f'<tr><td style="padding:28px 32px;font-family:{_FONT};color:#14171c;">'
        + "".join(body)
        + "</td></tr></table></td></tr></table></body></html>"
    )
