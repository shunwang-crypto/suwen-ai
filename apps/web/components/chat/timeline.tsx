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
};

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
  return <span className="h-2 w-2 shrink-0 rounded-full bg-emerald-500" />;
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
        <span className="mt-1.5">{statusDot(status)}</span>
        <div className="min-w-0 flex-1">
          <p className="text-xs text-slate-600">
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
  const { type, route, reason } = item.data;
  return (
    <li className="flex items-start gap-2.5">
      <span className="mt-1.5 h-2 w-2 shrink-0 rounded-full border border-indigo-400 bg-white" />
      <div className="min-w-0 flex-1">
        <p className="text-xs text-slate-600">
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
        {reason && <p className="text-xs text-slate-500">{reason}</p>}
        <ExtraFields data={item.data} known={["type", "route", "reason"]} />
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
          last.data.reason ? `：${last.data.reason}` : ""
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
            <path d="M20 6 9 17l-5-5" />
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
