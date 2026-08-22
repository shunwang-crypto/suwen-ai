// 轻量全局 toast：模块级发布订阅，零第三方依赖（契合国内源 / 供应链约束）。
// 用法：toast.success("已保存") / toast.error("上传失败") / toast.info("正在生成…")。
// <Toaster/> 挂在根布局订阅并渲染，因此登录、引导和专注会话都共用同一提示层。
// 仅在客户端事件里调用——单例状态是「每浏览器标签页」级别，与服务端渲染无关。

export type ToastType = "success" | "error" | "info" | "warning";

export interface ToastAction {
  label: string;
  href: string;
}

export interface ToastOptions {
  action?: ToastAction;
  /** null 表示只允许用户手动关闭；未传时由 Toaster 使用类型默认值。 */
  durationMs?: number | null;
}

export interface ToastItem {
  id: number;
  type: ToastType;
  message: string;
  action?: ToastAction;
  durationMs?: number | null;
}

type Listener = (items: ToastItem[]) => void;

let items: ToastItem[] = [];
let seq = 0;
const listeners = new Set<Listener>();

function emit() {
  for (const listener of listeners) listener(items);
}

/** 订阅 toast 列表变化；立即回放当前列表。返回取消订阅函数。 */
export function subscribeToasts(listener: Listener): () => void {
  listeners.add(listener);
  listener(items);
  return () => {
    listeners.delete(listener);
  };
}

/** 移除一条 toast（自动消失或手动关闭都走这里）。 */
export function dismissToast(id: number) {
  items = items.filter((t) => t.id !== id);
  emit();
}

function push(type: ToastType, message: string, options: ToastOptions = {}): number {
  const id = ++seq;
  // 同文案去重：避免重试风暴时同一句话叠一屏
  const item: ToastItem = { id, type, message, ...options };
  items = [
    ...items.filter((t) => !(t.type === type && t.message === message)),
    item,
  ].slice(-4);
  emit();
  return id;
}

export const toast = {
  success: (message: string, options?: ToastOptions) => push("success", message, options),
  error: (message: string, options?: ToastOptions) => push("error", message, options),
  info: (message: string, options?: ToastOptions) => push("info", message, options),
  warning: (message: string, options?: ToastOptions) => push("warning", message, options),
};
