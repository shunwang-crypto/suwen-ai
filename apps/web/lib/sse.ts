// SSE 客户端：fetch + ReadableStream 自实现（原生 EventSource 无法带 Authorization 头）。
// 解析 event:/data: 行，产出 {event, data}；七类事件见 docs/api-contract.md。

import {
  API_BASE,
  ApiError,
  getAccessToken,
  notifyUnauthorized,
  parseErrorResponse,
  refreshAccessToken,
} from "./api";

export interface SseMessage {
  event: string;
  data: unknown;
}

/** 契约定义的七类事件名 + followups（追问 chips，done 前下发）。 */
export const SSE_EVENT_NAMES = [
  "token",
  "node_update",
  "trace_event",
  "citation",
  "followups",
  "interrupt",
  "done",
  "error",
] as const;

export type SseEventName = (typeof SSE_EVENT_NAMES)[number];

/** 软超时（P2-18）：连续 N 毫秒收不到任何服务端事件时，产出一条客户端合成的
 *  `stall` 事件（非契约事件，服务端永远不会发），随后继续等待。上游恢复推送后
 *  正常事件继续产出。默认 45 秒。 */
const DEFAULT_IDLE_TIMEOUT_MS = 45_000;

export interface StreamSseOptions {
  signal?: AbortSignal;
  /** 无事件软超时毫秒数；<=0 关闭。默认 45000。 */
  idleTimeoutMs?: number;
}

export interface StallEventData {
  idle_ms: number;
}

function parseBlock(block: string): SseMessage | null {
  let eventName = "message";
  const dataLines: string[] = [];
  for (const rawLine of block.split("\n")) {
    const line = rawLine.endsWith("\r") ? rawLine.slice(0, -1) : rawLine;
    if (!line || line.startsWith(":")) continue; // 空行/注释心跳
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") eventName = value;
    else if (field === "data") dataLines.push(value);
    // id / retry 字段忽略
  }
  if (dataLines.length === 0) return null;
  const raw = dataLines.join("\n");
  let data: unknown = raw;
  try {
    data = JSON.parse(raw);
  } catch {
    // 非 JSON 负载原样透传
  }
  return { event: eventName, data };
}

/**
 * POST 一个 SSE 端点，以 async generator 逐条产出 {event, data}。
 * 首个响应 401 时静默刷新 token 重试一次；刷新失败触发全局登出。
 */
export async function* streamSse(
  path: string,
  body: unknown,
  options: StreamSseOptions = {},
): AsyncGenerator<SseMessage, void, unknown> {
  const doFetch = () => {
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
    };
    const token = getAccessToken();
    if (token) headers.Authorization = `Bearer ${token}`;
    return fetch(`${API_BASE}${path}`, {
      method: "POST",
      headers,
      body: JSON.stringify(body),
      credentials: "include",
      signal: options.signal,
    });
  };

  let res = await doFetch();
  if (res.status === 401) {
    const refreshed = await refreshAccessToken();
    if (!refreshed) {
      notifyUnauthorized();
      throw new ApiError(401, "UNAUTHORIZED", "登录已过期，请重新登录");
    }
    res = await doFetch();
  }
  if (!res.ok) throw await parseErrorResponse(res);
  if (!res.body) {
    throw new ApiError(res.status, "NO_STREAM", "服务端未返回流式响应");
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  const idleMs = options.idleTimeoutMs ?? DEFAULT_IDLE_TIMEOUT_MS;
  let buffer = "";
  // 软超时实现要点：reader.read() 的 Promise 一旦发起就不能丢弃（ReadableStream
  // 不允许并发 read），超时后保留 pending 继续下一轮 race。
  let pending: Promise<ReadableStreamReadResult<Uint8Array>> | null = null;
  let stallNotified = false;
  const STALL = Symbol("stall");
  try {
    for (;;) {
      pending = pending ?? reader.read();
      let result: ReadableStreamReadResult<Uint8Array>;
      if (idleMs > 0) {
        let timer: ReturnType<typeof setTimeout> | undefined;
        const winner = await Promise.race([
          pending,
          new Promise<typeof STALL>((resolve) => {
            timer = setTimeout(() => resolve(STALL), idleMs);
          }),
        ]);
        clearTimeout(timer);
        if (winner === STALL) {
          if (!stallNotified) {
            stallNotified = true;
            yield { event: "stall", data: { idle_ms: idleMs } };
          }
          continue; // pending 保留，继续等上游
        }
        result = winner;
      } else {
        result = await pending;
      }
      pending = null;
      stallNotified = false;
      const { done, value } = result;
      if (done) break;
      // 统一换行符，避免 \r\n 分隔的事件块解析失败
      buffer = (buffer + decoder.decode(value, { stream: true })).replace(
        /\r\n/g,
        "\n",
      );
      let sep: number;
      while ((sep = buffer.indexOf("\n\n")) !== -1) {
        const block = buffer.slice(0, sep);
        buffer = buffer.slice(sep + 2);
        const msg = parseBlock(block);
        if (msg) yield msg;
      }
    }
    // 流结束时冲刷残留块（服务端未以空行收尾的兜底）
    if (buffer.trim()) {
      const msg = parseBlock(buffer);
      if (msg) yield msg;
    }
  } finally {
    try {
      await reader.cancel();
    } catch {
      // 流已关闭时忽略
    }
  }
}
