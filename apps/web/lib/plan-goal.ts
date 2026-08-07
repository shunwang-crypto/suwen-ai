export interface PlanGoalFormValue {
  goal: string;
  currentLevel: string;
  targetOutcome: string;
  successCriteria: string;
}

export interface PlanGoalExample extends PlanGoalFormValue {
  label: string;
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
  // 当前基础可以由后续基线任务补齐；主题、成果和验收方式必须由用户明确。
  return readiness.topic && readiness.outcome && readiness.criteria;
}
