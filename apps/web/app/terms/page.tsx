import Link from "next/link";

export default function TermsPage() {
  return (
    <main className="mx-auto min-h-dvh max-w-3xl px-6 py-16 text-slate-700">
      <h1 className="text-3xl font-semibold text-slate-900">EduAgents 用户协议</h1>
      <p className="mt-6">使用 EduAgents 即表示你会合法、合理地使用学习资料、练习和 AI 功能。</p>
      <p className="mt-4">账号由本人使用；请勿共享密码、上传无权处理的资料或利用服务骚扰他人。</p>
      <p className="mt-4">服务可能因维护、模型供应商或安全策略暂时不可用。协议更新会在本页面公布。</p>
      <Link href="/register" className="mt-8 inline-block text-brand-600 hover:underline">返回注册</Link>
    </main>
  );
}
