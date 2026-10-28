<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from "vue";
import { dashboard } from "../lib/dashboard";
import {
  activityWindow,
  buildHeatmap,
  CELL_PX,
  FILL_FLOOR_PERCENT,
  GAP_PX,
  GRID_HEIGHT_PX,
  GRID_WIDTH_PX,
  GUTTER_PX,
  MONTH_ROW_PX,
  WEEK_COLUMNS,
  WEEKDAY_LABELS,
  type HeatmapCell,
} from "../lib/heatmap";
import { formatTokens, formatTokensShort, formatUtc, NO_DATA } from "../lib/format";
import HudPanel from "./HudPanel.vue";

const state = dashboard.state;

/**
 * The window is fixed (it does not follow the range preset), so it is recomputed with the
 * payload it is drawn from: the grid slides one column over at the next UTC midnight read.
 */
const grid = computed(() => {
  const win = activityWindow(new Date());
  return { win, model: buildHeatmap(state.activity, win) };
});
const model = computed(() => grid.value.model);
const loading = computed(() => state.activity === null);
const empty = computed(() => model.value.max === null && model.value.noDataDays === model.value.cells.length);

/** The ramp is the fill range the scale really spans, so the key is not decorative. */
const RAMP = [FILL_FLOOR_PERCENT, 40, 60, 80, 100];
/** Rows the gutter names: Mon, Wed, Fri (plan D5, matching GitHub). */
const WEEKDAYS = WEEKDAY_LABELS.map((label, row) => ({ label, row })).filter((wd) => wd.label);

const cellX = (col: number): number => GUTTER_PX + col * (CELL_PX + GAP_PX);
const cellY = (row: number): number => MONTH_ROW_PX + row * (CELL_PX + GAP_PX);

/** Quality as words, never as colour alone: the outline only marks that something is off. */
function quality(cell: HeatmapCell): string {
  // A hollow day the API reported is not an absent day: its flags say why there is no total.
  if (!cell.observed) return "unobserved";
  const parts = [...cell.flags];
  if (cell.partial) parts.push("partial");
  if (cell.metadataComplete === false) parts.push("metadata_incomplete");
  return parts.length > 0 ? parts.join(" · ") : "complete";
}

/** UTC date, exact total, share of the window maximum, quality (plan AC3). */
function tooltip(cell: HeatmapCell): string {
  const total = cell.state === "none" ? NO_DATA : formatTokens(cell.value);
  const share = cell.state === "none" ? "—" : `${Math.round(cell.ratio * 100)}% of max`;
  return `${cell.day} · ${total} · ${share} · ${quality(cell)}`;
}

const maxLine = computed(() => {
  const max = model.value.max;
  return max ? `max ${formatTokensShort(max.value)} on ${max.day}` : "max —";
});

const ariaLabel = computed(() => {
  const { win, model: m } = grid.value;
  const max = m.max;
  return (
    `Year activity heatmap, processed tokens per UTC day, ${win.start} to ${win.today}, ` +
    `${WEEK_COLUMNS} weekly columns. ` +
    (max ? `Busiest day ${formatTokens(max.value)} on ${max.day}. ` : "No positive day in this window. ") +
    `${m.noDataDays} of ${m.cells.length} days have no data.`
  );
});

const metaLine = computed(() =>
  state.activity
    ? `52 weeks · processed tokens · UTC · as of ${formatUtc(state.activity.generated_at)}`
    : "52 weeks · processed tokens · UTC",
);

/**
 * Below 1080 px the grid scrolls inside its own panel (plan D8): the newest days are the
 * point of the panel, so the strip opens on them, at mount and on every refresh.
 */
const scroller = ref<HTMLElement | null>(null);

function anchorRight(): void {
  const el = scroller.value;
  if (el) el.scrollLeft = el.scrollWidth;
}

onMounted(() => void nextTick(anchorRight));
watch(() => state.activity, () => void nextTick(anchorRight));
</script>

<template>
  <HudPanel
    index="03"
    title="Activity"
    testid="activity-panel"
    class="pnl-act"
    aria-label="Year activity heatmap"
  >
    <template #meta>
      <span class="meta">{{ metaLine }}</span>
    </template>

    <div v-if="loading" class="hm-loading" data-testid="activity-loading">
      <span class="visually-hidden" role="status">Loading activity</span>
      <span class="sk sk-activity" aria-hidden="true"></span>
    </div>

    <template v-else>
      <div class="hm-scroll" ref="scroller">
        <svg
          class="hm-svg"
          :viewBox="`0 0 ${GRID_WIDTH_PX} ${GRID_HEIGHT_PX}`"
          preserveAspectRatio="xMinYMin meet"
          role="img"
          :aria-label="ariaLabel"
          data-testid="activity-grid"
        >
          <text
            v-for="wd in WEEKDAYS"
            :key="wd.row"
            class="hm-wd"
            :x="GUTTER_PX - 6"
            :y="cellY(wd.row) + CELL_PX / 2"
            text-anchor="end"
            dominant-baseline="middle"
            aria-hidden="true"
          >
            {{ wd.label }}
          </text>
          <text
            v-for="month in model.months"
            :key="`m-${month.col}`"
            class="hm-month"
            :x="cellX(month.col)"
            :y="MONTH_ROW_PX - 4"
            aria-hidden="true"
          >
            {{ month.label }}
          </text>
          <g aria-hidden="true">
            <rect
              v-for="cell in model.cells"
              :key="cell.day"
              class="hm-cell"
              :class="[`hm-${cell.state}`, { 'hm-partial': cell.partial }]"
              :x="cellX(cell.col)"
              :y="cellY(cell.row)"
              :width="CELL_PX"
              :height="CELL_PX"
              rx="2"
              :style="cell.state === 'value' ? { '--p': String(cell.pct) } : undefined"
              :data-day="cell.day"
              :data-state="cell.state"
              :data-ratio="cell.ratio.toFixed(3)"
              :data-pct="cell.pct.toFixed(1)"
              :data-partial="String(cell.partial)"
            >
              <title>{{ tooltip(cell) }}</title>
            </rect>
          </g>
        </svg>
      </div>

      <p v-if="empty" class="hm-empty" data-testid="activity-empty">
        No observed days in this window.
      </p>

      <div class="legend hm-legend" data-testid="activity-legend">
        <span class="hm-key"><i class="hm-sw hm-none"></i>no data</span>
        <span class="hm-key"><i class="hm-sw hm-zero"></i>0</span>
        <span class="hm-key">
          <i
            v-for="pct in RAMP"
            :key="pct"
            class="hm-sw hm-value"
            :style="{ '--p': String(pct) }"
          ></i>
          0 · √ scale · max
        </span>
        <span class="span" data-testid="activity-max">{{ maxLine }}</span>
      </div>

      <details class="chart-disclosure" data-testid="activity-disclosure">
        <summary>Show as table</summary>
        <div class="trend-table">
          <table class="chart-table" data-testid="activity-table">
            <caption>Processed tokens per UTC day</caption>
            <thead>
              <tr>
                <th scope="col">UTC day</th>
                <th scope="col">Processed</th>
                <th scope="col">Computed input</th>
                <th scope="col">Generated output</th>
                <th scope="col">Quality</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="cell in model.cells" :key="cell.day">
                <th scope="row">{{ cell.day }}</th>
                <td>{{ formatTokens(cell.value) }}</td>
                <td>{{ formatTokens(cell.computedInput) }}</td>
                <td>{{ formatTokens(cell.generatedOutput) }}</td>
                <td class="q">{{ quality(cell) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </details>
    </template>
  </HudPanel>
</template>
