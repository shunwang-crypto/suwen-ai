/** ?from= 返回路径统一校验（实施指令 0.1-7）：只接受站内路径——
 *  以 `/` 开头且不以 `//` 开头（防协议相对 URL 跳外域），否则回落默认页。 */
export function safeInternalPath(
  value: string | null | undefined,
  fallback = "/practice",
): string {
  if (!value) return fallback;
  if (!value.startsWith("/") || value.startsWith("//")) return fallback;
  return value;
}
