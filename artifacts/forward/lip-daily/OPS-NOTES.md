# LIP study - operations notes (not part of the frozen code or the pre-registration)

## 2026-10-04 (UTC 2026-10-03 22:47) - the cron lines are guarded on the UTC hour

The machine's timezone moved from Asia/Shanghai (UTC+8) to Asia/Seoul (UTC+9) on 2026-10-03.
Both cron lines were written in local time (`25 20` for LIP = 12:25 UTC, `10 10` for the maker
study = 02:10 UTC, at UTC+8), so at UTC+9:

- the LIP line fired at 11:25 UTC, inside the maker study's window; the recorder refused (exit 3)
  and day 2026-10-03 was lost (`runs.jsonl`: RECORDER_FAILURE, refused_inside_maker_window);
- the maker line would have fired at 01:10 UTC, while the previous LIP day is still draining
  (until 02:00 UTC) - an isolation breach, which stops the LIP study. It did not happen: on
  2026-10-03 the machine was asleep at that time, and no LIP day was running on 2026-10-04.

The crontab now fires both lines every hour and runs each only in its registered UTC hour:

    10 * * * * [ "$(date -u +\%H)" = "02" ] && .../scripts/record_maker_day.sh
    25 * * * * [ "$(date -u +\%H)" = "12" ] && .../scripts/record_lip_day.sh

Both studies therefore start at their registered UTC times (02:10 and 12:25) in any whole-hour
timezone. Nothing frozen changed: `freeze-manifest --check` stays clean (the crontab is not part
of the freeze; `docs/lip-runbook.md` still shows the original local-time line and is left as
frozen). The previous crontab is saved at /private/tmp/claude-501/crontab.before-tz.

## Day log so far

| day | run | counted | note |
|---|---|---|---|
| 2026-10-01 | SUCCESS | yes (0.9317) | no sleep |
| 2026-10-02 | SUCCESS | no (0.4141) | the machine slept 6.6 h of the window (11 wall-clock jumps) |
| 2026-10-03 | refused (exit 3) | no | cron fired at 11:25 UTC after the timezone change |
| 2026-10-04 | SUCCESS | yes (0.938) | slept from 00:17 UTC, after the counted window |
| 2026-10-05 | never launched | no | the machine was asleep at 12:25 UTC (lid closed, on battery; timezone now Asia/Shanghai, UTC+8, so 20:25 local); `run-status`: NEVER_LAUNCHED. Not re-run by hand (runbook). |
| 2026-10-06 | never launched | no | asleep at 12:25 UTC (lid closed on battery 38%; dark wakes only, full wake 13:10 UTC); then on battery until it hibernated at 1% (17:47 UTC). `run-status`: NEVER_LAUNCHED. |

Tally after 2026-10-06: 2 counted days of 6. The pre-registration needs at least 20 counted days
(else INSUFFICIENT_EVIDENCE); 24 calendar days remain (10-07..10-30), so at most 6 more may be lost.

## The maker day can push the LIP start back (found 2026-10-07)

The maker study's 20 sessions take about 10 h from 02:10 UTC when the machine stays awake; a sleep
stretches them. The LIP recorder waits for the maker study's lock and process to clear and gives
up at 13:30 UTC. On 2026-10-02 the maker day finished at 13:25:51 UTC after sleeps, and the LIP
recorder started at 13:26:07 (startup_delay_s 3,668), about an hour into its window. On 2026-10-07
a 40-minute clamshell sleep during maker session 1 (10:40-11:20 local) puts the maker finish near
13:05 UTC if nothing else sleeps; about 25 more minutes of sleep before then and the LIP day is lost.
A counted LIP day needs book reads on at least 80% of 44,100 ticks (12:30-00:45 UTC), so a late
start of up to about 2.4 h is survivable only if nothing else is lost.

| day | run | counted | note |
|---|---|---|---|
| 2026-10-07 | never launched | no | asleep at 12:25 UTC (clamshell sleep on battery since 08:40 UTC, dark wakes only); `run-status`: NEVER_LAUNCHED. The machine hibernated at 2% battery at 18:28 UTC and was rebooted at 03:01 UTC 2026-10-08 (the `pmvl schedule` collector came back by itself). |

