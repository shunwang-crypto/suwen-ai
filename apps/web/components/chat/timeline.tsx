"use client";

// 过程时间线：node_update / trace_event 的可折叠透明展示（杜绝黑盒等待）。
// 已知字段结构化展示，evidences 的 dense_hits 等额外字段原样透出。

import { useState } from "react";

import type { NodeUpdateData, TraceEventData } from "@/lib/types";

export type TimelineItem =
  | { kind: "node"; data: NodeUpdateData }
  | { kind: "trace"; data: TraceEventData };

const NODE_LABELS: Record<string, string> = {
  cache: "缓存匹配",
  cache_lookup: "缓存匹配",
  query_plan: "整理问题",
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
  verify: "核验依据",
  verify_support: "核验依据",
  followups: "整理追问",
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
  cache: "缓存匹配",
  cache_lookup: "缓存匹配",
  router: "路由决策",
  query_plan: "整理问题",
  retrieval: "检索",
  retrieve: "检索",
  rerank: "融合重排",
  grade: "资料评估",
  generate: "生成回答",
  verify: "依据核验",
  verify_support: "依据核验",
  followups: "整理追问",
  reflection: "自省",
  reflect: "自省",
  sanitizer: "注入过滤",
  confidence: "置信度",
  profile: "学情画像",
  web_query_plan: "整理搜索问题",
  web_search: "网页搜索",
};

/** 已知 trace 事件的一句话描述；未知类型回退到 reason 字段。 */
function traceDetail(data: TraceEventData): string | null {
  if (data.type === "cache" || data.type === "cache_lookup") {
    const source = typeof data.source === "string" ? data.source.toUpperCase() : "";
    return source ? `已命中 ${source} 缓存` : "已完成缓存匹配";
  }
  if (data.type === "query_plan") {
    const count = Array.isArray(data.queries) ? data.queries.length : 0;
    return count > 0 ? `已整理 ${count} 个检索问题` : "已整理检索问题";
  }
  if (data.type === "retrieval" || data.type === "retrieve") {
    const returned = Number(data.returned);
    return Number.isFinite(returned) ? `已找到 ${returned} 条相关资料` : "已完成资料检索";
  }
  if (data.type === "generate") return "已生成回答";
  if (data.type === "verify" || data.type === "verify_support") {
    return data.supported === false ? "部分内容需要进一步核验" : "已核验回答依据";
  }
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
  // 缓存匹配：闪电
  cache: "m13 2-9 12h7l-1 8 9-12h-7l1-8Z",
  cache_lookup: "m13 2-9 12h7l-1 8 9-12h-7l1-8Z",
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
  query_plan: "M17 3a2.8 2.8 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3Z",
  // 学情画像：用户
  profile: "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8Zm-7 8a7 7 0 0 1 14 0",
  web_query_plan: "m21 21-4.35-4.35M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16Z",
  web_search: "m21 21-4.35-4.35M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16Z",
  verify: "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Zm-3-10 2 2 4-4",
  verify_support: "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Zm-3-10 2 2 4-4",
  followups: "m8 10 4-4 4 4m-4-4v12M5 21h14",
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
  return <span className="h-2 w-2 shrink-0 rounded-full bg-surface-muted" />;
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
          <dt className="shrink-0 font-medium text-ink-muted">{k}</dt>
          <dd className="min-w-0 break-all font-mono text-ink-muted">
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
          <p className="text-xs text-ink-secondary">
            {stageIcon(
              node,
              status === "error" || status === "failed"
                ? "mr-1 text-red-400"
                : status === "running" || status === "pending"
                  ? "mr-1 text-indigo-500"
                  : "mr-1 text-ink-muted",
            )}
            <span className="font-medium text-ink">
              {NODE_LABELS[node] ?? node}
            </span>
            <span className="ml-1.5 font-mono text-ink-muted">{node}</span>
            <span
              className={`ml-1.5 ${
                status === "error" || status === "failed"
                  ? "text-red-600"
                  : status === "running" || status === "pending"
                    ? "text-indigo-600"
                    : "text-emerald-600"
              }`}
            >
              {STATUS_LABELS[status] ?? status}
            </span>
          </p>
          {detail && <p className="text-xs text-ink-muted">{detail}</p>}
          <ExtraFields
            data={item.data}
            known={["node", "status", "detail", "synthetic"]}
          />
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
        <p className="text-xs text-ink-secondary">
          {stageIcon(
            type,
            type === "profile" ? "mr-1 text-violet-400" : "mr-1 text-ink-muted",
          )}
          <span className="font-medium text-ink">
            {TRACE_LABELS[type] ?? type}
          </span>
          <span className="ml-1.5 font-mono text-ink-muted">{type}</span>
          {route && (
            <span className="ml-1.5 rounded bg-indigo-50 px-1.5 py-0.5 font-mono text-indigo-600">
              → {route}
            </span>
          )}
        </p>
        {detail && <p className="text-xs text-ink-muted">{detail}</p>}
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
  answerStarted = false,
}: {
  items: TimelineItem[];
  streaming: boolean;
  answerStarted?: boolean;
}) {
  const [open, setOpen] = useState(false);
  if (items.length === 0) return null;

  const last = items[items.length - 1];
  const lastStage = last.kind === "node" ? last.data.node : last.data.type;
  const lastLabel =
    last.kind === "node"
      ? NODE_LABELS[last.data.node] ?? last.data.node
      : TRACE_LABELS[last.data.type] ?? last.data.type;
  const lastDetail =
    last.kind === "node" ? last.data.detail : traceDetail(last.data);
  let summary = `${lastLabel}${lastDetail ? `：${lastDetail}` : ""}`;
  if (streaming && answerStarted) {
    summary = "正在生成回答…";
  } else if (streaming && ["router", "route", "query_plan"].includes(lastStage)) {
    summary = "正在检索资料…";
  } else if (
    streaming &&
    ["retrieve", "retrieval", "rerank", "grade"].includes(lastStage)
  ) {
    summary = "资料已就绪，正在生成回答…";
  } else if (
    streaming &&
    ["generate", "verify", "verify_support"].includes(lastStage)
  ) {
    summary = "回答已生成，正在核验依据…";
  }

  return (
    <div className="mb-2 overflow-hidden rounded-lg border border-line bg-surface-muted/80">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-3 py-1.5 text-left"
        aria-expanded={open}
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
        <span className="min-w-0 flex-1 truncate text-xs text-ink-secondary">
          过程时间线（{items.length}）
          <span className="ml-1.5 text-ink-muted">{summary}</span>
        </span>
        <svg
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          className={`h-3.5 w-3.5 shrink-0 text-ink-muted transition-transform ${
            open ? "rotate-180" : ""
          }`}
          aria-hidden
        >
          <path d="m6 9 6 6 6-6" />
        </svg>
      </button>
      {open && (
        <ul className="space-y-2 border-t border-line px-3 py-2">
          {items.map((item, i) => (
            <TimelineRow key={i} item={item} />
          ))}
        </ul>
      )}
    </div>
  );
}
