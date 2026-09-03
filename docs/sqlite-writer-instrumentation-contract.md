# SQLite writer-transaction instrumentation contract

```
Contract version:   2
Ratification phase: 4C0.36E
Supersedes:         v1 (Phase 4C0.34-4C0.36, never implemented)
Status:             active
```

## Why v1 was superseded

v1 specified a `t_commit_end` timestamp and derived two metrics from it,
`confirmed_write_txn_seconds` and `write_txn_upper_bound_seconds`. Phase 4C0.36D
proved from a temporary-database experiment that on this repository's stack the
value cannot be observed:

```
after_exec   INSERT INTO t VALUES (1)   51436.890535
commit_event                            51436.945568
visible_to_other_conn_during_commit_event = 0    <- not yet durable
final row count after the block         = 1
```

SQLAlchemy 2.0.44 emits `ConnectionEvents.commit` **before** the DBAPI commit
runs. A second connection still reads zero rows at that instant. The event is a
commit *request* boundary, not a completion.

`Session.after_commit` does fire after the DBAPI commit, but only for ORM
sessions. The pmvl.db writers are not uniformly ORM-based, so mixing the two
would give different writers different definitions of the same metric - a
redesign wearing a measurement's name. Rejected.

Monkeypatching `sqlite3`, replacing the DBAPI connection factory, wrapping the
production commit, writing sentinel rows or probing visibility from a second
connection were all rejected: each changes or perturbs the behaviour being
measured. Instrumentation stays measurement-only.

v1's two metric names are retired. They must not appear in active
instrumentation, because the implementation does not observe transaction
completion. Historical reports keep them marked `V1 / SUPERSEDED`.

## Observable events

| field | source | meaning |
| --- | --- | --- |
| `t_txn_begin` | `Engine.begin` | connection-level transaction opened |
| `t_first_dml_start` | `Engine.before_cursor_execute`, first DML only | the first write statement is about to execute |
| `t_first_dml_success` | `Engine.after_cursor_execute`, same statement | that statement returned without raising |
| `t_commit_requested` | `Engine.commit` | commit requested; **pre-DBAPI-commit boundary** |
| `t_rollback_requested` | `Engine.rollback` | rollback requested |

Durations use `time.monotonic()`. Wall-clock UTC is carried separately for
correlation only.

## Metrics

```
first_dml_wait_seconds
    = t_first_dml_success - t_first_dml_start

confirmed_precommit_write_seconds
    = t_commit_requested - t_first_dml_success

precommit_write_window_upper_bound_seconds
    = t_commit_requested - t_first_dml_start
```

What each one is, and is not:

- `first_dml_wait_seconds` **bounds** how long the first write took to return.
  It contains any SQLite lock wait *plus* statement execution and driver
  overhead. It is **not** an exact lock-wait measurement.
- `confirmed_precommit_write_seconds` **measures** the observable interval
  between a confirmed first write and the commit request. It **excludes** DBAPI
  commit and fsync time, so it is a **lower bound** on the true write-transaction
  span. It is **not** total write-transaction duration.
- `precommit_write_window_upper_bound_seconds` **bounds** the observable
  pre-commit window from the first DML attempt. It is an upper bound on *that
  window only*. It is **not** an upper bound on SQLite lock duration or on total
  transaction duration.

None of the three proves that SQLite held a write lock for any specific span.

## Not uniformly observable

```
exact SQLite write-lock acquisition
exact SQLite write-lock release
DBAPI commit completion
fsync completion
exact lock wait
exact total write-transaction duration
```

## Prohibited field and claim names

```
exact_lock_duration      lock_acquired_at        lock_released_at
commit_completed_at      durable_at              exact_lock_wait
t_commit_end             confirmed_write_txn_seconds
write_txn_upper_bound_seconds
```

A future phase may introduce any of these only alongside a mechanism that
genuinely measures them.

## Telemetry sink

```
TELEMETRY_WRITES_SQLITE = FALSE
```

Structured records go to the existing non-SQLite logging path. Telemetry must
never write to `pmvl.db`, to `job_runs`, or to any other SQLite database: the
measurement must not amplify the contention it exists to measure.

Telemetry failure is fail-open for the application - a logging error must never
break a healthy production transaction, and must never suppress the original
SQLite or application exception.

## Writer attribution

Attribution comes from an explicit job-scoped context variable set by the job
wrapper, never inferred from statement text or runtime length. Unattributed
transactions report `writer_path = "unknown"`.