Tally after 2026-10-07: 2 counted days of 7. At least 20 are needed; 23 calendar days remain
(10-08..10-30), so at most 5 more may be lost.
| 2026-10-08 | SUCCESS, on time (12:25:02 UTC) | no (0.1760: 7,763 of 44,100 ticks) | awake on battery until it hibernated at 1% at 17:15 UTC; woke on AC at 00:34 UTC 2026-10-09 (heartbeat: slept 26,320 s). The 80% rule cannot be met; the manifest at the end of the run gives the exact share. |

Tally after 2026-10-08: 2 counted days of 8. 22 calendar days remain (10-09..10-30), so at most 4
more may be lost.
  2026-10-08 detail: besides the 7.3 h hibernation, book reads failed in bursts (the circuit opened 64 times) in the
  hour before the battery ran out (16:05-17:13 UTC) and after the wake (00:34 UTC on); none from 12:25 to 16:05 UTC.
| 2026-10-09 | SUCCESS, on time (12:25:01 UTC), on AC from 14:39 UTC | no (0.7750: 34,179 of 44,100 ticks; 1,101 short of 80%) | awake and error-free until the lid was closed at 23:28 UTC (07:28 local), still 77 min before the window ends (00:45 UTC = 08:45 local); clamshell sleep even on AC, then unplugged at 23:49 UTC; three sleeps total 7,836 s. Keeping the lid open to 08:45 local would have counted it (about 0.88). |

Tally after 2026-10-09: 2 counted days of 9. 21 calendar days remain (10-10..10-30), so at most 3
more may be lost. The counted window ends at 00:45 UTC (08:45 local): the lid must stay open at
least until then; a closed lid sleeps even on AC (no external display).
| 2026-10-10 | SUCCESS, on time (12:25:00 UTC), on AC, lid open past 00:45 UTC | **yes (0.8936: 39,409 of 44,100 ticks)** | no sleep, no circuit opens. |

Tally after 2026-10-10: 3 counted days of 10. 20 calendar days remain (10-11..10-30); 17 more are
needed, so at most 3 more may be lost.
| 2026-10-11 | SUCCESS; started 12:45:02 UTC after waiting 1,202 s for the maker day (20/20 sessions, finished 12:44:57 UTC) | **yes (0.8942)** | no sleep. A full maker day with no sleep at all still ends about 12:45 UTC, so a ~20-minute LIP delay is normal. |

Tally after 2026-10-11: 4 counted days of 11. 19 calendar days remain (10-12..10-30); 16 more are
needed, so at most 3 more may be lost.
| 2026-10-12 | SUCCESS, on time (12:25:00 UTC); on battery until plugged in during the night | **yes (0.8596)** | one 17 s sleep (05:15 local); circuit opened 4 times 18:35-18:41 UTC. |

2026-10-13 (counted day 5, the first interim checkpoint): `lip_cli study` -> INTERIM_NOT_A_DECISION,
counted_days 5, isolation_breaches 0, verdict_date 2026-11-13 (counts only, no dollars, by design).
`freeze-manifest --check` -> unchanged, no drift. Tally: 5 counted of 12; 18 calendar days remain
(10-13..10-30), 15 more needed, so at most 3 more may be lost.
The maker day 2026-10-13 started at 02:11:42 UTC and the machine slept 10:04-10:34 local (lid
closed on battery), so it will finish later than usual; the LIP start waits for it (gives up 13:30 UTC).
| 2026-10-13 | never launched | no | asleep at 12:25 UTC (lid closed on battery from 11:57 UTC, dark wakes only); `run-status`: NEVER_LAUNCHED. Correction to the note above: the maker day 2026-10-13 had already died at about 02:23 UTC (0 sessions), so it never delayed anything. |

Tally after 2026-10-13: 5 counted days of 13. 17 calendar days remain (10-14..10-30); 15 more are
needed, so at most 2 more may be lost.

2026-10-19: 10-14, 10-15, 10-16, 10-18 NEVER_LAUNCHED; 10-17 DIED (run-status). Counted days stay at 5.
Only 12 calendar days remain (10-19..10-30), so at most 17 counted days are reachable: the
precondition of 20 counted days can no longer be met, and the verdict will be INSUFFICIENT_EVIDENCE
whatever happens next (pre-registration section 7, "Precondition").
