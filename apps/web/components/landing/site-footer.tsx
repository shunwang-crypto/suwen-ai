// 落地页页脚：品牌 + 演示项目声明。

import Link from "next/link";

import { BrandMark } from "@/components/ui/brand";

export function SiteFooter() {
  return (
    <footer className="border-t border-slate-200 bg-white/70">
      <div className="mx-auto flex w-full max-w-6xl flex-col items-center gap-4 px-5 py-10 text-center md:flex-row md:justify-between md:text-left">
        <div>
          <span className="flex items-center justify-center md:justify-start">
            <BrandMark withWordmark />
          </span>
          <p className="mt-2 max-w-md text-xs leading-relaxed text-slate-400">
            演示项目：本站为多智能体学习平台的竞赛演示，页面数据来自仿真实验与示例内容，不代表真实用户数据。
          </p>
        </div>
        <div className="flex items-center gap-4 text-sm text-slate-500">
          <Link href="/login" className="transition-colors hover:text-indigo-600">
            登录
          </Link>
          <Link
            href="/register"
            className="transition-colors hover:text-indigo-600"
          >
            注册
          </Link>
        </div>
      </div>
    </footer>
  );
}
