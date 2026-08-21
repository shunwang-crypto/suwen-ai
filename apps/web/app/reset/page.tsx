"use client";

// 重置密码页：读 ?token=（邮件链接）+ 新密码二次确认 -> POST /api/auth/reset（204）。
// 失败 code=RESET_TOKEN_INVALID -> 提示链接失效并引导重新申请；成功引导去登录。
// useSearchParams 需包 Suspense（生产构建要求，同 chat 页先例）。

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useState, type FormEvent } from "react";

import { AuthHero, BrandMark } from "@/components/ui/brand";
import { PasswordInput } from "@/components/ui/password-input";
import { ApiError, apiJson } from "@/lib/api";

function ResetForm() {
  const searchParams = useSearchParams();
  const token = searchParams.get("token") ?? "";

  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [tokenInvalid, setTokenInvalid] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [done, setDone] = useState(false);

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    if (password.length < 8) {
      setError("密码长度至少 8 位");
      return;
    }
    if (password.length > 128) {
      setError("密码长度不能超过 128 位");
      return;
    }
    if (password !== confirm) {
      setError("两次输入的密码不一致");
      return;
    }
    setSubmitting(true);
    try {
      await apiJson<undefined>("/api/auth/reset", {
        method: "POST",
        body: { token, new_password: password },
      });
      setDone(true);
    } catch (err) {
      if (err instanceof ApiError && err.code === "RESET_TOKEN_INVALID") {
        setTokenInvalid(true);
      } else {
        setError(
          err instanceof ApiError ? err.message : "网络异常，请稍后重试",
        );
      }
      setSubmitting(false);
    }
  };

  const inputCls =
    "w-full rounded-control border border-line-strong/80 bg-white/90 px-3.5 py-2.5 text-sm text-ink outline-none transition-[border-color,box-shadow] duration-200 placeholder:text-ink-muted focus:border-brand-500 focus:shadow-card focus:ring-4 focus:ring-brand-500/10";

  // 无 token 或后端判定 token 失效：统一失效态
  if (!token || tokenInvalid) {
    return (
      <div className="glass animate-fade-up rounded-card border border-white/70 p-6 text-center shadow-floating ring-1 ring-line-strong/5">
        <span className="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-full bg-warning-50 text-warning-600">
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.8"
            strokeLinecap="round"
            strokeLinejoin="round"
            className="h-6 w-6"
            aria-hidden
          >
            <path d="M13.7 4.9a7 7 0 0 1 5.4 5.4L21 12l-1.9 1.7a7 7 0 0 1-5.4 5.4L12 21l-1.7-1.9a7 7 0 0 1-5.4-5.4L3 12l1.9-1.7a7 7 0 0 1 5.4-5.4L12 3l1.7 1.9Z" />
            <path d="M12 9v4m0 3h.01" />
          </svg>
        </span>
        <p className="text-sm font-semibold text-ink">
          重置链接无效或已过期
        </p>
        <p className="mt-1.5 text-xs text-ink-muted">
          出于安全考虑，重置链接只能使用一次且短期内有效，请重新申请。
        </p>
        <Link
          href="/forgot"
          className="btn-brand mt-5 inline-block rounded-control px-6 py-2.5 text-sm font-semibold text-white shadow-card hover:shadow-floating"
        >
          重新申请重置链接
        </Link>
      </div>
    );
  }

  if (done) {
    return (
      <div className="glass animate-fade-up rounded-card border border-white/70 p-6 text-center shadow-floating ring-1 ring-line-strong/5">
        <span className="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-full bg-success-50 text-success-600">
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
            className="h-6 w-6"
            aria-hidden
          >
            <path d="M20 6 9 17l-5-5" />
          </svg>
        </span>
        <p className="text-sm font-semibold text-ink">密码已重置</p>
        <p className="mt-1.5 text-xs text-ink-muted">
          请使用新密码登录你的账号。
        </p>
        <Link
          href="/login"
          className="btn-brand mt-5 inline-block rounded-control px-6 py-2.5 text-sm font-semibold text-white shadow-card hover:shadow-floating"
        >
          去登录
        </Link>
      </div>
    );
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="glass rounded-card border border-white/70 p-6 shadow-floating ring-1 ring-line-strong/5"
      noValidate
    >
      {error && (
        <div
          role="alert"
          className="mb-4 animate-pop rounded-control border border-danger-500/25 bg-danger-50 px-3 py-2 text-sm text-danger-700"
        >
          {error}
        </div>
      )}

      <div className="mb-4">
        <label htmlFor="reset-password" className="mb-1.5 block text-sm font-medium text-ink-secondary">
          新密码
        </label>
        <PasswordInput
          id="reset-password"
          name="password"
          autoComplete="new-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder="至少 8 位"
          className={inputCls}
          required
        />
      </div>

      <div className="mb-6">
        <label htmlFor="reset-password-confirm" className="mb-1.5 block text-sm font-medium text-ink-secondary">
          确认新密码
        </label>
        <PasswordInput
          id="reset-password-confirm"
          name="password-confirmation"
          autoComplete="new-password"
          value={confirm}
          onChange={(e) => setConfirm(e.target.value)}
          placeholder="再次输入新密码"
          className={inputCls}
          required
        />
      </div>

      <button
        type="submit"
        disabled={submitting}
        className="btn-brand w-full rounded-control py-2.5 text-sm font-semibold text-white shadow-card hover:shadow-floating disabled:cursor-not-allowed disabled:opacity-60"
      >
        {submitting ? "重置中…" : "重置密码"}
      </button>
    </form>
  );
}

export default function ResetPage() {
  return (
    <div className="grid min-h-dvh lg:grid-cols-2">
      {/* 左侧品牌 hero 区（lg 以上显示） */}
      <AuthHero />

      {/* 右侧表单区：浅色 aurora 光晕底 */}
      <div className="flex items-center justify-center bg-aurora px-4 py-10">
        <div className="w-full max-w-md animate-fade-up">
          <div className="mb-8 flex flex-col items-center gap-2">
            <BrandMark size={48} />
            <h1 className="text-2xl font-semibold tracking-tight text-ink">
              重置密码
            </h1>
            <p className="text-sm text-ink-muted">设置一个新密码，然后重新登录</p>
          </div>

          <Suspense
            fallback={
              <div className="glass rounded-card border border-white/70 p-6 text-center text-sm text-ink-muted shadow-floating ring-1 ring-line-strong/5">
                加载中…
              </div>
            }
          >
            <ResetForm />
          </Suspense>

          <p className="mt-6 text-center text-sm text-ink-muted">
            想起密码了？
            <Link
              href="/login"
              className="ml-1 font-medium text-brand-600 transition-colors hover:text-brand-700"
            >
              去登录
            </Link>
          </p>
        </div>
      </div>
    </div>
  );
}
