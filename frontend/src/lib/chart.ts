import {
  BarController,
  BarElement,
  CategoryScale,
  Chart,
  Legend,
  LinearScale,
  Tooltip,
  type ChartConfiguration,
  type TooltipItem,
} from "chart.js";
import { onBeforeUnmount, onMounted, watch, type Ref } from "vue";
import type { HistoryResponse } from "../api/client";
import { dashboard } from "./dashboard";
import { bucketLabel, flagLabel, formatTokens } from "./format";

// Bar-only registration: nothing that can render a pie, line, or time axis is
// bundled, so the chart cannot grow a second visual grammar by accident.
Chart.register(BarController, BarElement, CategoryScale, LinearScale, Legend, Tooltip);

const DATASETS = [
  { key: "computed_input", label: "Computed input", token: "--series-input" },
  { key: "generated_output", label: "Generated output", token: "--series-output" },
] as const;

function tokenColor(token: string, fallback: string): string {
  const value = getComputedStyle(document.documentElement).getPropertyValue(token).trim();
  return value || fallback;
}

function bucketAt(history: HistoryResponse | null, index: number | undefined) {
  if (!history || index === undefined) return undefined;
  return history.buckets[index];
}

/** "2026-10-25 17:00 – 2026-10-25 18:00 UTC" — the bucket's own UTC bounds. */
function bounds(bucket: { started_at: string; ended_at: string }): string {
  const stamp = (value: string): string => new Date(value).toISOString().slice(0, 16).replace("T", " ");
  return `${stamp(bucket.started_at)} – ${stamp(bucket.ended_at)} UTC`;
}

/** One stacked bar chart over the current history payload; null buckets stay absent. */
export function useTrendChart(
  canvasRef: Ref<HTMLCanvasElement | null>,
  historyRef: Ref<HistoryResponse | null>,
): () => void {
  let chart: Chart<"bar", (number | null)[], unknown> | null = null;

  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

  function build(): Chart<"bar", (number | null)[], unknown> | null {
    const canvas = canvasRef.value;
    if (!canvas) return null;
    const history = historyRef.value;
    const labels = history ? history.buckets.map(bucketLabel) : [];
    // A null total is a null data point: Chart.js omits the bar. Zero would be a lie.
    const dataFor = (key: "computed_input" | "generated_output") =>
      history
        ? history.buckets.map((bucket) => {
            const tokens = bucket.counters[key].tokens;
            return tokens === null || tokens === undefined ? null : Number(tokens);
          })
        : [];
    const config: ChartConfiguration<"bar", (number | null)[], unknown> = {
      type: "bar",
      data: {
        labels,
        datasets: DATASETS.map((set) => ({
          label: set.label,
          data: dataFor(set.key),
          backgroundColor: tokenColor(set.token, set.key === "computed_input" ? "#2563eb" : "#b45309"),
          borderRadius: 2,
          borderSkipped: false,
        })),
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: reducedMotion.matches ? false : undefined,
        scales: {
          x: { stacked: true, ticks: { autoSkip: true, maxRotation: 0 } },
          y: { stacked: true, beginAtZero: true },
        },
        plugins: {
          legend: { position: "bottom" },
          tooltip: {
            callbacks: {
              title: (items: TooltipItem<"bar">[]) => {
                const bucket = bucketAt(historyRef.value, items[0]?.dataIndex);
                return bucket ? bounds(bucket) : "";
              },
              label: (item: TooltipItem<"bar">) => {
                const bucket = bucketAt(historyRef.value, item.dataIndex);
                const key = DATASETS.find((set) => set.label === item.dataset.label)?.key;
                const tokens = bucket && key ? bucket.counters[key].tokens : null;
                return `${item.dataset.label}: ${formatTokens(tokens)}`;
              },
              footer: (items: TooltipItem<"bar">[]) => {
                const bucket = bucketAt(historyRef.value, items[0]?.dataIndex);
                if (!bucket) return [];
                const lines = bucket.quality_flags.map(flagLabel);
                if (bucket.partial) lines.push("partial");
                if (bucket.incomplete) lines.push("incomplete");
                const empty =
                  bucket.counters.computed_input.tokens == null &&
                  bucket.counters.generated_output.tokens == null;
                if (empty) lines.push("no data");
                return lines;
              },
            },
          },
        },
      },
    };
    return new Chart(canvas, config);
  }

  function apply(): void {
    const history = historyRef.value;
    if (!chart || !history) return;
    chart.data.labels = history.buckets.map(bucketLabel);
    chart.data.datasets.forEach((dataset, index) => {
      const key = DATASETS[index].key;
      dataset.data = history.buckets.map((bucket) => {
        const tokens = bucket.counters[key].tokens;
        return tokens === null || tokens === undefined ? null : Number(tokens);
      });
    });
    chart.update("none");
  }

  function destroy(): void {
    if (!chart) return;
    chart.destroy();
    chart = null;
  }

  const stop = watch(historyRef, () => {
    if (chart) apply();
    else chart = build();
  });

  onMounted(() => {
    if (!chart) chart = build();
  });
  chart = build();

  const cleanup = (): void => {
    stop();
    destroy();
    dashboard.cleanups.delete(cleanup);
  };
  dashboard.cleanups.add(cleanup);
  onBeforeUnmount(cleanup);
  return cleanup;
}
