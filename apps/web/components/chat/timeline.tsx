"use client";

// 过程时间线：node_update / trace_event 的可折叠透明展示（杜绝黑盒等待）。
// 已知字段结构化展示，evidences 的 dense_hits 等额外字段原样透出。

import { useState } from "react";

import type { NodeUpdateData, TraceEventData } from "@/lib/types";

export type TimelineItem =
  | { kind: "node"; data: NodeUpdateData }
  | { kind: "trace"; data: TraceEventData };

const NODE_LABELS: Record<string, string> = {
  retrieve: "检索",
  retrieval: "检索",
  rerank: "重排",
  generate: "生成",
  router: "路由",
  route: "路由",
  reflect: "自省",
  reflection: "自省",
  rewrite: "改写",
  grade: "评估",
  sanitize: "注入过滤",
};

const STATUS_LABELS: Record<string, string> = {
  running: "进行中",
  pending: "等待中",
  done: "已完成",
  success: "已完成",
  completed: "已完成",
  finished: "已完成",
  error: "失败",
  failed: "失败",
  skipped: "已跳过",
};

const TRACE_LABELS: Record<string, string> = {
  router: "路由决策",
  retrieval: "检索",
  retrieve: "检索",
  rerank: "融合重排",
  reflection: "自省",
  reflect: "自省",
  sanitizer: "注入过滤",
  confidence: "置信度",
  profile: "学情画像",
};

/** 已知 trace 事件的一句话描述；未知类型回退到 reason 字段。 */
function traceDetail(data: TraceEventData): string | null {
  if (data.type === "profile") {
    const raw = Array.isArray(data.weak_points) ? data.weak_points : [];
    const names = raw.filter((x): x is string => typeof x === "string");
    return names.length > 0
      ? `已加载学情画像：${names.length} 个薄弱点（${names.join("、")}）`
      : "已加载学情画像";
  }
  return typeof data.reason === "string" ? data.reason : null;
}

/* 各阶段 16px 内联小图标（路由/检索/自评/生成 + 相邻别名），纯视觉 */
const STAGE_ICON_PATHS: Record<string, string> = {
  // 路由：交叉换轨
  router: "M16 3h5v5M4 20 21 3M21 16v5h-5M15 15l6 6M4 4l5 5",
  route: "M16 3h5v5M4 20 21 3M21 16v5h-5M15 15l6 6M4 4l5 5",
  // 检索：放大镜
  retrieve: "m21 21-4.35-4.35M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16Z",
  retrieval: "m21 21-4.35-4.35M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16Z",
  // 重排：双向箭头
  rerank: "m3 16 4 4 4-4M7 20V4m14 4-4-4-4 4m4-4v16",
  // 自评（自省/评估/置信度）：护盾对勾
  reflect: "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Zm-3-10 2 2 4-4",
  reflection: "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Zm-3-10 2 2 4-4",
  grade: "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Zm-3-10 2 2 4-4",
  confidence: "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Zm-3-10 2 2 4-4",
  // 注入过滤：漏斗
  sanitize: "M22 3H2l8 9.46V19l4 2v-8.54L22 3Z",
  sanitizer: "M22 3H2l8 9.46V19l4 2v-8.54L22 3Z",
  // 生成：星芒
  generate:
    "m12 3-1.9 5.8a2 2 0 0 1-1.3 1.3L3 12l5.8 1.9a2 2 0 0 1 1.3 1.3L12 21l1.9-5.8a2 2 0 0 1 1.3-1.3L21 12l-5.8-1.9a2 2 0 0 1-1.3-1.3L12 3Z",
  // 改写：钢笔
  rewrite: "M17 3a2.8 2.8 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3Z",
  // 学情画像：用户
  profile: "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8Zm-7 8a7 7 0 0 1 14 0",
};

function stageIcon(kind: string, className: string) {
  const d = STAGE_ICON_PATHS[kind];
  if (!d) return null;
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={`inline-block h-4 w-4 shrink-0 align-text-bottom ${className}`}
      aria-hidden
    >
      <path d={d} />
    </svg>
  );
}

const DONE_STATUSES = new Set(["done", "success", "completed", "finished"]);

function statusDot(status: string) {
  if (status === "running" || status === "pending") {
    return (
      <span className="relative flex h-2 w-2 shrink-0">
        <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-indigo-400 opacity-60" />
        <span className="relative inline-flex h-2 w-2 rounded-full bg-indigo-500" />
      </span>
    );
  }
  if (status === "error" || status === "failed") {
    return <span className="h-2 w-2 shrink-0 rounded-full bg-red-500" />;
  }
  if (DONE_STATUSES.has(status)) {
    // 完成项：对勾 draw 动画
    return (
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2.6"
        strokeLinecap="round"
        strokeLinejoin="round"
        className="h-3 w-3 shrink-0 text-emerald-600"
        aria-hidden
      >
        <path d="M20 6 9 17l-5-5" className="fx-check-draw" />
      </svg>
    );
  }
  return <span className="h-2 w-2 shrink-0 rounded-full bg-slate-300" />;
}

