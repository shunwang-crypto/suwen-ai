"use client";

// 图形验证码字段：验证码图片（点击可换）+ 刷新按钮 + 答案输入框。
// 契约：GET /api/auth/captcha -> { captcha_id, image(dataURL) }，验证码一次性使用。
// 后端未就绪/接口失败时优雅降级为「暂不可用，点击重试」，captchaId 置 null，
// 由调用方决定是否携带验证码字段提交（register 在服务不可用时可先不带）。
// 需要强制换一张（提交失败后）时，调用方换 key 重挂载即可。

import { useEffect, useRef, useState } from "react";

import { ApiError, apiJson } from "@/lib/api";

export interface CaptchaState {
  /** 当前验证码 id；服务不可用时为 null */
  captchaId: string | null;
  /** 用户填写的答案 */
  answer: string;
}

export const EMPTY_CAPTCHA: CaptchaState = { captchaId: null, answer: "" };

interface CaptchaOut {
  captcha_id: string;
  image: string;
}

const DEFAULT_INPUT_CLS =
  "w-full rounded-control border border-line-strong/80 bg-white/90 px-3.5 py-2.5 text-sm text-ink outline-none transition-[border-color,box-shadow] duration-200 placeholder:text-ink-muted focus:border-brand-500 focus:shadow-card focus:ring-4 focus:ring-brand-500/10";

export function CaptchaField({
  value,
  onChange,
  disabled = false,
  inputClassName = DEFAULT_INPUT_CLS,
  className = "",
}: {
  value: CaptchaState;
  onChange: (next: CaptchaState) => void;
  disabled?: boolean;
  inputClassName?: string;
  className?: string;
}) {
  const [image, setImage] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState<string | null>(null);
  // 点「刷新」+1 触发重新拉图（首个 setState 在 await 之后，仓库先例）
  const [epoch, setEpoch] = useState(0);

  // onChange 走 ref，拉图 effect 依赖保持稳定（父组件重渲染不触发重新拉图）
  const onChangeRef = useRef(onChange);
  useEffect(() => {
    onChangeRef.current = onChange;
  });

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const data = await apiJson<CaptchaOut>("/api/auth/captcha");
        if (!alive) return;
        setImage(data.image);
        setFailed(null);
        onChangeRef.current({ captchaId: data.captcha_id, answer: "" });
      } catch (err) {
        if (!alive) return;
        setImage(null);
        onChangeRef.current(EMPTY_CAPTCHA);
        setFailed(
          err instanceof ApiError && err.status !== 404
            ? "加载失败，点击重试"
            : "验证码暂不可用",
        );
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, [epoch]);

  const refresh = () => {
    setLoading(true);
    setEpoch((n) => n + 1);
  };

  return (
    <div className={className}>
      <span className="mb-1.5 block text-sm font-medium text-ink-secondary">
        验证码
      </span>
      <div className="flex items-stretch gap-2">
        <input
          type="text"
          inputMode="text"
          autoComplete="off"
          value={value.answer}
          disabled={disabled}
          onChange={(e) => onChange({ ...value, answer: e.target.value })}
          placeholder="图中字符"
          className={`min-w-0 flex-1 ${inputClassName}`}
          aria-label="验证码答案"
        />
        <button
          type="button"
          onClick={refresh}
          disabled={loading || disabled}
          title="看不清？点击换一张"
          className="relative h-[42px] w-28 shrink-0 overflow-hidden rounded-control border border-line-strong/80 bg-white/90 transition-colors hover:border-brand-300 disabled:cursor-not-allowed"
        >
          {loading ? (
            <span
              className="absolute inset-0 animate-pulse bg-surface-muted/70"
              aria-hidden
            />
          ) : image ? (
            // eslint-disable-next-line @next/next/no-img-element -- dataURL 图形验证码，无需 next/image 优化
            <img
              src={image}
              alt="图形验证码，点击刷新"
              className="h-full w-full object-cover"
            />
          ) : (
            <span className="flex h-full w-full items-center justify-center px-1 text-center text-xs leading-tight text-ink-muted">
              {failed ?? "点击加载"}
            </span>
          )}
        </button>
        <button
          type="button"
          onClick={refresh}
          disabled={loading || disabled}
          aria-label="刷新验证码"
          className="flex h-[42px] w-10 shrink-0 items-center justify-center rounded-control border border-line-strong/80 bg-white/90 text-ink-muted transition-colors hover:border-brand-300 hover:text-brand-600 disabled:cursor-not-allowed disabled:opacity-60"
        >
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.8"
            strokeLinecap="round"
            strokeLinejoin="round"
            className={`h-4 w-4 ${loading ? "animate-spin" : ""}`}
            aria-hidden
          >
            <path d="M3 12a9 9 0 1 0 3-6.7L3 8m0-5v5h5" />
          </svg>
        </button>
      </div>
    </div>
  );
}
