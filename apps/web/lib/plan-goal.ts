export interface PlanGoalFormValue {
  goal: string;
  currentLevel: string;
  targetOutcome: string;
  successCriteria: string;
}

export interface PlanGoalExample extends PlanGoalFormValue {
  label: string;
}

export interface ParsedPlanIntent {
  prompt: string;
  deadline: string | null;
  dailyHours: number | null;
  totalDays: number | null;
  missing: readonly ("deadline" | "daily_hours")[];
}

/** Parse only explicit, user-authored scheduling facts; do not invent dates or time. */
export function parsePlanIntent(prompt: string, now = new Date()): ParsedPlanIntent {
  const value = prompt.trim();
  const dateMatch = value.match(/(?:在|到|于)?\s*(\d{1,2})月(\d{1,2})(?:日|号)/);
  let deadline: string | null = null;
  if (dateMatch) {
    const month = Number(dateMatch[1]);
    const day = Number(dateMatch[2]);
    const year = now.getFullYear();
    const candidate = new Date(year, month - 1, day);
    if (candidate.getMonth() === month - 1 && candidate.getDate() === day) {
      if (candidate.getTime() < new Date(year, now.getMonth(), now.getDate()).getTime()) {
        candidate.setFullYear(year + 1);
      }
      deadline = `${candidate.getFullYear()}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
    }
  }
  const weekMatch = value.match(/(\d{1,2})\s*周(?:内|后|完成|达到|冲刺)?/);
  const chineseWeekMatch = value.match(/(一|两|二|三|四|五|六|七|八|九|十)\s*周(?:内|后|完成|达到|冲刺)?/);
  const dayMatch = value.match(/(\d{1,3})\s*天(?:内|后|完成|达到|冲刺)?/);
  const hoursMatch = value.match(/每天\s*(\d+(?:\.\d+)?)\s*(?:小时|h)/i);
  const dailyHours = hoursMatch ? Math.max(0.5, Math.min(12, Number(hoursMatch[1]))) : null;
  const chineseWeeks = { 一: 1, 两: 2, 二: 2, 三: 3, 四: 4, 五: 5, 六: 6, 七: 7, 八: 8, 九: 9, 十: 10 } as const;
  const weekCount = weekMatch
    ? Number(weekMatch[1])
    : chineseWeekMatch
      ? chineseWeeks[chineseWeekMatch[1] as keyof typeof chineseWeeks]
      : null;
  const totalDays = weekCount != null
    ? Math.max(1, Math.min(90, weekCount * 7))
    : dayMatch
      ? Math.max(1, Math.min(90, Number(dayMatch[1])))
      : null;
  const missing: Array<"deadline" | "daily_hours"> = [];
  if (!deadline && !totalDays) missing.push("deadline");
  if (dailyHours == null) missing.push("daily_hours");
  return { prompt: value, deadline, dailyHours, totalDays, missing };
}

export const PLAN_GOAL_EXAMPLES: readonly PlanGoalExample[] = [
  {
    label: "高数基础",
    goal: "极限与导数基础",
    currentLevel: "会基本代数运算，但极限概念不熟",
    targetOutcome: "能独立判断常见极限题型并完成基础导数计算",
    successCriteria: "不看提示完成 10 道教材基础题，至少正确 8 道并能说明错因",
  },
  {
    label: "英语阅读",
    goal: "大学英语四级阅读",
    currentLevel: "能读懂短句，长难句和定位题容易出错",
    targetOutcome: "能在限定时间内完成一篇四级阅读并说明每题依据",
    successCriteria: "连续 3 篇练习中，每篇至少答对 4/5，并标出原文证据句",
  },
  {
    label: "Python 入门",
    goal: "Python 数据处理基础",
    currentLevel: "了解变量和循环，尚未独立完成小项目",
    targetOutcome: "能读取 CSV、清洗缺失值并输出一份汇总结果",
    successCriteria: "独立完成一个可重复运行的小脚本，并用新数据验证输出",
  },
] as const;

export function planGoalReadiness(value: PlanGoalFormValue) {
  return {
    topic: Boolean(value.goal.trim()),
    baseline: Boolean(value.currentLevel.trim()),
    outcome: Boolean(value.targetOutcome.trim()),
    criteria: Boolean(value.successCriteria.trim()),
  };
}

export function isPlanGoalReady(value: PlanGoalFormValue): boolean {
  const readiness = planGoalReadiness(value);
  // 一句话目标先启动；当前基础、成果和验收标准由图在确实缺失时追问。
  return readiness.topic;
}
