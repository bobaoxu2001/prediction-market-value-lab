# Weather source lag: the edge is real, and it belongs to whoever is fastest

The one kind of edge this project had not measured is speed. Everything before it was
microstructure (arbitrage, maker spread) or forecasting skill, and the forecasting model
already loses to the market (Brier 0.07415 against the market's 0.07167, `adr-003`).

## The thesis

A Kalshi daily-high market settles on the maximum temperature at one station. The true
daily maximum is at or above every reading taken that day, so once a reading clears a
threshold the outcome is decided. If the book still offers the certain side below a
dollar, buying it is close to riskless.

## What one afternoon showed

2026-09-23, 17:49-22:59 UTC: 42 rungs across 7 cities, books every 15 s, NWS readings
every 60 s, 1,008 book sweeps, 473 observations. **18 rungs resolved from genuine
uncertainty during the recording.**

Most of them resolved with **no crossing at all** - upper buckets and tails collapsing to
a cent as each city's peak passed. That is the market pricing "it will not get there" from
the trajectory, not reacting to a reading.

Where a crossing did happen, the clearest case is Los Angeles, "less than 78°F":

| UTC | |
|---|---|
| **20:45:00** | KLAX reads **78.80°F** - the maximum is now at least 78 |
| 20:45:43 | still uncertain: YES 0.70, **NO offered at 0.34** |
| 20:45:58 | repricing begins |
| **20:47:13** | **resolved**: YES 0.01, NO no longer offered |
| **21:06:31** | the public NWS feed first shows the 78.80°F reading |

The market begins moving **about a minute** after the reading and finishes in **about 75
seconds**. The public feed publishes it **about 21 minutes** after the reading - nineteen
minutes after there was anything left to buy.

So the edge exists. Someone holding that reading at 20:45 could have bought NO at 0.34 into
a certain dollar: **+$0.66 a contract**. Someone trading off the public feed had nothing.

## No free feed is fast enough

| source | carries | newest reading when checked |
|---|---|---|
| aviationweather.gov | hourly METAR only - cannot see a 5-minute reading at all | ~44 min |
| api.weather.gov | 5-minute ASOS, delayed | 17-46 min |
| what the market reacts to | the 5-minute reading | **~1 min** |

The participants capturing this are on a sub-minute source that neither free API offers:
The Weather Company (which is the settlement source), Synoptic, or direct ASOS.

## Mistakes this found in my own measurement

- **A market that priced it first is not a market that reacted first.** The first interim
  reported a 17-23 minute lead over the feed. The certain side had already been gone from
  the very first book, three minutes before the temperature crossed; "first book where it
  was gone" read as a lead.
- **The first polling round is not a publication time.** Treating its poll time as one
  reported five lock events that never happened.
- **Five-minute readings are whole degrees Celsius.** 96.8°F is "36°C", which only proves
  [95.9, 97.7)°F. A 0.3°F lock margin claimed locks the readings could not establish; the
  margin is now half a degree Celsius.
- **A lock detector is the wrong instrument here.** Most resolutions are the market deciding
  a level will *not* be reached, which no reading can lock. Watching prices resolve found 18
  events where watching readings cross found one.

## Verdict

Real edge; not available on public data. Capturing it needs a paid sub-minute feed and
execution fast enough to act inside a ~75-second repricing that established participants are
already taking.
