"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";

import { AuthHero, BrandMark } from "@/components/ui/brand";
import {
  CaptchaField,
  EMPTY_CAPTCHA,
  type CaptchaState,
} from "@/components/ui/captcha";
import { PasswordInput } from "@/components/ui/password-input";
import { ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const REMEMBERED_EMAIL_KEY = "suwen:remembered-login-email";

function readRememberedEmail(): string {
  if (typeof window === "undefined") return "";
  return window.localStorage.getItem(REMEMBERED_EMAIL_KEY) ?? "";
}

export default function LoginPage() {
  const router = useRouter();
  const { login } = useAuth();
  const [email, setEmail] = useState(readRememberedEmail);
  const [rememberEmail, setRememberEmail] = useState(
    () => readRememberedEmail().length > 0,
  );
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  // 后端回 CAPTCHA_REQUIRED 时内联展开验证码行（验证码一次性，失败换 key 重挂载刷新）
  const [needCaptcha, setNeedCaptcha] = useState(false);
  const [captcha, setCaptcha] = useState<CaptchaState>(EMPTY_CAPTCHA);
  const [captchaEpoch, setCaptchaEpoch] = useState(0);

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    if (!EMAIL_RE.test(email.trim())) {
      setError("请输入有效的邮箱地址");
      return;
    }
    if (password.length < 8) {
      setError("密码长度至少 8 位");
      return;
    }
    if (needCaptcha && captcha.captchaId && !captcha.answer.trim()) {
      setError("请输入图中的验证码");
      return;
    }
    setSubmitting(true);
    try {
      await login(
        email.trim().toLowerCase(),
        password,
        needCaptcha && captcha.captchaId
          ? {
              captcha_id: captcha.captchaId,
              captcha_answer: captcha.answer.trim(),
            }
          : undefined,
      );
      if (rememberEmail) {
        window.localStorage.setItem(REMEMBERED_EMAIL_KEY, email.trim().toLowerCase());
      } else {
        window.localStorage.removeItem(REMEMBERED_EMAIL_KEY);
      }
      router.replace("/today");
    } catch (err) {
      if (err instanceof ApiError && err.code === "CAPTCHA_REQUIRED") {
        setNeedCaptcha(true);
        setError("需要完成图形验证码后再登录");
      } else {
        setError(err instanceof ApiError ? err.message : "网络异常，请稍后重试");
      }
      // 验证码一次性使用：失败后强制换一张
      if (needCaptcha) setCaptchaEpoch((n) => n + 1);
      setSubmitting(false);
    }
  };

  const inputCls =
    "w-full rounded-control border border-slate-300/80 bg-white/90 px-3.5 py-2.5 text-sm text-slate-900 outline-none transition-[border-color,box-shadow] duration-200 placeholder:text-slate-400 focus:border-brand-500 focus:shadow-card focus:ring-4 focus:ring-brand-500/10";

  return (
    <div className="grid min-h-dvh lg:grid-cols-2">
      {/* 左侧品牌 hero 区（lg 以上显示） */}
      <AuthHero />

      {/* 右侧表单区：浅色 aurora 光晕底 */}
      <div className="flex items-center justify-center bg-aurora px-4 py-10">
        <div className="w-full max-w-md animate-fade-up">
          <div className="mb-8 flex flex-col items-center gap-2">
            <BrandMark size={48} />
            <h1 className="text-2xl font-semibold tracking-tight text-slate-900">
              登录苏问
            </h1>
            <p className="text-sm text-slate-500">
              多智能体驱动的个性化学习平台
            </p>
          </div>

          <form
            onSubmit={handleSubmit}
            className="glass rounded-card border border-white/70 p-6 shadow-floating ring-1 ring-slate-900/5"
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
              <span className="mb-1.5 block text-sm font-medium text-slate-700">
                邮箱
              </span>
              <input
                type="email"
                name="email"
                autoComplete="username"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="you@example.com"
                className={inputCls}
                required
              />
            </label>

            <div className="mb-2">
              <label htmlFor="login-password" className="mb-1.5 block text-sm font-medium text-slate-700">
                密码
              </label>
              <PasswordInput
                id="login-password"
                name="password"
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="至少 8 位"
                className={inputCls}
                required
              />
            </div>

            <div className="mb-4 flex items-center justify-between gap-3">
              <label className="flex cursor-pointer items-center gap-2 text-xs text-slate-500">
                <input
                  type="checkbox"
                  checked={rememberEmail}
                  onChange={(event) => {
                    const checked = event.target.checked;
                    setRememberEmail(checked);
                    if (!checked) {
                      window.localStorage.removeItem(REMEMBERED_EMAIL_KEY);
                    }
                  }}
                  className="h-3.5 w-3.5 rounded border-slate-300 accent-brand-600"
                />
                记住账号
              </label>
              <Link
                href="/forgot"
                className="text-xs font-medium text-slate-400 transition-colors hover:text-brand-600"
              >
                忘记密码？
              </Link>
            </div>

            {needCaptcha && (
              <CaptchaField
                key={captchaEpoch}
                value={captcha}
                onChange={setCaptcha}
                disabled={submitting}
                inputClassName={inputCls}
                className="mb-4 animate-pop"
              />
            )}

            <button
              type="submit"
              disabled={submitting}
              className="btn-brand w-full rounded-control py-2.5 text-sm font-semibold text-white shadow-card hover:shadow-floating disabled:cursor-not-allowed disabled:opacity-60"
            >
              {submitting ? "登录中…" : "登录"}
            </button>
          </form>

          <p className="mt-6 text-center text-sm text-slate-500">
            还没有账号？
            <Link
              href="/register"
              className="ml-1 font-medium text-brand-600 transition-colors hover:text-brand-700"
            >
              立即注册
            </Link>
          </p>
        </div>
      </div>
    </div>
  );
}
