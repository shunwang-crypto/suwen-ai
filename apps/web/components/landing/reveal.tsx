"use client";

// 滚动入场轻实现：IntersectionObserver 命中一次即加 .fx-visible（fade-up）。
// prefers-reduced-motion 场景由 globals.css 的媒体查询直接置为可见，无 JS 分支。

import { useEffect, useRef, type CSSProperties, type ReactNode } from "react";

export function Reveal({
  children,
  className = "",
  delay = 0,
}: {
  children: ReactNode;
  className?: string;
  /** 入场延迟（ms），映射到 CSS 变量 --fx-delay，逐个错峰 */
  delay?: number;
}) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (typeof IntersectionObserver === "undefined") {
      el.classList.add("fx-visible");
      return;
    }
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            el.classList.add("fx-visible");
            observer.disconnect();
          }
        }
      },
      { threshold: 0.15, rootMargin: "0px 0px -40px 0px" },
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  return (
    <div
      ref={ref}
      className={`fx-reveal ${className}`}
      style={{ "--fx-delay": `${delay}ms` } as CSSProperties}
    >
      {children}
    </div>
  );
}
