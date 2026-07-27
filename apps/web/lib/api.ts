// fetch 封装：API 基址 / Bearer / 401 静默刷新重试一次 / 错误信封解析。
// access token 只存内存（模块级变量），刷新靠 httpOnly refresh cookie。

import type { TokenResponse } from "./types";

// 默认同源 (dev 走 next.config.ts rewrites, 生产走 Caddy 反代);
// 仅当前后端确实异源部署时才设置 NEXT_PUBLIC_API_BASE
export const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "";

let accessToken: string | null = null;

export function setAccessToken(token: string | null): void {
  accessToken = token;
}

export function getAccessToken(): string | null {
  return accessToken;
}

/** 401 且刷新失败时的全局回调（AuthProvider 注册 -> 清态并跳登录）。 */
let onUnauthorized: (() => void) | null = null;

export function setOnUnauthorized(handler: (() => void) | null): void {
  onUnauthorized = handler;
}

export function notifyUnauthorized(): void {
  onUnauthorized?.();
}

/** 402 INSUFFICIENT_CREDITS 全局回调（CreditsToast 注册 -> 提示额度不足并引导充值）。 */
let onInsufficientCredits: (() => void) | null = null;

export function setOnInsufficientCredits(handler: (() => void) | null): void {
  onInsufficientCredits = handler;
}

/** 后端错误信封 {code, message, request_id} 的统一异常。message 为中文，可直接展示。 */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId?: string;

  constructor(status: number, code: string, message: string, requestId?: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.requestId = requestId;
  }
}

/** 从失败响应解析错误信封；非 JSON 时给出兜底中文提示。 */
export async function parseErrorResponse(res: Response): Promise<ApiError> {
  let code = `HTTP_${res.status}`;
  let message = `请求失败（HTTP ${res.status}）`;
  let requestId: string | undefined;
  try {
    const body = (await res.json()) as {
      code?: string;
      message?: string;
      request_id?: string;
      detail?: unknown;
    };
    if (typeof body.code === "string") code = body.code;
    if (typeof body.message === "string" && body.message) message = body.message;
    else if (typeof body.detail === "string" && body.detail) message = body.detail;
    if (typeof body.request_id === "string") requestId = body.request_id;
  } catch {
    // 保留兜底提示
  }
  return new ApiError(res.status, code, message, requestId);
}

// 刷新去重：并发 401 只发一次 refresh。
let refreshPromise: Promise<boolean> | null = null;

/** 用 refresh cookie 静默换新 access token。成功返回 true。 */
export function refreshAccessToken(): Promise<boolean> {
  if (!refreshPromise) {
    refreshPromise = (async () => {
      try {
        const res = await fetch(`${API_BASE}/api/auth/refresh`, {
          method: "POST",
          credentials: "include",
        });
        if (!res.ok) return false;
        const data = (await res.json()) as TokenResponse;
        accessToken = data.access_token;
        return true;
      } catch {
        return false;
      }
    })().finally(() => {
      refreshPromise = null;
    });
  }
  return refreshPromise;
}

function buildHeaders(extra?: HeadersInit): Headers {
  const headers = new Headers(extra);
  if (accessToken && !headers.has("Authorization")) {
    headers.set("Authorization", `Bearer ${accessToken}`);
  }
  return headers;
}

/**
 * 带鉴权的 fetch。401 时（非 /api/auth/* 端点）刷新一次并重试；
 * 仍 401 则触发全局 onUnauthorized。非 2xx 抛 ApiError。
 */
export async function apiFetch(
  path: string,
  init: RequestInit = {},
): Promise<Response> {
  const doFetch = () =>
    fetch(`${API_BASE}${path}`, {
      ...init,
      headers: buildHeaders(init.headers),
      credentials: "include",
    });

  let res = await doFetch();
  if (res.status === 401 && !path.startsWith("/api/auth/")) {
    const refreshed = await refreshAccessToken();
    if (refreshed) res = await doFetch();
    if (res.status === 401) notifyUnauthorized();
  }
  if (!res.ok) {
    const err = await parseErrorResponse(res);
    // 402 额度不足：任何功能请求都可能返回。全局提示一次，同时照常抛出，
    // 调用方仍可按需做内联展示（不吞错、不改调用层契约）。
    if (err.status === 402 && err.code === "INSUFFICIENT_CREDITS") {
      onInsufficientCredits?.();
    }
    throw err;
  }
  return res;
}

/** JSON 便捷封装：自动序列化请求体、反序列化响应；204 返回 undefined。 */
export async function apiJson<T>(
  path: string,
  options: { method?: string; body?: unknown; signal?: AbortSignal } = {},
): Promise<T> {
  const init: RequestInit = {
    method: options.method ?? (options.body !== undefined ? "POST" : "GET"),
    signal: options.signal,
  };
  if (options.body !== undefined) {
    init.body = JSON.stringify(options.body);
    init.headers = { "Content-Type": "application/json" };
  }
  const res = await apiFetch(path, init);
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}
