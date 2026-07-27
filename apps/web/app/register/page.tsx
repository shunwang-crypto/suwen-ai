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
import { ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export default function RegisterPage() {
  const router = useRouter();
  const { register } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  // 图形验证码（契约：register 带 captcha_id/captcha_answer）。
  // 服务未就绪（captchaId 为 null）时不带字段提交，由后端裁决。
  const [captcha, setCaptcha] = useState<CaptchaState>(EMPTY_CAPTCHA);
  const [captchaEpoch, setCaptchaEpoch] = useState(0);

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    if (!EMAIL_RE.test(email)) {
      setError("请输入有效的邮箱地址");
      return;
    }
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
    if (captcha.captchaId && !captcha.answer.trim()) {
      setError("请输入图中的验证码");
      return;
    }
    setSubmitting(true);
    try {
      // 注册成功后自动登录
      await register(
        email,
        password,
        captcha.captchaId
          ? {
              captcha_id: captcha.captchaId,
              captcha_answer: captcha.answer.trim(),
            }
          : undefined,
      );
      // G12 冷启动：新注册用户先走 Onboarding 三步偏好表单（可跳过）；登录页不跳
      router.replace("/onboarding");
    } catch (err) {
      if (err instanceof ApiError && err.code === "CAPTCHA_REQUIRED") {
        // 注册已成功但自动登录被要求验证码：去登录页完成
        router.replace("/login");
        return;
      }
      setError(err instanceof ApiError ? err.message : "网络异常，请稍后重试");
      // 验证码一次性使用：失败后强制换一张
      setCaptchaEpoch((n) => n + 1);
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
              注册 EduAgents
            </h1>
            <p className="text-sm text-slate-500">
              创建账号，开始你的个性化学习
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
                autoComplete="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="you@example.com"
                className={inputCls}
                required
              />
            </label>

            <label className="mb-4 block">
              <span className="mb-1.5 block text-sm font-medium text-slate-700">
                密码
              </span>
              <input
                type="password"
                autoComplete="new-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="至少 8 位"
                className={inputCls}
                required
              />
            </label>

            <label className="mb-4 block">
              <span className="mb-1.5 block text-sm font-medium text-slate-700">
                确认密码
              </span>
              <input
                type="password"
                autoComplete="new-password"
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
                placeholder="再次输入密码"
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
              {submitting ? "注册中…" : "注册并登录"}
            </button>
          </form>

          <p className="mt-6 text-center text-sm text-slate-500">
            已有账号？
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
