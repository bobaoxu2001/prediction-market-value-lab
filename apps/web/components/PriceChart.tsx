"use client";

import {
  CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";

interface HistoryPoint {
  observed_at: string;
  yes_bid: string | null;
  yes_ask: string | null;
  mid: string | null;
}
interface PredictionPoint {
  t: string;
  mean: string;
  low: string;
  high: string;
}

/**
 * Market price against the model's fair probability.
 *
 * Both series are plotted on the same 0–100% axis because a binary contract's price
 * *is* an implied probability; putting them on one axis is what makes the gap
 * between model and market directly readable.
 */
export function PriceChart({
  history,
  predictions,
}: {
  history: HistoryPoint[];
  predictions: PredictionPoint[];
}) {
  const byTime = new Map<number, Record<string, number | null>>();

  for (const point of history) {
    const t = new Date(point.observed_at).getTime();
    if (!Number.isFinite(t)) continue;
    byTime.set(t, {
      ...(byTime.get(t) ?? {}),
      t,
      ask: point.yes_ask ? Number(point.yes_ask) * 100 : null,
      mid: point.mid ? Number(point.mid) * 100 : null,
    });
  }
  for (const point of predictions) {
    const t = new Date(point.t).getTime();
    if (!Number.isFinite(t)) continue;
    byTime.set(t, {
      ...(byTime.get(t) ?? { t }),
      t,
      fair: Number(point.mean) * 100,
      fairLow: Number(point.low) * 100,
      fairHigh: Number(point.high) * 100,
    });
  }

  const data = [...byTime.values()].sort(
    (a, b) => (a.t as number) - (b.t as number),
  );
  if (data.length < 2) {
    return <p className="text-sm text-neutral-500">Not enough history to plot yet.</p>;
  }

  return (
    <div className="h-72 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 5, right: 8, left: -18, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" className="stroke-neutral-200 dark:stroke-neutral-800" />
          <XAxis
            dataKey="t" type="number" domain={["dataMin", "dataMax"]} scale="time"
            tick={{ fontSize: 11 }}
            tickFormatter={(v) =>
              new Date(v).toLocaleDateString(undefined, { month: "short", day: "numeric" })
            }
          />
          <YAxis domain={[0, 100]} tick={{ fontSize: 11 }} tickFormatter={(v) => `${v}%`} />
          <Tooltip
            contentStyle={{ fontSize: 12, borderRadius: 6 }}
            labelFormatter={(v) => new Date(Number(v)).toLocaleString()}
            formatter={(value: number | string, name: string) => [
              typeof value === "number" ? `${value.toFixed(1)}%` : value, name,
            ]}
          />
          <Legend wrapperStyle={{ fontSize: 12 }} />
          <Line type="monotone" dataKey="ask" name="YES ask" stroke="#2563eb" dot={false} connectNulls strokeWidth={1.5} />
          <Line type="monotone" dataKey="mid" name="Market mid" stroke="#94a3b8" dot={false} connectNulls strokeWidth={1} />
          <Line type="monotone" dataKey="fair" name="Model fair" stroke="#0f9d58" dot={false} connectNulls strokeWidth={2} />
          <Line type="monotone" dataKey="fairLow" name="Conservative bound" stroke="#0f9d58" dot={false} connectNulls strokeDasharray="4 3" strokeWidth={1} />
          <Line type="monotone" dataKey="fairHigh" name="Upper bound" stroke="#0f9d58" dot={false} connectNulls strokeDasharray="4 3" strokeWidth={1} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
