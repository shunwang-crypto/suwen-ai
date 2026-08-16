// 动态页标题（UI 重构阶段 5）：/courses/[kbId] 顶栏需显示课程名（TITLE_MAP 是静态
// 映射无法覆盖动态段）。页面挂载时 setPageTitle(name)，卸载清空；AppShell 经
// useSyncExternalStore 订阅，动态标题优先于静态 TITLE_MAP。

type Listener = () => void;

let currentTitle: string | null = null;
const listeners = new Set<Listener>();

export function setPageTitle(title: string | null): void {
  currentTitle = title;
  listeners.forEach((listener) => listener());
}

export function subscribePageTitle(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function getSnapshotPageTitle(): string | null {
  return currentTitle;
}
