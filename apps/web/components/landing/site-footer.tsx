// 落地页页脚：品牌定位 + 用户入口。

import Link from "next/link";

import { BrandMark } from "@/components/ui/brand";

export function SiteFooter() {
  return (
    <footer className="border-t border-line bg-white/70">
      <div className="mx-auto flex w-full max-w-6xl flex-col items-center gap-4 px-5 py-10 text-center md:flex-row md:justify-between md:text-left">
        <div>
          <span className="flex items-center justify-center md:justify-start">
            <BrandMark withWordmark />
          </span>
          <p className="mt-2 max-w-md text-xs leading-relaxed text-ink-muted">
            可溯源问答、苏格拉底式辅导与个性化复习，让每次学习都有依据、每次进步都有记录。
          </p>
        </div>
        <div className="flex items-center gap-4 text-sm text-ink-muted">
          <Link href="/login" className="transition-colors hover:text-indigo-600">
            登录
          </Link>
          <Link
            href="/register"
            className="transition-colors hover:text-indigo-600"
          >
            注册
          </Link>
          <Link
            href="/terms"
            className="transition-colors hover:text-indigo-600"
          >
            用户协议
          </Link>
          <Link
            href="/privacy"
            className="transition-colors hover:text-indigo-600"
          >
            隐私政策
          </Link>
        </div>
      </div>
    </footer>
  );
}
