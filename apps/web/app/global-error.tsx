"use client";

// 根布局崩溃时的最终兜底（App Router `global-error.js` 约定）。
// 它替换的正是加载 globals.css 的根布局，因此这里不能依赖 Tailwind/token，
// 全部用内联样式并硬编码品牌色值，保证即使样式管线失效也能正确渲染。
// 恢复回调是本版 Next 的 `unstable_retry`；防御式退回硬刷新。

import { useEffect, useRef } from "react";

export default function GlobalError({
  error,
  unstable_retry,
}: {
  error: Error & { digest?: string };
  unstable_retry?: () => void;
}) {
  const headingRef = useRef<HTMLHeadingElement>(null);

  useEffect(() => {
    console.error("[global-error]", error);
    headingRef.current?.focus();
  }, [error]);

  const retry = () => {
    if (typeof unstable_retry === "function") unstable_retry();
    else window.location.reload();
  };

  return (
    <html lang="zh-CN">
      <body
        style={{
          margin: 0,
          minHeight: "100vh",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          background: "#f7f6f4",
          fontFamily:
            "system-ui, -apple-system, 'Segoe UI', Roboto, 'PingFang SC', 'Microsoft YaHei', sans-serif",
          color: "#1b1b19",
        }}
      >
        <div
          role="alert"
          aria-labelledby="global-error-title"
          style={{ maxWidth: 460, padding: "0 24px", textAlign: "center" }}
        >
          <div style={{ fontSize: 40, lineHeight: 1 }} aria-hidden>
            ⚠️
          </div>
          <h1
            ref={headingRef}
            id="global-error-title"
            tabIndex={-1}
            style={{
              margin: "20px 0 0",
              fontSize: 22,
              fontWeight: 600,
              letterSpacing: "-0.03em",
              outline: "none",
            }}
          >
            页面加载失败
          </h1>
          <p
            style={{
              margin: "10px 0 0",
              fontSize: 14,
              lineHeight: 1.6,
              color: "#57564e",
            }}
          >
            应用遇到了一个未预期的错误。如果你刚提交了内容或完成任务，请先确认结果，再决定是否重试。
          </p>
          <div
            style={{
              marginTop: 24,
              display: "flex",
              gap: 12,
              justifyContent: "center",
            }}
          >
            <button
              type="button"
              onClick={retry}
              style={{
                height: 40,
                padding: "0 20px",
                borderRadius: 999,
                border: "none",
                background: "#1e1e1c",
                color: "#fff",
                fontSize: 14,
                fontWeight: 500,
                cursor: "pointer",
              }}
            >
              重试
            </button>
            <a
              href="/today"
              style={{
                height: 40,
                lineHeight: "40px",
                padding: "0 20px",
                borderRadius: 999,
                border: "1px solid #d6d4cf",
                background: "#fff",
                color: "#1b1b19",
                fontSize: 14,
                fontWeight: 500,
                textDecoration: "none",
              }}
            >
              返回今日
            </a>
          </div>
          {error.digest && (
            <p style={{ marginTop: 24, fontSize: 12, color: "#73726b" }}>
              错误编号：{error.digest}
            </p>
          )}
        </div>
      </body>
    </html>
  );
}
