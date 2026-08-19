"use client";

/**
 * 推荐理由的展示文案层 —— 稳定码 → 中文文案的唯一出处。
 *
 * 后端只产出 `reason_code` + `reason_params`（见 apps/api 的 reason_codes.py），
 * 文案在这里落地。这样改文案不动后端、不改行为，也不会让同一条理由在
 * next-actions / 练习推荐 / 出题蓝图三处各写一遍。
 *
 * 过渡期：响应里仍带 `reason` 散文字段，仅供尚未升级的旧客户端。
 * **新代码只读 reason_code / reason_params**，禁止读 reason
 * （守卫见 lib/__tests__/reason-text.test.ts 与后端 tests/unit/test_reason_codes.py）。
 *
 * 脱敏口径（P2-7）：参数里只会出现定性档位词、计数与展示字符串，
 * 不会出现掌握度小数 —— 后端 reason_params 的闭合白名单从类型上就不允许 float。
 */

/** 与后端 ReasonCode 枚举一一对应（值即 wire format）。 */
export const REASON_CODES = [
  "review_due",
  "resume_confused",
  "resume_misconception",
  "mastery_zone",
  "weak_kc_generic",
  "weak_kc_unstable",
  "kc_unverified",
  "practice_maintain",
  "plan_task_progress",
  "plan_task_pending",
  "cold_start_no_evidence",
  "kb_ready_first_quiz",
  "kb_choice_required",
  "kb_preparing",
  "kb_empty",
  "kb_absent",
] as const;

export type ReasonCode = (typeof REASON_CODES)[number];

export type ReasonParamValue = string | number;
export type ReasonParams = Record<string, ReasonParamValue>;

/** 带稳定码的推荐载荷；`reason` 为过渡字段，新代码不要读。 */
export interface ReasonCarrier {
  reason_code?: string;
  reason_params?: ReasonParams;
}

const DIFFICULTY_LABELS: Record<string, string> = {
  easy: "简单",
  mid: "中等",
  hard: "困难",
};

/**
 * mastery_zone 的 16 种措辞：与后端 profile_summary._ZONE_REASONS /
 * _RECENT_ERROR_SUFFIXES 逐字同源（4 区间 × 2 段位 × 2 错题态）。
 * 后端保留同一份散文只为兼容旧客户端；改文案时两边必须一起改，
 * test_mastery_zone_matches_backend_wording 会盯住结构。
 */
const ZONE_PHRASES: Record<string, readonly [string, string]> = {
  较弱: [
    "掌握度较弱，尚在起步，低于最近发展区，先用{difficulty}题把基础概念立起来",
    "掌握度较弱，有过接触但还没入门，先用{difficulty}题打稳地基",
  ],
  待巩固: [
    "掌握度待巩固，刚迈进最近发展区，用{difficulty}题小步推进最见效",
    "掌握度待巩固，已到最近发展区后段，用{difficulty}题练熟就能上台阶",
  ],
  接近掌握: [
    "接近掌握，还差一段火候，用{difficulty}题继续逼近达标线",
    "接近掌握，离达标线只差一步，用{difficulty}题冲刺达标",
  ],
  已掌握: [
    "已掌握，用{difficulty}题保温，防止生疏回退",
    "已掌握且相当扎实，用{difficulty}题挑战边界、保持手感",
  ],
};

const RECENT_ERROR_SUFFIXES: readonly [string, string] = [
  "；近 7 天答错过，优先安排重练",
  "；近 7 天答错过，趁热订正防再错",
];

/** 未知码兜底：不编造依据，只说明这是系统按当前学习路径给的建议。 */
export const REASON_FALLBACK = "系统按你当前的学习路径推荐";

function text(params: ReasonParams, key: string): string {
  const value = params[key];
  return typeof value === "string" ? value : "";
}

function count(params: ReasonParams, key: string): number {
  const value = params[key];
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function masteryZoneText(params: ReasonParams): string {
  const zone = text(params, "zone");
  const phrases = ZONE_PHRASES[zone];
  if (!phrases) return REASON_FALLBACK;
  const position = count(params, "band_position") === 1 ? 1 : 0;
  const difficulty = DIFFICULTY_LABELS[text(params, "difficulty")] ?? "中等";
  const kcName = text(params, "kc_name");
  const body = phrases[position].replace("{difficulty}", difficulty);
  const suffix = text(params, "recent_error") === "yes" ? RECENT_ERROR_SUFFIXES[position] : "";
  return `${kcName ? `「${kcName}」` : ""}${body}${suffix}`;
}

const TEMPLATES: Record<ReasonCode, (params: ReasonParams) => string> = {
  review_due: (p) =>
    `FSRS 间隔复习排程：${count(p, "count")} 道错题今天到了最佳复习时点，现在重练巩固效果最好`,
  resume_confused: (p) => `长期记忆里记着你当时的原话：“${text(p, "quote")}”`,
  resume_misconception: (p) => `辅导小结记录过这个易错点：“${text(p, "quote")}”`,
  mastery_zone: masteryZoneText,
  weak_kc_generic: (p) => `掌握度${text(p, "zone") || "待巩固"}，定向练习最见效`,
  weak_kc_unstable: () => "近期作答显示这些知识点还不稳定，先用一组短练习巩固",
  kc_unverified: (p) => `「${text(p, "kc_name")}」尚未测过，先做一次快速验证`,
  practice_maintain: () =>
    "已有作答记录，但目前没有需要优先补强的薄弱知识点；先做一组短练习保持熟练度",
  plan_task_progress: (p) =>
    `计划《${text(p, "goal")}》进行到第 ${count(p, "day_no")} 天，按日程还有 ${count(p, "undone")} 个任务没完成`,
  plan_task_pending: (p) => `今天的学习计划还有这个任务未完成：${text(p, "goal")}`,
  cold_start_no_evidence: () => "还没有足够的真实作答证据，先用几道题建立起点，再决定接下来怎么学",
  kb_ready_first_quiz: () => "这份资料已经准备好，先从真实章节知识点各取一道题了解你的起点",
  kb_choice_required: () => "你有多份已准备好的资料，但当前没有足够上下文判断练哪一科",
  kb_preparing: () => "资料已收到，系统正在整理知识点；完成后会自动准备第一组练习",
  kb_empty: () => "这份知识库还没有可用资料；上传后才能建立章节知识点和练习",
  kb_absent: () => "目前还没有可用的个人资料；上传后才能按你的内容建立练习",
};

function isKnownCode(code: string): code is ReasonCode {
  return code in TEMPLATES;
}

/** 稳定码 + 参数 → 展示文案；未知码走兜底，绝不抛错也绝不显示裸码。 */
export function reasonText(code: string | undefined, params?: ReasonParams): string {
  if (!code || !isKnownCode(code)) return REASON_FALLBACK;
  try {
    return TEMPLATES[code](params ?? {});
  } catch {
    return REASON_FALLBACK;
  }
}

/**
 * 从带码载荷取展示文案。过渡期允许传入旧散文做回退（`legacyReason`）：
 * 只在后端还没给码时用，新接口一律走码。
 */
export function reasonTextOf(carrier: ReasonCarrier | null | undefined, legacyReason = ""): string {
  const code = carrier?.reason_code;
  if (code && isKnownCode(code)) return reasonText(code, carrier?.reason_params);
  return legacyReason || REASON_FALLBACK;
}
