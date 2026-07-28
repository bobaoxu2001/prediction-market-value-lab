import Link from "next/link";
import { apiGet, qs, type DataMode } from "@/lib/api";
import { ageLabel, ageRelativeToSnapshot, cents, compactUsd, displayTitle, localTime, pct, prob, relativeTime, relativeToSnapshot, signedCents } from "@/lib/format";
import { ApiDown, DemoBanner, EmptyState, Metric, PageHeader, PlatformChip, RiskFlags, SideChip, StateChip, VenueAvailability } from "@/components/ui";
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
            <span className="chip bg-neutral-100 text-neutral-600 dark:bg-neutral-800 dark:text-neutral-400">
              {m.category}
            </span>
            <span className="chip bg-neutral-100 text-neutral-600 dark:bg-neutral-800 dark:text-neutral-400">
              {m.status}
            </span>
          </div>
        }
      />
      <DemoBanner notice={res.demo_notice} />

      {m.venue_availability ? (
        <div className="card mb-4 p-4">
          <h2 className="text-sm font-semibold">Where this contract can be traded</h2>
          <div className="mt-2">
            <VenueAvailability venues={m.venue_availability} />
          </div>
          <p className="mt-2 text-xs text-neutral-500">
            Exchange availability is asserted only for venues read directly. Broker
            availability is never inferred from an exchange listing: brokers resell a
            subset that changes without notice and is gated by jurisdiction and
            account type, and no discovery source for them is wired up here.
          </p>
        </div>
      ) : null}

      {/* ---- quotes ---- */}
      <section className="card mb-4 p-4">
        <h2 className="mb-3 text-sm font-semibold">Quotes</h2>
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
        <div className="mt-3 grid grid-cols-2 gap-4 border-t border-neutral-100 pt-3 dark:border-neutral-800 sm:grid-cols-4 lg:grid-cols-6">
          <Metric label="Tick size" value={m.tick_size} />
          <Metric label="Taker fee rate" value={m.fee_rate} hint={`Fee model: ${m.fee_type}`} />
          <Metric label="Min order" value={m.min_order_size} />
          <Metric label="Open interest" value={compactUsd(m.open_interest)} />
          <Metric label="Quote age" value={ageRelativeToSnapshot(m.quote_observed_at, snapshotAt)} />
          <Metric label="Resolves" value={relativeToSnapshot(m.expected_resolution_time, snapshotAt)} hint={localTime(m.expected_resolution_time)} />
        </div>
      </section>

      <div className="grid gap-4 lg:grid-cols-3">
        {/* ---- orderbook ---- */}
        <section className="card p-4">
          <h2 className="mb-1 text-sm font-semibold">Order book</h2>
          <p className="mb-3 text-xs text-neutral-500">
            Observed {localTime(d.orderbook?.observed_at)}
          </p>
          {d.orderbook?.yes_asks?.length || d.orderbook?.no_asks?.length ? (
            <div className="grid grid-cols-2 gap-4">
              <BookSide title="YES asks" levels={d.orderbook.yes_asks} />
              <BookSide title="NO asks" levels={d.orderbook.no_asks} />
            </div>
          ) : (
            <p className="text-sm text-neutral-500">
              No order book captured. Only markets that clear the volume and horizon
              filters receive an orderbook fetch.
            </p>
          )}
        </section>

        {/* ---- model ---- */}
        <section className="card p-4 lg:col-span-2">
          <h2 className="mb-3 text-sm font-semibold">Model estimate</h2>
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
                <Metric label="Data freshness" value={latest.data_freshness_seconds != null ? `${Math.round(latest.data_freshness_seconds / 60)}m` : "—"} />
                <Metric label="Model version" value={latest.model_version} />
              </div>

              {!latest.has_independent_prior && (
                <div className="mt-3 rounded border border-warn/40 bg-warn/10 p-3 text-xs dark:border-warn-dark/40 dark:bg-warn-dark/10">
                  <strong>No independent prior.</strong> Every component contributing to
                  this estimate derives from this market&apos;s own price, so it carries no
                  information the market does not already have and cannot support a value
                  recommendation.
                </div>
              )}

              <p className="mt-3 text-xs text-neutral-600 dark:text-neutral-400">
                {latest.explanation}
              </p>

              {latest.components?.length > 0 && (
                <div className="table-wrap mt-3">
                  <table className="w-full">
                    <thead className="border-b border-neutral-200 dark:border-neutral-800">
                      <tr><th>Component</th><th>Probability</th><th>Weight</th><th>Confidence</th><th>Detail</th></tr>
                    </thead>
                    <tbody className="divide-y divide-neutral-100 dark:divide-neutral-800">
                      {latest.components.map((c: any, i: number) => (
                        <tr key={i} className={Number(c.weight) > 0 ? "" : "opacity-50"}>
                          <td>{c.name}</td>
                          <td className="num">{c.probability ? prob(c.probability) : "no opinion"}</td>
                          <td className="num">{pct(c.weight)}</td>
                          <td className="num">{pct(c.confidence)}</td>
                          <td className="max-w-md truncate text-xs text-neutral-500">{c.detail}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </>
          ) : (
            <p className="text-sm text-neutral-500">
              This market has not been scored yet. Run `make rank`.
            </p>
          )}
        </section>
      </div>

      {/* ---- price history ---- */}
      {d.price_history?.length > 1 && (
        <section className="card mt-4 p-4">
          <h2 className="mb-3 text-sm font-semibold">
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
      <section className="card mt-4 p-4">
        <h2 className="mb-3 text-sm font-semibold">Settlement</h2>
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
            <p className="mt-1 max-h-64 overflow-y-auto whitespace-pre-wrap text-xs text-neutral-600 dark:text-neutral-400">
              {m.settlement_rules_raw || m.description || "—"}
            </p>
          </div>
        </div>
        <div className="mt-3 grid grid-cols-2 gap-4 border-t border-neutral-100 pt-3 dark:border-neutral-800 sm:grid-cols-4">
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
      <section className="card mt-4 p-4">
        <h2 className="mb-1 text-sm font-semibold">Cross-platform matches</h2>
        <p className="mb-3 text-xs text-neutral-500">
          Only <em>identical</em> rule compatibility permits an executable arbitrage
          claim. Anything less is shown with the specific terms that differ.
        </p>
        {d.cross_platform_matches?.length ? (
          <div className="table-wrap">
            <table className="w-full">
              <thead className="border-b border-neutral-200 dark:border-neutral-800">
                <tr><th>Counterpart</th><th>Venue</th><th>YES ask</th><th>Rules</th><th>Confidence</th><th>Polarity</th><th>Differences</th></tr>
              </thead>
              <tbody className="divide-y divide-neutral-100 dark:divide-neutral-800">
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
                      <span className={`chip ${mt.rule_compatibility === "identical" ? "bg-edge/20 text-edge dark:bg-edge-dark/20 dark:text-edge-dark" : "bg-neutral-100 text-neutral-600 dark:bg-neutral-800 dark:text-neutral-400"}`}>
                        {mt.rule_compatibility}
                      </span>
                    </td>
                    <td className="num">{pct(mt.match_confidence)}</td>
                    <td>{mt.polarity_inverted ? "inverted" : "same"}</td>
                    <td className="max-w-sm truncate text-xs text-neutral-500">
                      {(mt.mismatch_reasons ?? []).join("; ") || "none"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="text-sm text-neutral-500">
            No counterpart market was matched on the other venue.
          </p>
        )}
      </section>

      {/* ---- evidence ---- */}
      <section className="card mt-4 p-4">
        <h2 className="mb-3 text-sm font-semibold">Evidence timeline</h2>
        {d.evidence?.length ? (
          <ul className="space-y-3">
            {d.evidence.map((e: any, i: number) => (
              <li key={i} className="border-l-2 border-neutral-200 pl-3 dark:border-neutral-700">
                <div className="flex flex-wrap items-center gap-2">
                  <span className={`chip ${e.stance === "supports_yes" ? "bg-edge/15 text-edge dark:bg-edge-dark/15 dark:text-edge-dark" : e.stance === "supports_no" ? "bg-risk/15 text-risk dark:bg-risk-dark/15 dark:text-risk-dark" : "bg-neutral-100 text-neutral-600 dark:bg-neutral-800"}`}>
                    {e.stance.replace(/_/g, " ")}
                  </span>
                  <span className="text-xs text-neutral-500">
                    {e.source_name} · published {localTime(e.published_at)}
                    {e.is_novel ? "" : " · repeat coverage"}
                  </span>
                </div>
                <p className="mt-1 text-sm">{displayTitle(e.title)}</p>
                <p className="text-xs text-neutral-600 dark:text-neutral-400">{e.summary}</p>
                {e.source_url && (
                  <a href={e.source_url} target="_blank" rel="noopener noreferrer"
                    className="text-xs underline">source</a>
                )}
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-sm text-neutral-500">
            No evidence gathered. The research agent is disabled by default
            (<code>RESEARCH_ENABLED=false</code>); with no API key it contributes
            nothing rather than inventing sources.
          </p>
        )}
      </section>

      {/* ---- recommendation history ---- */}
      {d.recommendations?.length > 0 && (
        <section className="card mt-4 p-4">
          <h2 className="mb-3 text-sm font-semibold">Recommendation history</h2>
          <div className="table-wrap">
            <table className="w-full">
              <thead className="border-b border-neutral-200 dark:border-neutral-800">
                <tr><th>Published</th><th>Horizon</th><th>Rank</th><th>Side</th><th>Entry</th><th>Current</th><th>Net EV then</th><th>State</th><th>Result</th><th>Realised</th></tr>
              </thead>
              <tbody className="divide-y divide-neutral-100 dark:divide-neutral-800">
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

      <p className="mt-6 text-xs text-neutral-500 dark:text-neutral-400">{res.disclaimer}</p>
    </div>
  );
}

function BookSide({ title, levels }: { title: string; levels: Array<{ price: string; size: string }> }) {
  return (
    <div>
      <div className="metric-label mb-1">{title}</div>
      <table className="w-full text-xs">
        <tbody>
          {(levels ?? []).slice(0, 8).map((l, i) => (
            <tr key={i}>
              <td className="num py-0.5">{cents(l.price)}</td>
              <td className="num py-0.5 text-right text-neutral-500">
                {Number(l.size).toFixed(0)}
              </td>
            </tr>
          ))}
          {!levels?.length && (
            <tr><td className="py-0.5 text-neutral-500">no offers</td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
