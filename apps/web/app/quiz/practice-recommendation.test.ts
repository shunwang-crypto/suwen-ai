import { describe, expect, it } from "vitest";

import {
  adjustmentLabels,
  recommendationScopeRequest,
  recommendationStatusMessage,
} from "./practice-recommendation";

const base = {
  action: "quiz" as const,
  source: "cold_start" as const,
  title: "用 3 道题建立第一版画像",
  // 过渡字段：仍在类型里，但 ready 态的文案应来自稳定码而不是这段散文
  reason: "旧散文（不应被展示）",
  reason_code: "kb_ready_first_quiz",
  reason_params: {},
  question_count: 3,
  difficulty_mix: { easy: 1, mid: 2, hard: 0 },
  kb: { id: "kb-1", name: "数学" },
  scope_options: [],
  adjustments: ["switch_kb" as const],
};

describe("practice recommendation UI states", () => {
  it("renders the ready state from the stable reason code, not the legacy prose", () => {
    expect(recommendationStatusMessage({ ...base, status: "ready" })).toContain(
      "先从真实章节知识点各取一道题",
    );
    expect(recommendationStatusMessage({ ...base, status: "ready" })).not.toContain("旧散文");
  });

  it("falls back to the legacy reason only while the server has no code", () => {
    expect(
      recommendationStatusMessage({
        ...base,
        status: "ready",
        reason_code: "",
        reason: "资料已经准备好",
      }),
    ).toBe("资料已经准备好");
  });

  it("does not turn KC preparation into a generation error", () => {
    expect(recommendationStatusMessage({ ...base, status: "preparing" })).toContain("整理知识点");
  });

  it("sends only a safe KB identifier for scope selection", () => {
    expect(recommendationScopeRequest("kb-2")).toEqual({ scope_kb_id: "kb-2" });
  });

  it("renders only server-allowed adjustment labels", () => {
    expect(adjustmentLabels(["easier", "switch_kc"])).toEqual([
      ["easier", "简单一点"],
      ["switch_kc", "换个知识点"],
    ]);
    expect(adjustmentLabels([])).toEqual([]);
  });
});
