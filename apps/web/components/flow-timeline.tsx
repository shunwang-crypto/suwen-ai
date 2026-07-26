"use client";

// 图工作流（quiz/plan）的过程时间线：复用 chat 页 ProcessTimeline 的可折叠透明模式，
// 但事件负载是 {node, event, ...}（stream_graph_run 透传的 trace），标签表针对图节点。

import { useState } from "react";

import type { GraphTraceData, NodeUpdateData } from "@/lib/types";

export type FlowItem =
  | { kind: "node"; data: NodeUpdateData }
  | { kind: "trace"; data: GraphTraceData };

const NODE_LABELS: Record<string, string> = {
  weak_points: "画像定向",
  make_blueprint: "生成蓝图",
  confirm_blueprint: "蓝图确认",
  generate: "生成题目",
  validate: "校验题目",
  parse_goal: "解析目标",
  diagnose: "水平摸底",
  gather_resources: "收集资源",
  draft_plan: "起草计划",
  confirm_plan: "计划确认",
};

const EVENT_LABELS: Record<string, string> = {
  weak_points_selected: "已按画像选中薄弱点",
  blueprint_ready: "蓝图就绪",
  approved: "已确认",
  rejected: "已取消",
  generated: "题目已生成",
  passed: "校验通过",
  failed: "校验未通过，打回修复",
  parsed: "目标已解析",
  llm_parse_failed_fallback: "解析降级（使用兜底主题）",
  external_diagnosis_used: "采用已有摸底结果",
  conservative_initialized: "保守初始化掌握度",
  no_topics: "未识别出主题",
  gathered: "资源已收集",
  resources_pending: "资源待补充",
  kb_error: "知识库检索失败",
  web_error: "网络检索失败",
  drafted: "草案完成",
  fix: "程序性修正",
  draft_fallback: "草案降级（确定性兜底）",
  plan_approved: "计划已确认",
  plan_approved_with_edits: "计划已确认（含编辑）",
  plan_rejected: "计划已取消",
};

/** 截断展示任意 JSON 值（修正明细等字段原样透出）。 */
function formatValue(value: unknown, max = 400): string {
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

function FlowRow({ item }: { item: FlowItem }) {
  if (item.kind === "node") {
    const { node, status } = item.data;
    const failed = status === "error" || status === "failed";
    return (
      <li className="flex items-start gap-2.5">
        <span
          className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${
            failed ? "bg-red-500" : "bg-emerald-500"
          }`}
        />
        <div className="min-w-0 flex-1">
          <p className="text-xs text-slate-600">
            <span className="font-medium text-slate-800">
              {NODE_LABELS[node] ?? node}
            </span>
            <span className="ml-1.5 font-mono text-slate-400">{node}</span>
            <span className={`ml-1.5 ${failed ? "text-red-600" : "text-emerald-600"}`}>
              {failed ? "失败" : "已完成"}
            </span>
          </p>
          <ExtraFields data={item.data} known={["node", "status", "detail"]} />
        </div>
      </li>
    );
  }
  const node = typeof item.data.node === "string" ? item.data.node : "";
  const event = typeof item.data.event === "string" ? item.data.event : "";
  return (
    <li className="flex items-start gap-2.5">
      <span className="mt-1.5 h-2 w-2 shrink-0 rounded-full border border-indigo-400 bg-white" />
      <div className="min-w-0 flex-1">
        <p className="text-xs text-slate-600">
          <span className="font-medium text-slate-800">
            {NODE_LABELS[node] ?? (node || "过程")}
          </span>
          {event && (
            <span className="ml-1.5 rounded bg-indigo-50 px-1.5 py-0.5 text-indigo-600">
              {EVENT_LABELS[event] ?? event}
            </span>
          )}
        </p>
        <ExtraFields data={item.data} known={["node", "event"]} />
      </div>
    </li>
  );
}

export function FlowTimeline({
  items,
  streaming,
}: {
  items: FlowItem[];
  streaming: boolean;
}) {
  const [open, setOpen] = useState(false);
  if (items.length === 0) return null;

  const last = items[items.length - 1];
  const lastNode =
    last.kind === "node"
      ? last.data.node
      : typeof last.data.node === "string"
        ? last.data.node
        : "";
  const lastEvent =
    last.kind === "trace" && typeof last.data.event === "string"
      ? last.data.event
      : "";
  const summary = `${NODE_LABELS[lastNode] ?? lastNode}${
    lastEvent ? `：${EVENT_LABELS[lastEvent] ?? lastEvent}` : ""
  }`;

  return (
    <div className="overflow-hidden rounded-lg border border-slate-200 bg-slate-50/80">
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
            <FlowRow key={i} item={item} />
          ))}
        </ul>
      )}
    </div>
  );
}
