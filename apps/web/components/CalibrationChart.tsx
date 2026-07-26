"use client";

import {
  CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Scatter,
  ComposedChart, Tooltip, XAxis, YAxis, ZAxis,
} from "recharts";

export interface CalibrationBin {
  bin_lower: number;
  bin_upper: number;
  n: number;
  mean_predicted: number;
  observed_frequency: number;
}

/**
 * Reliability diagram. The diagonal is perfect calibration; points below it mean the
 * model was overconfident in that band, points above mean underconfident. Bin sizes
 * are shown because a bin holding three observations says nothing.
 */
export function CalibrationChart({
  model,
  market,
}: {
  model: CalibrationBin[];
  market?: CalibrationBin[];
}) {
  if (!model?.length) {
    return <p className="text-sm text-neutral-500">No settled predictions to calibrate against yet.</p>;
  }

  const data = model.map((bin) => {
    const counterpart = market?.find(
      (m) => Math.abs(m.mean_predicted - bin.mean_predicted) < 0.06,
    );
    return {
      predicted: Number((bin.mean_predicted * 100).toFixed(1)),
      observed: Number((bin.observed_frequency * 100).toFixed(1)),
      perfect: Number((bin.mean_predicted * 100).toFixed(1)),
      marketObserved: counterpart
        ? Number((counterpart.observed_frequency * 100).toFixed(1))
        : null,
      n: bin.n,
    };
  });

  return (
    <div className="h-80 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart data={data} margin={{ top: 5, right: 8, left: -18, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" className="stroke-neutral-200 dark:stroke-neutral-800" />
          <XAxis
            dataKey="predicted" type="number" domain={[0, 100]}
            tick={{ fontSize: 11 }} tickFormatter={(v) => `${v}%`}
            label={{ value: "Predicted probability", position: "insideBottom", offset: -2, fontSize: 11 }}
          />
          <YAxis domain={[0, 100]} tick={{ fontSize: 11 }} tickFormatter={(v) => `${v}%`} />
          <Tooltip
            contentStyle={{ fontSize: 12, borderRadius: 6 }}
            formatter={(value: number | string, name: string, item: any) =>
              name === "Observed frequency"
                ? [`${value}% (n=${item?.payload?.n ?? "?"})`, name]
                : [`${value}%`, name]
            }
          />
          <Legend wrapperStyle={{ fontSize: 12 }} />
          <Line type="linear" dataKey="perfect" name="Perfect calibration" stroke="#94a3b8" strokeDasharray="5 4" dot={false} />
          <Line type="monotone" dataKey="observed" name="Observed frequency" stroke="#0f9d58" strokeWidth={2} dot={{ r: 3 }} />
          {market?.length ? (
            <Line type="monotone" dataKey="marketObserved" name="Market price as forecast" stroke="#2563eb" strokeWidth={1.5} strokeDasharray="3 3" dot={false} connectNulls />
          ) : null}
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
}
