"use client";

import Link from "next/link";
import { BookOpen, FileUp, Settings2 } from "lucide-react";

import { BrandMark } from "@/components/ui/brand";

export default function OnboardingPage() {
  return (
    <div className="min-h-dvh bg-[#f6f7f9]">
      <header className="flex items-center justify-between border-b border-line bg-white px-8 py-4">
        <BrandMark size={32} withWordmark className="text-ink" />
        <Link href="/today" className="text-body-sm font-medium text-ink-muted hover:text-ink">
          稍后再说
        </Link>
      </header>

      <main className="mx-auto flex min-h-[calc(100dvh-65px)] max-w-[1040px] items-center px-8 py-12">
        <section className="w-full">
          <p className="text-body-sm font-semibold text-brand-700">开始学习</p>
          <h1 className="mt-2 max-w-2xl text-[34px] font-semibold leading-tight text-ink">
            选择这次学习从哪里开始
          </h1>
          <p className="mt-3 max-w-2xl text-body leading-7 text-ink-muted">
            有课程资料时，系统会把计划、练习和掌握度限定在资料范围内；没有资料也可以先制定目标起步计划。
          </p>

          <div className="mt-8 grid gap-4 md:grid-cols-2">
            <Link href="/courses" className="group rounded-[8px] border border-line-strong bg-white p-6 shadow-subtle transition-colors hover:border-brand-400">
              <span className="flex h-10 w-10 items-center justify-center rounded-[8px] bg-brand-50 text-brand-700">
                <FileUp size={20} aria-hidden />
              </span>
              <h2 className="mt-5 text-[18px] font-semibold text-ink group-hover:text-brand-700">上传资料开始</h2>
              <p className="mt-2 text-body-sm leading-6 text-ink-muted">建立课程空间，上传讲义、教材或题目资料，再生成有真实来源的学习计划。</p>
              <span className="mt-5 inline-flex items-center gap-2 text-body-sm font-semibold text-brand-700">进入课程资料 <BookOpen size={16} aria-hidden /></span>
            </Link>

            <Link href="/plans?create=1&mode=goal_only" className="group rounded-[8px] border border-line-strong bg-white p-6 shadow-subtle transition-colors hover:border-warning-400">
              <span className="flex h-10 w-10 items-center justify-center rounded-[8px] bg-warning-50 text-warning-700">
                <BookOpen size={20} aria-hidden />
              </span>
              <h2 className="mt-5 text-[18px] font-semibold text-ink group-hover:text-warning-800">暂时没有资料，先制定计划</h2>
              <p className="mt-2 text-body-sm leading-6 text-ink-muted">先拆解目标、安排基线练习和每日任务。临时概念不会进入正式知识图谱或掌握度。</p>
              <span className="mt-5 inline-flex items-center gap-2 text-body-sm font-semibold text-warning-800">制定目标起步计划 <BookOpen size={16} aria-hidden /></span>
            </Link>
          </div>

          <p className="mt-6 flex items-center gap-2 text-caption text-ink-muted">
            <Settings2 size={14} aria-hidden />
            讲解风格、每日时长和兴趣偏好可随时在设置中调整，不会阻塞首次学习。
          </p>
        </section>
      </main>
    </div>
  );
}
