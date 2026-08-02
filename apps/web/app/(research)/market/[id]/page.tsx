import Link from "next/link";
import { apiGet, qs, type DataMode } from "@/lib/api";
import { humanizeSeconds, ageRelativeToSnapshot, cents, compactUsd, displayTitle, localTime, pct, prob, relativeToSnapshot, signedCents } from "@/lib/format";
import { ApiDown, DemoBanner, EmptyState, Metric, PageHeader, PlatformChip, SideChip, StateChip, VenueAvailability } from "@/components/ui";
import { PriceChart } from "@/components/PriceChart";

export const dynamic = "force-dynamic";

interface Detail {
  market: Record<string, any>;
  rule: Record<string, any> | null;
  orderbook: Record<string, any>;
  price_history: Array<{
    observed_at: string;
    yes_bid: string | null;
    yes_ask: string | null;
    mid: string | null;
  }>;
  predictions: Array<Record<string, any>>;
  evidence: Array<Record<string, any>>;
  cross_platform_matches: Array<Record<string, any>>;
  recommendations: Array<Record<string, any>>;
  settlement: Record<string, any> | null;
}

export default async function MarketDetailPage({
  params,
  searchParams,
}: {
  params: Promise<{ id: string }>;
  searchParams: Promise<{ mode?: DataMode }>;
}) {
  const { id } = await params;
  const { mode: rawMode } = await searchParams;
  const mode: DataMode = rawMode === "demo" ? "demo" : "live";

  const res = await apiGet<Detail>(`/markets/${id}${qs({ mode })}`);
  // Anchor relative times to the capture instant. On a frozen deployment now()
  // advances while the data does not, so a market with hours left at capture drifts
  // into "resolved 12h ago" while still listed as upcoming.
  const system = await apiGet<{
    snapshot_mode?: boolean;
    freshest_quote_observed_at?: string | null;
  }>("/system");
  const snapshotAt = system?.data?.snapshot_mode
    ? (system?.data?.freshest_quote_observed_at ?? null)
    : null;

  if (!res) return <ApiDown />;
  const d = res.data;
  if (!d?.market) {
    return <EmptyState title="Market not found" body="This market id does not exist in the database." />;
  }

  const m = d.market;
  const latest = d.predictions[0];

  return (
    <div>
      <PageHeader
        title={displayTitle(m.title)}
        subtitle={m.subtitle || undefined}
        right={
          <div className="flex flex-wrap items-center gap-2">
            <PlatformChip platform={m.platform} />
            <span className="chip bg-sunken text-ink-muted">
              {m.category}
            </span>
            <span className="chip bg-sunken text-ink-muted">
              {m.status}
            </span>
          </div>
        }
      />
      <DemoBanner notice={res.demo_notice} />

      {/*
       * The three probabilities, named.
       *
       * They existed already but were scattered: "Market implied" sat inside the
       * model card as one metric among eight, and the conservative bound that the
       * ranking actually decides on appeared only on the opportunities list. A
       * reader had to know the codebase to tell which number was the market's
       * opinion, which was independent of it, and which one drove a decision.
       */}
      <ProbabilityTriad
        marketImplied={latest?.market_implied_probability ?? m.best_yes_ask ?? null}
        independent={latest?.fair_probability_mean ?? null}
        decision={latest?.fair_probability_low ?? null}
        high={latest?.fair_probability_high ?? null}
        confidence={latest?.model_confidence ?? null}
        hasIndependentPrior={Boolean(latest?.has_independent_prior)}
        scored={Boolean(latest)}
      />

      {m.venue_availability ? (
        <div className="panel mb-4 p-4">
          <h2 className="text-sm font-semibold">Where this contract can be traded</h2>
          <div className="mt-2">
            <VenueAvailability venues={m.venue_availability} />
          </div>
          <p className="mt-2 text-xs text-ink-faint">
            Exchange availability is asserted only for venues read directly. Broker
            availability is never inferred from an exchange listing: brokers resell a
            subset that changes without notice and is gated by jurisdiction and
            account type, and no discovery source for them is wired up here.
          </p>
        </div>
      ) : null}

      {/* ---- quotes ---- */}
      <section className="panel mb-4 p-4">
        <h2 className="t-section-title mb-3">Quotes</h2>
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-4 lg:grid-cols-8">
          <Metric label="YES bid" value={cents(m.best_yes_bid)} />
          <Metric label="YES ask" value={cents(m.best_yes_ask)} hint="Executable price to buy YES" />
          <Metric label="NO bid" value={cents(m.best_no_bid)} />
          <Metric label="NO ask" value={cents(m.best_no_ask)} hint="Executable price to buy NO" />
          <Metric label="Spread" value={cents(m.spread)} />
          <Metric label="Book depth" value={compactUsd(m.orderbook_depth_usd)} />
          <Metric label="24h volume" value={compactUsd(m.volume_24h)} />
          <Metric label="Last trade" value={cents(m.last_trade_price)} hint="Reference only — never used as an entry price" />
        </div>
        <div className="mt-3 grid grid-cols-2 gap-4 border-t border-line-subtle pt-3 sm:grid-cols-4 lg:grid-cols-6">
          <Metric label="Tick size" value={m.tick_size} />
          <Metric label="Taker fee rate" value={m.fee_rate} hint={`Fee model: ${m.fee_type}`} />
          <Metric label="Min order" value={m.min_order_size} />
          <Metric label="Open interest" value={compactUsd(m.open_interest)} />
          <Metric label="Quote age" value={ageRelativeToSnapshot(m.quote_observed_at, snapshotAt)} />
          <Metric label="Resolves" value={relativeToSnapshot(m.expected_resolution_time, snapshotAt)} hint={localTime(m.expected_resolution_time)} />
        </div>
      </section>

      {/*
        * `min-w-0` on both cells: a grid item defaults to `min-width: auto`, so
        * the wide components table inside the model section stretched its track,
        * which stretched the row, which made every sibling section wider than the
        * viewport. The table scrolls inside its own container instead.
        */}
      <div className="grid gap-4 lg:grid-cols-3">
        {/* ---- orderbook ---- */}
        <section className="panel min-w-0 p-4">
          <h2 className="t-section-title mb-1">Order book</h2>
          <p className="mb-3 text-xs text-ink-faint">
            Observed {localTime(d.orderbook?.observed_at)}
          </p>
          {d.orderbook?.yes_asks?.length || d.orderbook?.no_asks?.length ? (
            <div className="grid grid-cols-2 gap-4">
              <BookSide title="YES asks" levels={d.orderbook.yes_asks} />
              <BookSide title="NO asks" levels={d.orderbook.no_asks} />
            </div>
          ) : (
            <p className="text-sm text-ink-faint">
              No order book captured. Only markets that clear the volume and horizon
              filters receive an orderbook fetch.
            </p>
          )}
        </section>

        {/* ---- model ---- */}
        <section className="panel min-w-0 p-4 lg:col-span-2">
          <h2 className="t-section-title mb-3">Model estimate</h2>
          {latest ? (
            <>
              <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
                <Metric label="Fair probability" value={prob(latest.fair_probability_mean)} />
                <Metric label="Interval" value={`${prob(latest.fair_probability_low)} – ${prob(latest.fair_probability_high)}`} />
                <Metric label="Market implied" value={prob(latest.market_implied_probability)} />
                <Metric
                  label="Model vs market"
                  value={signedCents(
                    Number(latest.fair_probability_mean) -
                      Number(latest.market_implied_probability ?? latest.fair_probability_mean),
                  )}
                />
                <Metric label="Confidence" value={pct(latest.model_confidence)} />
                <Metric label="Evidence quality" value={pct(latest.evidence_quality)} />
                <Metric label="Data freshness" value={humanizeSeconds(latest.data_freshness_seconds)} />
                <Metric label="Model version" value={latest.model_version} />
              </div>

              {!latest.has_independent_prior && (
                <div className="mt-3 rounded border border-warn/40 bg-warn/10 p-3 text-xs">
                  <strong>No independent prior.</strong> Every component contributing to
                  this estimate derives from this market&apos;s own price, so it carries no
                  information the market does not already have and cannot support a value
                  recommendation.
                </div>
              )}

              <p className="mt-3 text-xs text-ink-muted">
                {latest.explanation}
              </p>

              {latest.components?.length > 0 && (
                <div className="table-wrap mt-3">
                  <table className="w-full">
                    <thead className="border-b border-line">
                      <tr><th>Component</th><th>Probability</th><th>Weight</th><th>Confidence</th><th>Detail</th></tr>
                    </thead>
                    <tbody className="divide-y divide-line-subtle">
                      {latest.components.map((c: any, i: number) => (
                        <tr key={i} className={Number(c.weight) > 0 ? "" : "opacity-50"}>
                          <td>{c.name}</td>
                          <td className="num">{c.probability ? prob(c.probability) : "no opinion"}</td>
                          <td className="num">{pct(c.weight)}</td>
                          <td className="num">{pct(c.confidence)}</td>
                          <td className="max-w-md truncate text-xs text-ink-faint">{c.detail}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </>
          ) : (
            <p className="text-sm text-ink-faint">
              This market has not been scored yet. Run `make rank`.
            </p>
          )}
        </section>
      </div>

      {/* ---- price history ---- */}
      {d.price_history?.length > 1 && (
        <section className="panel mt-4 p-4">
          <h2 className="t-section-title mb-3">
            Price history vs model probability
          </h2>
          <PriceChart
            history={d.price_history}
            predictions={d.predictions.map((p) => ({
              t: p.created_at,
              mean: p.fair_probability_mean,
              low: p.fair_probability_low,
              high: p.fair_probability_high,
            }))}
          />
        </section>
      )}

      {/* ---- settlement rules ---- */}
      <section className="panel mt-4 p-4">
        <h2 className="t-section-title mb-3">Settlement</h2>
        <div className="grid gap-4 sm:grid-cols-2">
          <div>
            <div className="metric-label">Settlement source</div>
            <p className="mt-1 text-sm">{m.settlement_source || "—"}</p>
            <div className="metric-label mt-3">Normalized terms</div>
            <p className="num mt-1 text-xs">{m.settlement_rules_normalized || "—"}</p>
            <div className="metric-label mt-3">Resolution hash</div>
            <p className="num mt-1 text-xs break-all">{m.resolution_hash || "—"}</p>
            {d.rule && (
              <dl className="mt-3 grid grid-cols-2 gap-2 text-xs">
                <div><dt className="metric-label">Comparator</dt><dd className="num">{d.rule.comparator || "—"}</dd></div>
                <div><dt className="metric-label">Threshold</dt><dd className="num">{d.rule.threshold_value ?? "—"}</dd></div>
                <div><dt className="metric-label">Basis</dt><dd className="num">{d.rule.threshold_semantics || "—"}</dd></div>
                <div><dt className="metric-label">Cutoff (UTC)</dt><dd className="num">{localTime(d.rule.cutoff_time)}</dd></div>
              </dl>
            )}
          </div>
          <div>
            <div className="metric-label">Full rules</div>
            <p className="mt-1 max-h-64 overflow-y-auto whitespace-pre-wrap text-xs text-ink-muted">
              {m.settlement_rules_raw || m.description || "—"}
            </p>
          </div>
        </div>
        <div className="mt-3 grid grid-cols-2 gap-4 border-t border-line-subtle pt-3 sm:grid-cols-4">
          <Metric label="Opens" value={localTime(m.open_time)} />
          <Metric label="Market close" value={localTime(m.close_time)} />
          <Metric label="Expected resolution" value={localTime(m.expected_resolution_time)} />
          <Metric label="Actual settlement" value={localTime(m.actual_settlement_time)} />
        </div>
        {d.settlement && (
          <div className="mt-3 rounded bg-neutral-100 p-3 text-sm dark:bg-neutral-800">
            Settled <strong>{d.settlement.result}</strong> (YES payout{" "}
            {d.settlement.yes_payout}) at {localTime(d.settlement.settled_at)}
            {d.settlement.disputed && " — DISPUTED"}
          </div>
        )}
      </section>

      {/* ---- cross-platform matches ---- */}
      <section className="panel mt-4 p-4">
        <h2 className="t-section-title mb-1">Cross-platform matches</h2>
        <p className="mb-3 text-xs text-ink-faint">
          Only <em>identical</em> rule compatibility permits an executable arbitrage
          claim. Anything less is shown with the specific terms that differ.
        </p>
        {d.cross_platform_matches?.length ? (
          <div className="table-wrap">
            <table className="w-full">
              <thead className="border-b border-line">
                <tr><th>Counterpart</th><th>Venue</th><th>YES ask</th><th>Rules</th><th>Confidence</th><th>Polarity</th><th>Differences</th></tr>
              </thead>
              <tbody className="divide-y divide-line-subtle">
                {d.cross_platform_matches.map((mt: any, i: number) => (
                  <tr key={i}>
                    <td className="max-w-xs truncate">
                      <Link href={`/market/${mt.other_market_id}`} className="hover:underline">
                        {displayTitle(mt.other_title)}
                      </Link>
                    </td>
                    <td><PlatformChip platform={mt.other_platform} /></td>
                    <td className="num">{cents(mt.other_best_yes_ask)}</td>
                    <td>
                      <span className={`chip ${mt.rule_compatibility === "identical" ? "bg-edge/20 text-edge dark:bg-edge-dark/20 dark:text-edge-dark" : "bg-sunken text-ink-muted"}`}>
                        {mt.rule_compatibility}
                      </span>
                    </td>
                    <td className="num">{pct(mt.match_confidence)}</td>
                    <td>{mt.polarity_inverted ? "inverted" : "same"}</td>
                    <td className="max-w-sm truncate text-xs text-ink-faint">
                      {(mt.mismatch_reasons ?? []).join("; ") || "none"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="text-sm text-ink-faint">
            No counterpart market was matched on the other venue.
          </p>
        )}
      </section>

      {/* ---- evidence ---- */}
      <section className="panel mt-4 p-4">
        <h2 className="t-section-title mb-3">Evidence timeline</h2>
        {d.evidence?.length ? (
          <ul className="space-y-3">
            {d.evidence.map((e: any, i: number) => (
              <li key={i} className="border-l-2 border-line pl-3 dark:border-neutral-700">
                <div className="flex flex-wrap items-center gap-2">
                  <span className={`chip ${e.stance === "supports_yes" ? "bg-edge/15 text-edge" : e.stance === "supports_no" ? "bg-risk/15 text-risk" : "bg-neutral-100 text-neutral-600 dark:bg-neutral-800"}`}>
                    {e.stance.replace(/_/g, " ")}
                  </span>
                  <span className="text-xs text-ink-faint">
                    {e.source_name} · published {localTime(e.published_at)}
                    {e.is_novel ? "" : " · repeat coverage"}
                  </span>
                </div>
                <p className="mt-1 text-sm">{displayTitle(e.title)}</p>
                <p className="text-xs text-ink-muted">{e.summary}</p>
                {e.source_url && (
                  <a href={e.source_url} target="_blank" rel="noopener noreferrer"
                    className="text-xs underline">source</a>
                )}
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-sm text-ink-faint">
            No evidence gathered. The research agent is disabled by default
            (<code>RESEARCH_ENABLED=false</code>); with no API key it contributes
            nothing rather than inventing sources.
          </p>
        )}
      </section>

      {/* ---- recommendation history ---- */}
      {d.recommendations?.length > 0 && (
        <section className="panel mt-4 p-4">
          <h2 className="t-section-title mb-3">Recommendation history</h2>
          <div className="table-wrap">
            <table className="w-full">
              <thead className="border-b border-line">
                <tr><th>Published</th><th>Horizon</th><th>Rank</th><th>Side</th><th>Entry</th><th>Current</th><th>Net EV then</th><th>State</th><th>Result</th><th>Realised</th></tr>
              </thead>
              <tbody className="divide-y divide-line-subtle">
                {d.recommendations.map((r: any, i: number) => (
                  <tr key={i}>
                    <td>{localTime(r.created_at)}</td>
                    <td>{r.horizon}</td>
                    <td className="num">#{r.rank}</td>
                    <td><SideChip side={r.side} /></td>
                    <td className="num">{cents(r.entry_price)}</td>
                    <td className="num">{cents(r.current_price)}</td>
                    <td className="num">{signedCents(r.net_ev_per_contract)}</td>
                    <td><StateChip state={r.state} /></td>
                    <td>{r.settlement_result ?? "—"}</td>
                    <td className="num">{signedCents(r.realized_profit_per_contract)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      <p className="mt-6 text-xs text-ink-faint">{res.disclaimer}</p>
    </div>
  );
}

/**
 * Market price, independent estimate and decision figure, side by side and
 * labelled for what each one is.
 *
 * The three are visually separated because conflating them is the specific error
 * this product exists to avoid: the market's own price is evidence about the
 * market, not evidence about the world, and the number that drives a decision is
 * neither of the other two.
 */
function ProbabilityTriad({
  marketImplied,
  independent,
  decision,
  high,
  confidence,
  hasIndependentPrior,
  scored,
}: {
  marketImplied: string | number | null;
  independent: string | number | null;
  decision: string | number | null;
  high: string | number | null;
  confidence: string | number | null;
  hasIndependentPrior: boolean;
  scored: boolean;
}) {
  return (
    <section className="panel mb-4 p-4" aria-labelledby="probability">
      <h2 id="probability" className="t-section-title">
        Probability
      </h2>
      <div className="mt-3 grid gap-4 sm:grid-cols-3">
        <div className="border-l-2 border-l-line pl-3">
          <div className="t-label">Market-implied</div>
          <div className="num mt-0.5 text-2xl font-semibold text-ink">
            {prob(marketImplied)}
          </div>
          <p className="t-meta mt-1">
            What the venue is charging. Evidence about the market, not about the
            world.
          </p>
        </div>

        <div className="border-l-2 border-l-info pl-3">
          <div className="t-label">Independent estimate</div>
          <div className="num mt-0.5 text-2xl font-semibold text-info">
            {scored ? prob(independent) : "—"}
          </div>
          <p className="t-meta mt-1">
            {!scored ? (
              "Not scored yet — no independent estimate exists for this contract."
            ) : !hasIndependentPrior ? (
              <span className="text-warn">
                Derived entirely from this market&apos;s own price, so it carries no
                information the market does not already have.
              </span>
            ) : (
              <>
                Ensemble mean. Interval {prob(decision)}–{prob(high)}, confidence{" "}
                {pct(confidence)}.
              </>
            )}
          </p>
        </div>

        <div className="border-l-2 border-l-accent pl-3">
          <div className="t-label">Decision-adjusted</div>
          <div className="num mt-0.5 text-2xl font-semibold text-ink">
            {scored && hasIndependentPrior ? prob(decision) : "—"}
          </div>
          <p className="t-meta mt-1">
            The conservative bound. A position must be profitable here, not merely
            at the central estimate — this is the figure the ranking decides on.
          </p>
        </div>
      </div>
    </section>
  );
}

function BookSide({ title, levels }: { title: string; levels: Array<{ price: string; size: string }> }) {
  return (
    <div>
      <div className="metric-label mb-1">{title}</div>
      {/* table-wrap: narrow today, but the guard holds without exceptions. */}
      <div className="table-wrap">
        <table className="w-full text-xs">
          <tbody>
            {(levels ?? []).slice(0, 8).map((l, i) => (
              <tr key={i}>
                <td className="num py-0.5">{cents(l.price)}</td>
                <td className="num py-0.5 text-right text-ink-faint">
                  {Number(l.size).toFixed(0)}
                </td>
              </tr>
            ))}
            {!levels?.length && (
              <tr><td className="py-0.5 text-ink-faint">no offers</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
