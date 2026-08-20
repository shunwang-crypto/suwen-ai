import { describe, expect, it } from "vitest";

import {
  REASON_CODES,
  REASON_FALLBACK,
  reasonText,
  reasonTextOf,
} from "@/lib/reason-text";

describe("推荐理由文案层", () => {
  it("覆盖后端全部稳定码，且都产出非空中文文案", () => {
    for (const code of REASON_CODES) {
      const copy = reasonText(code, {
        count: 3,
        quote: "卡在链式法则",
        kc_name: "链式法则",
        zone: "待巩固",
        difficulty: "mid",
        band_position: 0,
        recent_error: "no",
        goal: "两周补上导数",
        day_no: 2,
        undone: 1,
      });
      expect(copy.length, `${code} 无文案`).toBeGreaterThan(0);
      expect(copy, `${code} 落到兜底文案`).not.toBe(REASON_FALLBACK);
    }
  });

  it("未知码与空码走兜底，不显示裸码", () => {
    expect(reasonText("no_such_code", {})).toBe(REASON_FALLBACK);
    expect(reasonText(undefined)).toBe(REASON_FALLBACK);
    expect(reasonText("no_such_code", {})).not.toContain("no_such_code");
  });

  it("参数缺失或类型不对时不抛错，也不渲染 undefined/NaN", () => {
    const copy = reasonText("plan_task_progress", {});
    expect(copy).not.toContain("undefined");
    expect(copy).not.toContain("NaN");
    expect(reasonText("review_due", { count: "3" as unknown as number })).toContain("0 道");
  });

  it("mastery_zone 的 16 种措辞由 zone×段位×错题态 决定，与后端同源", () => {
    const zones = ["较弱", "待巩固", "接近掌握", "已掌握"];
    const seen = new Set<string>();
    for (const zone of zones) {
      for (const band_position of [0, 1]) {
        for (const recent_error of ["no", "yes"]) {
          seen.add(
            reasonText("mastery_zone", {
              kc_name: "链式法则",
              zone,
              difficulty: "mid",
              band_position,
              recent_error,
            }),
          );
        }
      }
    }
    expect(seen.size).toBe(16);
  });

  it("mastery_zone 复现后端逐字文案（较弱前段 + 近 7 天答错过）", () => {
    expect(
      reasonText("mastery_zone", {
        kc_name: "链式法则",
        zone: "较弱",
        difficulty: "easy",
        band_position: 0,
        recent_error: "yes",
      }),
    ).toBe(
      "「链式法则」掌握度较弱，尚在起步，低于最近发展区，先用简单题把基础概念立起来；近 7 天答错过，优先安排重练",
    );
  });

  it("文案里不出现掌握度小数或百分比（P2-7 脱敏口径）", () => {
    for (const code of REASON_CODES) {
      const copy = reasonText(code, {
        count: 3,
        quote: "卡住了",
        kc_name: "链式法则",
        zone: "接近掌握",
        difficulty: "hard",
        band_position: 1,
        recent_error: "yes",
        goal: "补上导数",
        day_no: 2,
        undone: 1,
      });
      expect(copy, `${code} 泄漏小数`).not.toMatch(/\d\.\d/);
      expect(copy, `${code} 泄漏百分比`).not.toMatch(/\d+\s*%/);
    }
  });

  it("reasonTextOf: 有码用码，无码走安全兜底", () => {
    expect(
      reasonTextOf({ reason_code: "review_due", reason_params: { count: 5 } }),
    ).toContain("5 道错题");
    expect(reasonTextOf({ reason_code: "" })).toBe(REASON_FALLBACK);
    expect(reasonTextOf(null)).toBe(REASON_FALLBACK);
    expect(reasonTextOf(undefined)).toBe(REASON_FALLBACK);
  });

  it("未知码不读取旧散文", () => {
    expect(reasonTextOf({ reason_code: "future_code" })).toBe(REASON_FALLBACK);
  });
});
