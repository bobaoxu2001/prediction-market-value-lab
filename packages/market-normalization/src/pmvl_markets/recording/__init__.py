"""Forward recording: what the book actually looked like, all legs at once.

The previous phase's positive results were all artefacts of comparing quotes that
never coexisted. Every leg was fetched in its own HTTP request, nothing recorded how
far apart those requests were, and the worst pair turned out to be three hours apart.
This package exists so that failure mode is structurally impossible rather than
merely checked for: a snapshot is a single request's response, and a snapshot that
had to be assembled from more than one request is not a snapshot.
"""

from .schema import (
    ANALYSIS_VERSION,
    STRATEGY_VERSION,
    CaptureMode,
    EventSnapshot,
    SnapshotRejection,
    TradeRecord,
)
from .reader import Recording, load_recording
from .recorder import RecorderConfig, RecorderStats, SynchronizedRecorder
from .writer import AppendOnlyLog, LogIntegrityError, read_log, verify_log

__all__ = [
    "ANALYSIS_VERSION",
    "STRATEGY_VERSION",
    "AppendOnlyLog",
    "CaptureMode",
    "EventSnapshot",
    "LogIntegrityError",
    "Recording",
    "RecorderConfig",
    "RecorderStats",
    "SnapshotRejection",
    "SynchronizedRecorder",
    "TradeRecord",
    "load_recording",
    "read_log",
    "verify_log",
]
