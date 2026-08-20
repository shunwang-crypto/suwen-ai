import Link from "next/link";

export default function PrivacyPage() {
  return (
    <main className="mx-auto min-h-dvh max-w-3xl px-6 py-16 text-slate-700">
      <h1 className="text-3xl font-semibold text-slate-900">苏问 AI课堂 隐私政策</h1>
      <p className="mt-6">我们使用邮箱用于账号登录、邮箱所有权验证和密码找回，不会把验证码或密码写入浏览器存储。</p>
      <p className="mt-4">你提交的学习资料只在授权范围内用于资料处理、练习和学习报告；必要的服务供应商只接收完成服务所需的数据。</p>
      <p className="mt-4">部分 AI 请求可能经由第三方免费模型通道处理，第三方可能将请求用于服务改进；我们不承诺零保留或完全私密。请不要上传身份证、成绩单姓名页或其他敏感资料。TrendDAO 的 public 认证方式也不是其官方保证的长期生产认证方式。</p>
      <p className="mt-4">你可以通过账户功能管理或删除个人数据。安全事件和政策变更会通过正式渠道说明。</p>
      <Link href="/register" className="mt-8 inline-block text-brand-600 hover:underline">返回注册</Link>
    </main>
  );
}
