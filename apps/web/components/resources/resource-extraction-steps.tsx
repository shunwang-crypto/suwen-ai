"use client";

import type { LearningResourceOut } from "@/lib/types";

const STEPS = ["原件检查", "页面转写", "题目整理", "待校对"];
const STAGES = ["inspecting", "transcribing", "structuring", "reviewing"] as const;

function stageIndex(resource: LearningResourceOut): number {
  const stage = resource.metadata.extraction_stage;
  const index = STAGES.indexOf(typeof stage === "string" ? stage as (typeof STAGES)[number] : "inspecting");
  return index >= 0 ? index : 0;
}

export function ResourceExtractionSteps({ resource }: { resource: LearningResourceOut }) {
  if (resource.status !== "uploaded" && resource.status !== "extracting") return null;
  const current = stageIndex(resource);
  return (
    <div
      className="min-h-[58px] rounded-[10px] border border-line bg-white px-3 py-2.5"
      role="status"
      aria-label={`题目资料阶段：${STEPS[current]}`}
    >
      <div className="flex items-center gap-1">
        {STEPS.map((label, index) => (
          <span
            key={label}
            className={`h-1.5 flex-1 rounded-full ${
              index < current
                ? "bg-emerald-400"
                : index === current
                  ? "edu-breathe bg-indigo-500"
                  : "bg-slate-200"
            }`}
            title={`${label}${index < current ? "（已完成）" : index === current ? "（进行中）" : ""}`}
          />
        ))}
      </div>
      <div className="mt-1.5 grid grid-cols-4 text-center text-caption text-ink-muted">
        {STEPS.map((label) => <span key={label}>{label}</span>)}
      </div>
    </div>
  );
}
