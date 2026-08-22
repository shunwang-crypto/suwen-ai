"use client";

/**
 * 通用 GET 资源加载 hook (A5 收敛): 替代各页手写的
 * `useEffect + alive flag + setLoading/setError` 三连样板。
 *
 * 契约:
 * - 过期防护走 request-epoch: 卸载/依赖变化时 invalidate, 迟到的响应不再写状态
 *   (与 materials/resource-library 同一机制, 替代散落 30+ 处的手写 alive flag);
 * - reload() 自增触发重新加载; 返回 setData 供乐观更新;
 * - path 为 null 或 enabled=false 时不发请求 (loading 置 false), 供条件加载;
 * - 错误信息提取统一在此 (ApiError.message / fallback), 页面不再各自落地 errText;
 * - deps 额外依赖变化即重载, 默认仅 [path]。
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, apiJson } from "@/lib/api";
import { createRequestEpoch } from "@/lib/request-epoch";

export interface UseApiResourceOptions {
  /** false 时不发请求 (条件加载); 默认 true */
  enabled?: boolean;
  /** 额外重载依赖 (默认仅 path 变化重载) */
  deps?: readonly unknown[];
  /** 请求失败时的兜底文案 */
  errorMessage?: string;
  /** 自定义错误文案提取 (如 billing 对 404 的"服务未就绪"特判); 默认 ApiError.message */
  toErrorMessage?: (err: unknown, fallback: string) => string;
}

export interface ApiResource<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  reload: () => void;
  setData: React.Dispatch<React.SetStateAction<T | null>>;
}

function errorText(err: unknown, fallback: string): string {
  return err instanceof ApiError ? err.message : fallback;
}

export function useApiResource<T>(
  path: string | null,
  options: UseApiResourceOptions = {},
): ApiResource<T> {
  const {
    enabled = true,
    deps = [],
    errorMessage = "加载失败，请稍后重试",
    toErrorMessage = errorText,
  } = options;
  const active = path != null && enabled;
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(active);
  const [reloadKey, setReloadKey] = useState(0);
  const epochRef = useRef(createRequestEpoch());

  const reload = useCallback(() => {
    if (active) setBusy(true);
    setReloadKey((key) => key + 1);
  }, [active]);

  /* eslint-disable react-hooks/set-state-in-effect -- request scope changes must synchronously enter loading/idle and retire stale snapshots */
  useEffect(() => {
    const epoch = epochRef.current;
    // A disabled/null path is itself a scope transition. Retire every request
    // from the previous scope and expose the documented idle state instead of
    // leaving stale data/error (or loading=true) visible to the next consumer.
    if (!active || path == null) {
      epoch.invalidate();
      setData(null);
      setError(null);
      setBusy(false);
      return;
    }
    setBusy(true);
    const token = epoch.begin();
    void (async () => {
      try {
        const result = await apiJson<T>(path);
        if (!epoch.isCurrent(token)) return;
        setData(result);
        setError(null);
      } catch (err) {
        if (!epoch.isCurrent(token)) return;
        setData(null);
        setError(toErrorMessage(err, errorMessage));
      } finally {
        if (epoch.isCurrent(token)) setBusy(false);
      }
    })();
    return () => {
      epoch.invalidate();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- deps 由调用方声明为重载触发器
  }, [path, active, reloadKey, errorMessage, ...deps]);
  /* eslint-enable react-hooks/set-state-in-effect */

  return { data, error, loading: active && busy, reload, setData };
}
