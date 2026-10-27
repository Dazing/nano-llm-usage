import {
  BarController,
  BarElement,
  CategoryScale,
  Chart,
  LinearScale,
  Tooltip,
  type ChartConfiguration,
  type ScriptableContext,
  type TooltipItem,
} from "chart.js";
import { onBeforeUnmount, onMounted, watch, type Ref } from "vue";
import type { HistoryResponse } from "../api/client";
import { dashboard } from "./dashboard";
import { bucketLabel, flagLabel, formatCompact, formatTokens } from "./format";

// Bar-only registration: nothing that can render a pie, line, or time axis is
// bundled, so the chart cannot grow a second visual grammar by accident.
Chart.register(BarController, BarElement, CategoryScale, LinearScale, Tooltip);

const DATASETS = [
  { key: "computed_input", label: "Computed input", token: "--series-input" },
  { key: "generated_output", label: "Generated output", token: "--series-output" },
] as const;

/** Fallbacks match the dark palette in tokens.css; the tokens win when present. */
const FALLBACK = { "--series-input": "#7aa2f7", "--series-output": "#ff9e64" } as const;

function tokenColor(token: keyof typeof FALLBACK): string {
  const value = getComputedStyle(document.documentElement).getPropertyValue(token).trim();
  return value || FALLBACK[token];
}

function rawToken(token: string, fallback: string): string {
  const value = getComputedStyle(document.documentElement).getPropertyValue(token).trim();
  return value || fallback;
}

/** `#rrggbb` (the token palette) to rgba with an alpha, for a pattern's ground. */
function withAlpha(hex: string, alpha: number): string {
  const value = hex.replace("#", "");
  const full = value.length === 3 ? value.split("").map((c) => c + c).join("") : value;
  const int = Number.parseInt(full, 16);
  return `rgba(${(int >> 16) & 255}, ${(int >> 8) & 255}, ${int & 255}, ${alpha})`;
}

/** Diagonal hatching: an incomplete bucket is marked by shape as well as colour. */
function hatchPattern(context: CanvasRenderingContext2D, colour: string): CanvasPattern | null {
  const tile = document.createElement("canvas");
  tile.width = 8;
  tile.height = 8;
  const g = tile.getContext("2d");
  if (!g) return null;
  g.fillStyle = withAlpha(colour, 0.22);
  g.fillRect(0, 0, 8, 8);
  g.strokeStyle = withAlpha(colour, 0.9);
  g.lineWidth = 2;
  g.beginPath();
  g.moveTo(-2, 6);
  g.lineTo(6, -2);
  g.moveTo(2, 10);
  g.lineTo(10, 2);
  g.stroke();
  return context.createPattern(tile, "repeat");
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

/**
 * One stacked bar chart over the current history payload; null buckets stay absent.
 *
 * The tooltip keeps every value the previous chart showed (bucket bounds, both
 * series, quality lines); per-bucket cost is not drawn — the cost lives in the
 * cost well and the trend-cost line (plan D7).
 */
export function useTrendChart(
  canvasRef: Ref<HTMLCanvasElement | null>,
  historyRef: Ref<HistoryResponse | null>,
): { cleanup: () => void; resize: () => void } {
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
    const grid = {
      color: withAlpha(rawToken("--line", "#565f89"), 0.28),
      dash: [3, 4] as number[],
    };
    const ticks = {
      color: rawToken("--fg-muted", "#8892b8"),
      font: { family: rawToken("--font-mono", "ui-monospace, monospace"), size: 10 },
    };
    const config: ChartConfiguration<"bar", (number | null)[], unknown> = {
      type: "bar",
      data: {
        labels,
        datasets: DATASETS.map((set) => ({
          label: set.label,
          data: dataFor(set.key),
          backgroundColor: (context: ScriptableContext<"bar">) => {
            const colour = tokenColor(set.token);
            const bucket = bucketAt(historyRef.value, context.dataIndex);
            return bucket?.incomplete ? hatchPattern(context.chart.ctx, colour) ?? colour : colour;
          },
          // A partial (unobserved intervals) bucket gets a warning edge; an
          // incomplete one is already hatched.
          borderColor: rawToken("--warning", "#e0af68"),
          borderWidth: (context: ScriptableContext<"bar">) => {
            const bucket = bucketAt(historyRef.value, context.dataIndex);
            return bucket?.partial && !bucket.incomplete ? 1 : 0;
          },
          borderRadius: 2,
          borderSkipped: false,
        })),
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: reducedMotion.matches ? false : undefined,
        scales: {
          x: {
            stacked: true,
            grid: { display: false },
            border: { color: grid.color },
            ticks: { ...ticks, autoSkip: true, maxRotation: 0 },
          },
          y: {
            stacked: true,
            beginAtZero: true,
            grid,
            border: { display: false },
            ticks: { ...ticks, maxTicksLimit: 6, callback: (value) => formatCompact(Number(value)) },
          },
        },
        plugins: {
          // The legend is HTML below the chart (plan D7), so the canvas legend is off.
          legend: { display: false },
          tooltip: {
            backgroundColor: rawToken("--sunk", "#121627"),
            borderColor: rawToken("--cyan", "#7dcfff"),
            borderWidth: 1,
            titleColor: rawToken("--fg", "#c0caf5"),
            bodyColor: rawToken("--fg", "#c0caf5"),
            footerColor: rawToken("--fg-muted", "#8892b8"),
            cornerRadius: 2,
            titleFont: { family: rawToken("--font-mono", "ui-monospace, monospace"), size: 11 },
            bodyFont: { family: rawToken("--font-mono", "ui-monospace, monospace"), size: 11 },
            footerFont: { family: rawToken("--font-mono", "ui-monospace, monospace"), size: 10.5, weight: 400 },
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

  return {
    cleanup,
    /** Re-measure after the chart's container stops being hidden (table mode). */
    resize: () => chart?.resize(),
  };
}
