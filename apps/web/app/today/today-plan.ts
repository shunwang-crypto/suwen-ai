import type { PlanResource, PlanSchedule, PlanTask, ReviewItem } from "@/lib/types";

export type FlatPlanTask = PlanTask & { day: number; stage: string };

function isPending(task: PlanTask): boolean {
  return task.status ? task.status === "pending" : !task.done;
}

export interface TodayItem {
  id: string;
  type: "study" | "practice" | "review";
  title: string;
  context: string;
  duration: string;
  priority: "high" | "normal";
  dueLabel?: string;
  done: boolean;
  task?: FlatPlanTask;
  review?: ReviewItem;
  href: string;
}

function attributionQuery(task: PlanTask, planId?: string | null): string {
  if (!planId || !task.task_key) return "";
  return `&plan_id=${encodeURIComponent(planId)}&task_key=${encodeURIComponent(task.task_key)}`;
}

export function taskExecutionHref(task: PlanTask, kbId?: string | null, planId?: string | null): string {
  const topic = task.kc_names?.[0] || task.title;
  const kb = kbId ? `&kb=${encodeURIComponent(kbId)}` : "";
  const count = task.practice_count && task.practice_count >= 1 && task.practice_count <= 20 ? task.practice_count : 3;
  const difficultyValue = task.difficulty && ["easy", "mid", "hard"].includes(task.difficulty) ? task.difficulty : "mid";
  const difficulty = `&difficulty=${encodeURIComponent(difficultyValue)}`;
  const attribution = attributionQuery(task, planId);
  switch (task.action_type) {
    case "practice":
      return `/quiz?topic=${encodeURIComponent(topic)}${kb}&count=${count}${difficulty}${attribution}`;
    case "read":
      return task.resource?.url || (task.resource?.kb_id || kbId ? `/kb?kb=${encodeURIComponent(task.resource?.kb_id || kbId || "")}` : "/tutor");
    case "review_errors":
      return attribution ? `/reviews?${attribution.slice(1)}` : "/reviews";
    case "mixed":
      return `/quiz?topic=${encodeURIComponent(topic)}${kb}&count=${count}${difficulty}${attribution}`;
    case "tutor":
    default:
      return planId && task.task_key
        ? `/tutor?plan_id=${encodeURIComponent(planId)}&task_key=${encodeURIComponent(task.task_key)}${kb}`
        : "/tutor";
  }
}

export interface TodayItemsResult {
  todayTasks: FlatPlanTask[];
  overdueTasks: FlatPlanTask[];
  nextTask: FlatPlanTask | null;
  todos: TodayItem[];
}

export function buildTodayItems(
  tasks: FlatPlanTask[],
  schedule: PlanSchedule | undefined,
  reviews: ReviewItem[],
  planKbId?: string | null,
  planId?: string | null,
): TodayItemsResult {
  const todayKeys = new Set(schedule?.today_tasks ?? []);
  const overdueKeys = new Set(schedule?.overdue_tasks ?? []);
  const todayTasks = tasks.filter((task) => isPending(task) && task.task_key && todayKeys.has(task.task_key));
  const overdueTasks = tasks.filter((task) => isPending(task) && task.task_key && overdueKeys.has(task.task_key));
  const nextTask = todayTasks[0] ?? overdueTasks[0] ?? null;

  const reviewItems: TodayItem[] = reviews.slice(0, 2).map((review) => ({
    id: `review-${review.error_item_id}`,
    type: "review",
    title: review.question.stem,
    context: "今日到期复习",
    duration: "约 10 分钟",
    priority: "high",
    dueLabel: "今日到期",
    done: false,
    review,
    href: "/reviews",
  }));
  const planItems: TodayItem[] = [...todayTasks, ...overdueTasks]
    .slice(0, Math.max(0, 5 - reviewItems.length))
    .map((task, index) => {
      const practice = task.action_type === "practice" || (!task.action_type && /练习|测验|测试|习题/.test(task.title));
      const isOverdue = overdueKeys.has(task.task_key ?? "");
      return {
        id: `task-${task.task_key ?? `${task.day}-${index}`}`,
        type: practice ? "practice" : "study",
        title: task.title,
        context: `${task.stage} · 第 ${task.day} 天`,
        duration: `${task.minutes} 分钟`,
        priority: !isOverdue && index === 0 ? "high" : "normal",
        dueLabel: isOverdue ? "逾期" : "计划今日",
        done: Boolean(task.done),
        task,
        href: taskExecutionHref(task, planKbId, planId),
      };
    });
  const completed = [...tasks].reverse().find((task) => task.done);
  const completedItem: TodayItem[] = completed && reviewItems.length + planItems.length < 5 ? [{
    id: `task-${completed.task_key ?? "completed"}`,
    type: /练习|测验|测试|习题/.test(completed.title) ? "practice" : "study",
    title: completed.title,
    context: `${completed.stage} · 第 ${completed.day} 天`,
    duration: `${completed.minutes} 分钟`,
    priority: "normal",
    done: true,
    task: completed,
    href: taskExecutionHref(completed, planKbId, planId),
  }] : [];

  return { todayTasks, overdueTasks, nextTask, todos: [...reviewItems, ...planItems, ...completedItem] };
}

export function resourceHref(resource: PlanResource | null | undefined): string | null {
  if (resource?.url) return resource.url;
  if (resource?.kb_id) return `/kb?kb=${encodeURIComponent(resource.kb_id)}`;
  return null;
}
