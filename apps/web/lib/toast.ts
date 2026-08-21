// 轻量全局 toast：模块级发布订阅，零第三方依赖（契合国内源 / 供应链约束）。
// 用法：toast.success("已保存") / toast.error("上传失败") / toast.info("正在生成…")。
// <Toaster/> 挂在 AppShell 订阅并渲染；仿 components/ui/credits-toast.tsx 的注册回调模式。
// 仅在客户端事件里调用——单例状态是「每浏览器标签页」级别，与服务端渲染无关。

export type ToastType = "success" | "error" | "info";

export interface ToastItem {
  id: number;
  type: ToastType;
  message: string;
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

function push(type: ToastType, message: string): number {
  const id = ++seq;
  // 同文案去重：避免重试风暴时同一句话叠一屏
  items = [...items.filter((t) => !(t.type === type && t.message === message)), { id, type, message }];
  emit();
  return id;
}

export const toast = {
  success: (message: string) => push("success", message),
  error: (message: string) => push("error", message),
  info: (message: string) => push("info", message),
};
