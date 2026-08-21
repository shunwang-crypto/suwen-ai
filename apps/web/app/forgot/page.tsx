"use client";

// 忘记密码页：邮箱 + 图形验证码 -> POST /api/auth/forgot（恒定 204）。
// 提交成功后恒定显示「若该邮箱已注册，重置链接已发送」——不泄露邮箱是否存在。

import Link from "next/link";
import { useState, type FormEvent } from "react";

import { AuthHero, BrandMark } from "@/components/ui/brand";
import {
  CaptchaField,
  EMPTY_CAPTCHA,
  type CaptchaState,
} from "@/components/ui/captcha";
import { ApiError, apiJson } from "@/lib/api";

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export default function ForgotPage() {
  const [email, setEmail] = useState("");
  const [captcha, setCaptcha] = useState<CaptchaState>(EMPTY_CAPTCHA);
  const [captchaEpoch, setCaptchaEpoch] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [submitted, setSubmitted] = useState(false);

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    if (!EMAIL_RE.test(email.trim())) {
      setError("请输入有效的邮箱地址");
      return;
    }
    if (!captcha.captchaId) {
      setError("验证码加载失败，请点击验证码图片重试");
      return;
    }
    if (!captcha.answer.trim()) {
      setError("请输入图中的验证码");
      return;
    }
    setSubmitting(true);
    try {
      await apiJson<undefined>("/api/auth/forgot", {
        method: "POST",
        body: {
          email: email.trim().toLowerCase(),
          captcha_id: captcha.captchaId,
          captcha_answer: captcha.answer.trim(),
        },
      });
      setSubmitted(true);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "网络异常，请稍后重试");
      // 验证码一次性使用：失败后强制换一张
      setCaptchaEpoch((n) => n + 1);
      setSubmitting(false);
    }
  };

  const inputCls =
    "w-full rounded-control border border-line-strong/80 bg-white/90 px-3.5 py-2.5 text-sm text-ink outline-none transition-[border-color,box-shadow] duration-200 placeholder:text-ink-muted focus:border-brand-500 focus:shadow-card focus:ring-4 focus:ring-brand-500/10";

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
              找回密码
            </h1>
            <p className="text-sm text-ink-muted">
              输入注册邮箱，我们会发送重置链接
            </p>
          </div>

          {submitted ? (
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
                  <path d="M4 6h16v12H4z" />
                  <path d="m4 7 8 6 8-6" />
                </svg>
              </span>
              <p className="text-sm font-semibold text-ink">
                若该邮箱已注册，重置链接已发送
              </p>
              <p className="mt-1.5 text-xs text-ink-muted">
                请查收 {email} 的收件箱（含垃圾邮件），链接短期内有效。
              </p>
              <Link
                href="/login"
                className="btn-brand mt-5 inline-block rounded-control px-6 py-2.5 text-sm font-semibold text-white shadow-card hover:shadow-floating"
              >
                返回登录
              </Link>
            </div>
          ) : (
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

              <label className="mb-4 block">
                <span className="mb-1.5 block text-sm font-medium text-ink-secondary">
                  邮箱
                </span>
                <input
                  type="email"
                  autoComplete="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  placeholder="you@example.com"
                  className={inputCls}
                  required
                />
              </label>

              <CaptchaField
                key={captchaEpoch}
                value={captcha}
                onChange={setCaptcha}
                disabled={submitting}
                inputClassName={inputCls}
                className="mb-6"
              />

              <button
                type="submit"
                disabled={submitting}
                className="btn-brand w-full rounded-control py-2.5 text-sm font-semibold text-white shadow-card hover:shadow-floating disabled:cursor-not-allowed disabled:opacity-60"
              >
                {submitting ? "发送中…" : "发送重置链接"}
              </button>
            </form>
          )}

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
