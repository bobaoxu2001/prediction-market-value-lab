"""The Founding Research Pilot: a paid daily digest over a validated Snapshot.

Deliberately a *reporting* layer. It adds no new economics: candidates are priced
by the same `value.ranking` code that backs the public site, so the digest and the
site can never disagree about whether something clears the bar.

What it does add is a gate (`gate.py`) that decides whether the Snapshot may be
used for actionable research at the moment of sending, and renderers that state
the same facts in Markdown, HTML email and plain text.
"""
