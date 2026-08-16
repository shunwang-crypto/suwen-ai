"use client";

// 专注会话壳（UI 重构阶段 3）：做题/复习在全屏（脱离 AppShell 侧栏）中完成，
// 结算后返回来源页（from，经 safeInternalPath 校验）。顶栏为极简进度条 +
// 进度文案 + 退出按钮；主体居中 ~860px。子内容通过 FocusSessionHandle 上报进度，
// 并可注册退出确认（如复习队列的「暂时退出」确认模态），未注册则直接返回 from。

import {
  createContext,
  useContext,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useRouter } from "next/navigation";

export interface FocusProgress {
  label: string;
  /** 0-1；null = 不确定进度（生成中） */
  ratio: number | null;
}

export interface FocusSessionHandle {
  setProgress: (progress: FocusProgress | null) => void;
  /** 子组件注册退出前确认（返回清理函数注销）；未注册时退出直接返回 from */
  registerExit: (confirm: (() => void) | null) => void;
}

const FocusSessionContext = createContext<FocusSessionHandle | null>(null);

/** 供专注壳内的子视图使用；不在壳内时返回 null（普通页面渲染不受影响）。 */
export function useFocusSession(): FocusSessionHandle | null {
  return useContext(FocusSessionContext);
}

export function FocusShell({
  from,
  children,
}: {
  from: string;
  children: ReactNode;
}) {
  const router = useRouter();
  const [progress, setProgress] = useState<FocusProgress | null>(null);
  const exitConfirmRef = useRef<(() => void) | null>(null);

  const handle = useMemo<FocusSessionHandle>(
    () => ({
      setProgress,
      registerExit: (confirm) => {
        exitConfirmRef.current = confirm;
      },
    }),
    [],
  );

  const exit = () => {
    const confirm = exitConfirmRef.current;
    if (confirm) confirm();
    else router.push(from);
  };

  return (
    <FocusSessionContext.Provider value={handle}>
      <div className="flex h-dvh flex-col overflow-hidden bg-canvas">
        <header className="flex h-12 shrink-0 items-center gap-4 border-b border-line bg-[#f7f6f4] px-4 sm:px-6">
          <div className="min-w-0 flex-1">
            {progress ? (
              <>
                <p className="truncate text-caption font-medium text-ink-secondary">
                  {progress.label}
                </p>
                {progress.ratio != null && (
                  <div
                    className="mt-1 h-1.5 overflow-hidden rounded-full bg-line"
                    role="progressbar"
                    aria-valuenow={Math.round(progress.ratio * 100)}
                    aria-valuemin={0}
                    aria-valuemax={100}
                  >
                    <div
                      className="h-full rounded-full bg-ink transition-[width] motion-reduce:transition-none"
                      style={{ width: `${Math.min(100, Math.max(0, progress.ratio * 100))}%` }}
                    />
                  </div>
                )}
              </>
            ) : (
              <p className="truncate text-caption text-ink-muted">专注模式</p>
            )}
          </div>
          <button
            type="button"
            onClick={exit}
            className="shrink-0 rounded-[8px] border border-line-strong bg-white px-3 py-1.5 text-body-sm font-medium text-ink-secondary transition-colors hover:border-ink-muted hover:text-ink"
          >
            退出
          </button>
        </header>
        <main className="min-h-0 flex-1 overflow-y-auto">
          <div className="mx-auto w-full max-w-[860px] px-4 pb-12 pt-4 sm:px-6">
            {children}
          </div>
        </main>
      </div>
    </FocusSessionContext.Provider>
  );
}
