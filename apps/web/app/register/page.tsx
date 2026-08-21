"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState, type FormEvent } from "react";

import { AuthHero, BrandMark } from "@/components/ui/brand";
import {
  CaptchaField,
  EMPTY_CAPTCHA,
  type CaptchaState,
} from "@/components/ui/captcha";
import { PasswordInput } from "@/components/ui/password-input";
import { ApiError, apiJson } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import type { PublicRegistrationConfigOut, RegistrationMode } from "@/lib/types";
import { useApiResource } from "@/lib/use-api-resource";

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export default function RegisterPage() {
  const router = useRouter();
  const { register } = useAuth();
  const [displayName, setDisplayName] = useState("");
  const [email, setEmail] = useState("");
  const [emailCode, setEmailCode] = useState("");
  const [inviteCode, setInviteCode] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [sendingCode, setSendingCode] = useState(false);
  const [cooldown, setCooldown] = useState(0);
  const [showCaptcha, setShowCaptcha] = useState(false);
  const [acceptedPolicies, setAcceptedPolicies] = useState(false);
  // 图形验证码（契约：register 带 captcha_id/captcha_answer）。
  // 服务未就绪（captchaId 为 null）时不带字段提交，由后端裁决。
  const [captcha, setCaptcha] = useState<CaptchaState>(EMPTY_CAPTCHA);
  const [captchaEpoch, setCaptchaEpoch] = useState(0);

  // 注册模式（A5 收敛: useApiResource 派生; 加载中为 null → 关闭态占位,
  // 配置拉取失败必须落入 closed，绝不能意外放开注册）
  const configRes = useApiResource<PublicRegistrationConfigOut>("/api/config/public");
  const registrationMode: RegistrationMode | null = configRes.error
    ? "closed"
    : (configRes.data?.registration_mode ?? null);

  useEffect(() => {
    if (cooldown <= 0) return;
    const timer = window.setInterval(() => {
      setCooldown((value) => Math.max(0, value - 1));
    }, 1000);
    return () => window.clearInterval(timer);
  }, [cooldown]);

  const handleEmailChange = (value: string) => {
    setEmail(value);
    setEmailCode("");
    setCooldown(0);
    setShowCaptcha(false);
    setCaptcha(EMPTY_CAPTCHA);
    setCaptchaEpoch((n) => n + 1);
  };

  const handleSendCode = async () => {
    setError(null);
    if (!EMAIL_RE.test(email.trim())) {
      setError("请输入有效的邮箱地址");
      return;
    }
    if (!showCaptcha) {
      setShowCaptcha(true);
      setError("请完成图形验证码后再次点击发送");
      return;
    }
    if (!captcha.captchaId || !captcha.answer.trim()) {
      setError("请输入图中的验证码");
      return;
    }
    setSendingCode(true);
    try {
      await apiJson<{ accepted: boolean }>("/api/auth/register/email-code", {
        method: "POST",
        body: {
          email: email.trim().toLowerCase(),
          captcha_id: captcha.captchaId,
          captcha_answer: captcha.answer.trim(),
        },
      });
      setCooldown(60);
      setCaptcha(EMPTY_CAPTCHA);
      setCaptchaEpoch((n) => n + 1);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "邮件发送失败，请稍后重试");
      setCaptchaEpoch((n) => n + 1);
    } finally {
      setSendingCode(false);
    }
  };

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    if (!displayName.trim()) {
      setError("请输入姓名或昵称");
      return;
    }
    if (!EMAIL_RE.test(email.trim())) {
      setError("请输入有效的邮箱地址");
      return;
    }
    if (!/^\d{6}$/.test(emailCode)) {
      setError("请输入 6 位邮箱验证码");
      return;
    }
    if (registrationMode === "invite" && !inviteCode.trim()) {
      setError("请输入邀请码");
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
    if (!acceptedPolicies) {
      setError("请先同意用户协议和隐私政策");
      return;
    }
    setSubmitting(true);
    try {
      // 注册成功后自动登录
      await register(
        email,
        password,
        displayName.trim(),
        emailCode,
        registrationMode === "invite" ? inviteCode.trim() : undefined,
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
    "w-full rounded-control border border-line-strong/80 bg-white/90 px-3.5 py-2.5 text-sm text-ink outline-none transition-[border-color,box-shadow] duration-200 placeholder:text-ink-muted focus:border-brand-500 focus:shadow-card focus:ring-4 focus:ring-brand-500/10";

  if (registrationMode === null || registrationMode === "closed") {
    return (
      <div className="grid min-h-dvh lg:grid-cols-2">
        <AuthHero />
        <div className="flex items-center justify-center bg-aurora px-4 py-10">
          <div className="w-full max-w-md text-center">
            <BrandMark size={48} />
            <h1 className="mt-4 text-2xl font-semibold tracking-tight text-ink">
              {registrationMode === null ? "正在检查注册状态" : "当前暂未开放注册"}
            </h1>
            <p className="mt-2 text-sm text-ink-muted">
              {registrationMode === null
                ? "请稍候…"
                : "如需注册，请等待管理员发出邀请。"}
            </p>
            <Link
              href="/login"
              className="mt-6 inline-flex rounded-control bg-brand-600 px-4 py-2 text-sm font-semibold text-white hover:bg-brand-700"
            >
              去登录
            </Link>
          </div>
        </div>
      </div>
    );
  }

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
              注册苏问
            </h1>
            <p className="text-sm text-ink-muted">
              创建账号，开始你的个性化学习
            </p>
          </div>

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
                姓名或昵称
              </span>
              <input
                type="text"
                autoComplete="name"
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                maxLength={80}
                placeholder="例如：林小溪"
                className={inputCls}
                required
              />
            </label>

            <label className="mb-4 block">
              <span className="mb-1.5 block text-sm font-medium text-ink-secondary">
                邮箱
              </span>
              <input
                type="email"
                name="email"
                autoComplete="email"
                value={email}
                onChange={(e) => handleEmailChange(e.target.value)}
                placeholder="you@example.com"
                className={inputCls}
                required
              />
            </label>

            <div className="mb-4">
              <div className="flex gap-2">
                <input
                  type="text"
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  value={emailCode}
                  onChange={(e) => setEmailCode(e.target.value.replace(/\D/g, "").slice(0, 6))}
                  maxLength={6}
                  placeholder="6 位邮箱验证码"
                  className={inputCls}
                  aria-label="邮箱验证码"
                  required
                />
                <button
                  type="button"
                  onClick={handleSendCode}
                  disabled={sendingCode || cooldown > 0}
                  className="w-32 shrink-0 rounded-control border border-brand-200 bg-brand-50 px-2 text-xs font-semibold text-brand-700 transition-colors hover:bg-brand-100 disabled:cursor-not-allowed disabled:opacity-60"
                >
                  {sendingCode ? "发送中…" : cooldown > 0 ? `${cooldown}s 后重发` : "发送验证码"}
                </button>
              </div>
              <p className="mt-1 text-xs text-ink-muted">验证码 10 分钟内有效；请先完成图形验证码。</p>
            </div>

            {showCaptcha && (
              <CaptchaField
                key={captchaEpoch}
                value={captcha}
                onChange={setCaptcha}
                disabled={sendingCode || submitting}
                inputClassName={inputCls}
                className="mb-6"
              />
            )}

            {registrationMode === "invite" && (
              <label className="mb-4 block">
                <span className="mb-1.5 block text-sm font-medium text-ink-secondary">
                  邀请码
                </span>
                <input
                  type="text"
                  autoComplete="off"
                  value={inviteCode}
                  onChange={(e) => setInviteCode(e.target.value.toUpperCase())}
                  maxLength={128}
                  placeholder="请输入管理员提供的邀请码"
                  className={inputCls}
                  required
                />
                <p className="mt-1 text-xs text-ink-muted">
                  邀请码只能使用一次，请勿公开分享。
                </p>
              </label>
            )}

            <div className="mb-4">
              <label htmlFor="register-password" className="mb-1.5 block text-sm font-medium text-ink-secondary">
                密码
              </label>
              <PasswordInput
                id="register-password"
                name="password"
                autoComplete="new-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="至少 8 位"
                className={inputCls}
                required
              />
            </div>

            <div className="mb-4">
              <label htmlFor="register-password-confirm" className="mb-1.5 block text-sm font-medium text-ink-secondary">
                确认密码
              </label>
              <PasswordInput
                id="register-password-confirm"
                name="password-confirmation"
                autoComplete="new-password"
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
                placeholder="再次输入密码"
                className={inputCls}
                required
              />
            </div>

            <label className="mb-6 flex items-start gap-2 text-xs leading-5 text-ink-secondary">
              <input
                type="checkbox"
                checked={acceptedPolicies}
                onChange={(e) => setAcceptedPolicies(e.target.checked)}
                className="mt-1 h-4 w-4 rounded border-line-strong text-brand-600 focus:ring-brand-500"
              />
              <span>
                我已阅读并同意 <Link href="/terms" className="text-brand-600 hover:underline">用户协议</Link>
                和 <Link href="/privacy" className="text-brand-600 hover:underline">隐私政策</Link>
              </span>
            </label>

            <button
              type="submit"
              disabled={submitting}
              className="btn-brand w-full rounded-control py-2.5 text-sm font-semibold text-white shadow-card hover:shadow-floating disabled:cursor-not-allowed disabled:opacity-60"
            >
              {submitting ? "注册中…" : "注册并登录"}
            </button>
          </form>

          <p className="mt-6 text-center text-sm text-ink-muted">
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
