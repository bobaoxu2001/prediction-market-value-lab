"use client";

import { useEffect, useState, useSyncExternalStore } from "react";
import Link from "next/link";

import { API_BASE, qs, type CostDetail, type Envelope } from "@/lib/api";
import { cents, displayTitle, num, pct } from "@/lib/format";
import {
  clearWatchlist,
  getWatchlistServerSnapshot,
  getWatchlistSnapshot,
  removeWatch,
  subscribeToWatchlist,
  watchKey,
  type WatchedContract,
} from "@/lib/watchlist";

/**
 * The watchlist, re-priced on load.
 *
 * A saved list of titles would be a bookmark folder. What makes this worth
 * returning to is that every entry is re-costed against the current snapshot when
 * the page opens, so a reader sees whether the premium on something they were
 * considering has moved.
 *
 * Client-rendered because the list lives in `localStorage` and the server cannot
 * see it. Costs are fetched straight from the read-only research API, the same
 * one the server components use.
 */

/** `false` once a fetch has failed or returned an unpriceable contract. */
type Priced = CostDetail | false;

export default function WatchlistPage() {
  const items = useSyncExternalStore(
    subscribeToWatchlist,
    getWatchlistSnapshot,
    getWatchlistServerSnapshot,
  );
  // Keyed by contract rather than parallel to `items`, so removing one row does
  // not misalign every price after it.
  const [prices, setPrices] = useState<Record<string, Priced>>({});
  const [hydrated, setHydrated] = useState(false);

  const signature = items
    .map((item) => `${watchKey(item.marketId, item.side)}@${item.size}`)
    .join(",");

  useEffect(() => {
    let cancelled = false;

    async function priceAll() {
      const results = await Promise.all(
        items.map(async (entry): Promise<[string, Priced]> => {
          const key = watchKey(entry.marketId, entry.side);
          try {
            const res = await fetch(
              `${API_BASE}/cost/${entry.marketId}${qs({
                size: entry.size,
                side: entry.side,
              })}`,
              { cache: "no-store" },
            );
            if (!res.ok) return [key, false];
            const body = (await res.json()) as Envelope<CostDetail>;
            return [key, body.data ?? false];
          } catch {
            // A failed row must not blank the others, and must never render as
            // "no premium" — it gets an explicit unavailable state below.
            return [key, false];
          }
        }),
      );
      if (cancelled) return;
      setPrices(Object.fromEntries(results));
      setHydrated(true);
    }

    void priceAll();
    return () => {
      cancelled = true;
    };
    // `signature` covers every field the request depends on; `items` itself is a
    // fresh array whenever the store is invalidated.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signature]);

  return (
    <>
      <div className="mb-5">
        <h1 className="t-page-title">Watchlist</h1>
        <p className="t-prose mt-2">
          Contracts you saved, re-costed against the current snapshot each time
          this page opens. Saving something does not subscribe you to anything and
          sends nothing anywhere.
        </p>
        <p className="t-meta mt-2">
          Stored in this browser only. It will not appear on another device or in a
          private window, and clearing site data removes it. There is no account to
          attach it to, and adding one would mean this site holding personal data
          it currently does not.
        </p>
      </div>

      {items.length === 0 ? (
        <div className="panel p-8 text-center">
          <p className="t-sub-title">Nothing saved yet</p>
          <p className="mx-auto mt-2 max-w-xl text-sm text-ink-muted">
            Open any contract on the cost surface and press Watch. The saved size
            and side come with it, so the premium you see here is the one you were
            actually looking at.
          </p>
          <Link href="/cost" className="btn-quiet mt-4 inline-flex">
            Browse contracts by premium
          </Link>
        </div>
      ) : (
        <>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th scope="col">Contract</th>
                  <th scope="col" className="hidden lg:table-cell">Side</th>
                  <th scope="col" className="num hidden lg:table-cell">
                    Size
                  </th>
                  <th scope="col" className="num hidden md:table-cell">
                    Quoted
                  </th>
                  <th scope="col" className="num">
                    Cost estimate
                  </th>
                  <th scope="col" className="num">
                    Est. premium
                  </th>
                  <th scope="col" className="num">
                    Est. break-even
                  </th>
                  <th scope="col">
                    <span className="sr-only">Remove</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {items.map((entry) => {
                  const key = watchKey(entry.marketId, entry.side);
                  return (
                    <Row
                      key={key}
                      entry={entry}
                      cost={hydrated ? (prices[key] ?? false) : undefined}
                      onRemove={() => removeWatch(entry.marketId, entry.side)}
                    />
                  );
                })}
              </tbody>
            </table>
          </div>

          <button
            type="button"
            className="btn-quiet mt-4"
            onClick={() => clearWatchlist()}
          >
            Clear watchlist
          </button>
        </>
      )}
    </>
  );
}

function Row({
  entry,
  cost,
  onRemove,
}: {
  entry: WatchedContract;
  /** `undefined` while pricing is in flight. */
  cost: Priced | undefined;
  onRemove: () => void;
}) {
  const priced = cost && cost.priced ? cost.requested : null;
  const ratio = priced ? num(priced.measured_premium_ratio) : null;
  const tone =
    ratio === null ? "" : ratio >= 0.5 ? "text-risk" : ratio >= 0.1 ? "text-warn" : "";

  return (
    <tr>
      <th scope="row" className="cell-title col-sticky">
        <Link
          href={`/cost/${entry.marketId}${qs({ size: entry.size, side: entry.side })}`}
          className="hover:underline"
        >
          {displayTitle(entry.title)}
        </Link>
      </th>
      <td className="hidden lg:table-cell">{entry.side.toUpperCase()}</td>
      <td className="num hidden lg:table-cell">{entry.size}</td>
      {cost === undefined || priced === null ? (
        // One cell per column rather than a colSpan.
        //
        // A `colSpan={4}` placeholder counts four cells at every width, but the
        // Quoted column is hidden below `md` — so on a phone the row claimed one
        // more cell than the header had and pushed Remove out of alignment.
        // Matching the header's own breakpoints keeps the row aligned at every
        // width without needing a responsive colSpan, which HTML has no way to
        // express.
        <>
          <td className="num hidden md:table-cell text-ink-faint">
            {cost === undefined ? "…" : "—"}
          </td>
          <td className="num text-ink-faint" colSpan={3}>
            {cost === undefined ? "pricing…" : "could not be priced"}
          </td>
        </>
      ) : (
        <>
          <td className="num hidden md:table-cell">{cents(priced.nominal_price)}</td>
          <td className="num">{cents(priced.measured_cost)}</td>
          <td className={`num ${tone}`}>
            +{cents(priced.measured_premium)}
            {ratio !== null && <span className="t-meta ml-1">({pct(ratio, 0)})</span>}
          </td>
          <td className="num">
            {priced.breakeven_probability === null
              ? "impossible"
              : pct(priced.breakeven_probability)}
          </td>
        </>
      )}
      <td>
        <button
          type="button"
          className="text-xs text-ink-faint underline underline-offset-2 hover:text-ink"
          onClick={onRemove}
        >
          Remove
        </button>
      </td>
    </tr>
  );
}