/** 截断展示任意 JSON 值（dense_hits 等 evidences 字段直接透出）。 */
function formatValue(value: unknown, max = 600): string {
  let s: string;
  try {
    s = typeof value === "string" ? value : JSON.stringify(value);
  } catch {
    s = String(value);
  }
  return s.length > max ? `${s.slice(0, max)}…` : s;
}

function ExtraFields({
  data,
  known,
}: {
  data: Record<string, unknown>;
  known: string[];
}) {
  const extras = Object.entries(data).filter(
    ([k, v]) => !known.includes(k) && v !== undefined && v !== null,
  );
  if (extras.length === 0) return null;
  return (
    <dl className="mt-1 space-y-0.5">
      {extras.map(([k, v]) => (
        <div key={k} className="flex gap-1.5 text-xs">
          <dt className="shrink-0 font-medium text-slate-400">{k}</dt>
          <dd className="min-w-0 break-all font-mono text-slate-500">
            {formatValue(v)}
          </dd>
        </div>
      ))}
    </dl>
  );
}

function TimelineRow({ item }: { item: TimelineItem }) {
  if (item.kind === "node") {
    const { node, status, detail } = item.data;
    return (
      <li className="flex items-start gap-2.5">
        <span className="mt-1">{statusDot(status)}</span>
        <div className="min-w-0 flex-1">
          <p className="text-xs text-slate-600">
            {stageIcon(
              node,
              status === "error" || status === "failed"
                ? "mr-1 text-red-400"
                : status === "running" || status === "pending"
                  ? "mr-1 text-indigo-500"
                  : "mr-1 text-slate-400",
            )}
            <span className="font-medium text-slate-800">
              {NODE_LABELS[node] ?? node}
            </span>
            <span className="ml-1.5 font-mono text-slate-400">{node}</span>
            <span
              className={`ml-1.5 ${
                status === "error" || status === "failed"
                  ? "text-red-600"
                  : status === "running"
                    ? "text-indigo-600"
                    : "text-emerald-600"
              }`}
            >
              {STATUS_LABELS[status] ?? status}
            </span>
          </p>
          {detail && <p className="text-xs text-slate-500">{detail}</p>}
          <ExtraFields data={item.data} known={["node", "status", "detail"]} />
        </div>
      </li>
    );
  }
  const { type, route } = item.data;
  const detail = traceDetail(item.data);
  return (
    <li className="flex items-start gap-2.5">
      <span
        className={`mt-1.5 h-2 w-2 shrink-0 rounded-full border bg-white ${
          type === "profile" ? "border-violet-500" : "border-indigo-400"
        }`}
      />
      <div className="min-w-0 flex-1">
        <p className="text-xs text-slate-600">
          {stageIcon(
            type,
            type === "profile" ? "mr-1 text-violet-400" : "mr-1 text-slate-400",
          )}
          <span className="font-medium text-slate-800">
            {TRACE_LABELS[type] ?? type}
          </span>
          <span className="ml-1.5 font-mono text-slate-400">{type}</span>
          {route && (
            <span className="ml-1.5 rounded bg-indigo-50 px-1.5 py-0.5 font-mono text-indigo-600">
              → {route}
            </span>
          )}
        </p>
        {detail && <p className="text-xs text-slate-500">{detail}</p>}
        <ExtraFields
          data={item.data}
          known={
            type === "profile"
              ? ["type", "route", "reason", "weak_points"]
              : ["type", "route", "reason"]
          }
        />
      </div>
    </li>
  );
}

export function ProcessTimeline({
  items,
  streaming,
}: {
  items: TimelineItem[];
  streaming: boolean;
}) {
  const [open, setOpen] = useState(false);
  if (items.length === 0) return null;

  const last = items[items.length - 1];
  const summary =
    last.kind === "node"
      ? `${NODE_LABELS[last.data.node] ?? last.data.node}${
          last.data.detail ? `：${last.data.detail}` : ""
        }`
      : `${TRACE_LABELS[last.data.type] ?? last.data.type}${
          traceDetail(last.data) ? `：${traceDetail(last.data)}` : ""
        }`;

  return (
    <div className="mb-2 overflow-hidden rounded-lg border border-slate-200 bg-slate-50/80">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-3 py-1.5 text-left"
      >
        {streaming ? (
          <span className="h-3.5 w-3.5 shrink-0 animate-spin rounded-full border-2 border-indigo-200 border-t-indigo-600" />
        ) : (
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2.2"
            strokeLinecap="round"
            strokeLinejoin="round"
            className="h-3.5 w-3.5 shrink-0 text-emerald-600"
            aria-hidden
          >
            <path d="M20 6 9 17l-5-5" className="fx-check-draw" />
          </svg>
        )}
        <span className="min-w-0 flex-1 truncate text-xs text-slate-600">
          过程时间线（{items.length}）
          <span className="ml-1.5 text-slate-400">{summary}</span>
        </span>
        <svg
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          className={`h-3.5 w-3.5 shrink-0 text-slate-400 transition-transform ${
            open ? "rotate-180" : ""
          }`}
          aria-hidden
        >
          <path d="m6 9 6 6 6-6" />
        </svg>
      </button>
      {open && (
        <ul className="space-y-2 border-t border-slate-200 px-3 py-2">
          {items.map((item, i) => (
            <TimelineRow key={i} item={item} />
          ))}
        </ul>
      )}
    </div>
  );
}
