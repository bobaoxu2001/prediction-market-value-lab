"""Add job_runs.heartbeat_at so liveness can be told apart from age.

The active-run guard superseded a 'running' row only once it was two hours old.
That bound is safe for short jobs and wrong for long ones: ``research_books``
holds its slot for an entire issuance window by design, so any age-based bound
wide enough not to evict a healthy run is also wide enough to let a dead one
block the job past the window it was blocking.

Observed rather than theorised. A worker restarted at 19:45 UTC on 2026-08-25
left run 281 marked running; every following tick recorded itself as skipped
against it, and collection would not have resumed until 21:30 - one window lost
and most of the next - had the row not been cleared by hand.

A heartbeat separates the two cases. Long-running loop jobs stamp this column
each tick; a run that has not stamped it in five minutes is dead however long it
has legitimately been running, and one that never stamps it at all is judged on
age exactly as before.

Revision ID: 8c41f0d7ab29
Revises: 459a549459e5
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '8c41f0d7ab29'
down_revision: Union[str, None] = '459a549459e5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Nullable, and left NULL for existing rows. Back-filling it with started_at
    # would claim every historical run reported a heartbeat it never sent, and
    # would make rows that predate this column look freshly alive.
    op.add_column(
        "job_runs",
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("job_runs", "heartbeat_at")
