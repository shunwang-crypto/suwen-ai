"use client";

// 教师组卷导出（U-A4，仅 teacher/admin）：列出可导出的卷子，
// 「导出 JSON」走 Blob + createObjectURL 本地下载（不落服务端文件），
// 「打印视图」在新窗口写一份自包含 A4 打印 HTML（题目页 + 分页的答案页），
// 打印 HTML 里的用户内容一律 escapeHtml 转义。

import { useEffect, useState } from "react";

import {
  Card,
  EmptyState,
  ErrorBanner,
  LoadingState,
  Spinner,
  fmtDateTime,
} from "@/components/common";
import { ApiError, apiJson } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { MathText, renderMathInText } from "@/lib/katex";
import type {
  TeacherExportJson,
  TeacherExportPrint,
  TeacherQuizItem,
} from "@/lib/types";

const DIFFICULTY_ORDER: { key: string; label: string; cls: string }[] = [
  { key: "easy", label: "易", cls: "bg-emerald-50 text-emerald-700" },
  { key: "mid", label: "中", cls: "bg-amber-50 text-amber-700" },
  { key: "hard", label: "难", cls: "bg-red-50 text-red-700" },
];

/** 打印 HTML 里所有用户内容都要过这里（防 XSS / 防标签串位）。 */
function escapeHtml(raw: unknown): string {
  return String(raw ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/** 打印窗口不加载应用 CSS，使用浏览器原生 MathML 输出并转义全部非公式文本。 */
function printMath(raw: unknown): string {
  return renderMathInText(String(raw ?? ""), { output: "mathml" });
}

/** 选项编号：后端已自带 "A ..." 前缀时不重复编号。 */
function optionLabel(text: string, i: number): string {
  const letter = String.fromCharCode(65 + i);
  const t = String(text ?? "").trim();
  if (new RegExp(`^${letter}([.、．:：)）\\]]|\\s)`).test(t)) return t;
  return `${letter}. ${t}`;
}

/** 文件名安全化：去掉路径分隔符与 Windows 非法字符。 */
function safeFileName(raw: string): string {
  return (
    String(raw ?? "")
      .replace(/[\\/:*?"<>|\s]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 40) || "quiz"
  );
}

function buildPrintHtml(data: TeacherExportPrint): string {
  const paper = data.paper;
  const items = paper.items ?? [];
  const answers = data.answer_key?.items ?? [];
  const exportedAt = fmtDateTime(data.exported_at);

  const questionsHtml = items
    .map((it) => {
      const opts =
        it.options && it.options.length > 0
          ? `<ol class="opts">${it.options
              .map((o, i) => `<li>${printMath(optionLabel(o, i))}</li>`)
              .join("")}</ol>`
          : `<div class="blank">答：______________________________________________</div>`;
      return `<section class="q">
  <p class="stem"><span class="no">${escapeHtml(it.index)}.</span> ${printMath(it.stem)}
    <span class="meta">（${escapeHtml(it.type_label)} · ${escapeHtml(
      it.difficulty_label,
    )} · ${escapeHtml(it.points)} 分）</span></p>
  ${opts}
</section>`;
    })
    .join("\n");

  const answersHtml = answers
    .map(
      (a) => `<section class="a">
  <p class="ans"><span class="no">${escapeHtml(a.index)}.</span> 答案：${printMath(
    a.answer,
  )}</p>
  <p class="exp">解析：${printMath(a.explanation) || "—"}</p>
</section>`,
    )
    .join("\n");

  return `<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>${escapeHtml(paper.title)}</title>
<style>
  @page { size: A4; margin: 18mm; }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: "Songti SC", "SimSun", "Noto Serif CJK SC", serif;
    font-size: 12pt;
    line-height: 1.7;
    color: #111827;
  }
  h1 { font-size: 18pt; text-align: center; margin: 0 0 6pt; }
  .sub { text-align: center; font-size: 10pt; color: #4b5563; margin: 0 0 4pt; }
  .instructions {
    margin: 10pt 0 16pt;
    padding: 8pt 10pt;
    border: 1px solid #d1d5db;
    font-size: 10.5pt;
    color: #374151;
  }
  .q { margin: 0 0 13pt; page-break-inside: avoid; break-inside: avoid; }
  .stem { margin: 0; font-weight: 400; }
  .stem .no { font-weight: 700; }
  .stem .meta { font-size: 9.5pt; color: #6b7280; font-weight: 400; }
  .opts { list-style: none; margin: 3pt 0 0; padding-left: 22pt; }
  .opts li { margin: 1pt 0; }
  .overflow-x-auto { display: block; max-width: 100%; overflow-x: auto; }
  math[display="block"] { margin: 3pt 0; }
  .blank { padding-left: 22pt; margin-top: 6pt; color: #6b7280; }
  .answers { page-break-before: always; break-before: page; }
  .a { margin: 0 0 10pt; page-break-inside: avoid; break-inside: avoid; }
  .ans { margin: 0; }
  .ans .no { font-weight: 700; }
  .exp { margin: 1pt 0 0; padding-left: 22pt; font-size: 10.5pt; color: #374151; }
  .footer {
    margin-top: 18pt; padding-top: 6pt;
    border-top: 1px solid #d1d5db;
    font-size: 9pt; color: #6b7280; text-align: center;
  }
  @media screen {
    body { max-width: 780px; margin: 0 auto; padding: 24px; }
    .answers { border-top: 2px dashed #d1d5db; margin-top: 28px; padding-top: 24px; }
  }
</style>
</head>
<body>
  <h1>${printMath(paper.title)}</h1>
  <p class="sub">共 ${escapeHtml(data.quiz.total)} 题 · 生成于 ${escapeHtml(
    fmtDateTime(data.quiz.created_at),
  )}</p>
  <div class="instructions">${printMath(paper.instructions)}</div>
  ${questionsHtml || '<p class="sub">（本卷暂无题目）</p>'}
  <div class="footer">苏问 组卷导出 · 导出时间 ${escapeHtml(exportedAt)}</div>

  <div class="answers">
    <h1>${printMath(data.answer_key?.title ?? "参考答案与解析")}</h1>
    ${answersHtml || '<p class="sub">（本卷暂无答案）</p>'}
    <div class="footer">苏问 组卷导出 · 导出时间 ${escapeHtml(exportedAt)}</div>
  </div>
</body>
</html>`;
}

function NoAccess() {
  return (
    <div className="flex h-full items-center justify-center p-6">
      <div className="w-full max-w-md rounded-2xl border border-slate-200 bg-white px-8 py-12 text-center">
        <span className="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-2xl bg-red-50 text-red-500">
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
            <path d="M12 15v2m-6 4h12a2 2 0 0 0 2-2v-6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v6a2 2 0 0 0 2 2Zm10-10V7a4 4 0 0 0-8 0v4h8Z" />
          </svg>
        </span>
        <h2 className="text-lg font-semibold text-slate-900">没有访问权限</h2>
        <p className="mt-1 text-sm text-slate-500">
          组卷导出仅对教师账号开放，请使用教师或管理员账号登录。
        </p>
      </div>
    </div>
  );
}

function DifficultyChips({ counts }: { counts: Record<string, number> }) {
  const known = new Set(DIFFICULTY_ORDER.map((d) => d.key));
  const extras = Object.entries(counts ?? {}).filter(([k]) => !known.has(k));
  const chips = [
    ...DIFFICULTY_ORDER.filter((d) => (counts?.[d.key] ?? 0) > 0).map((d) => ({
      key: d.key,
      text: `${d.label} ${counts[d.key]}`,
      cls: d.cls,
    })),
    ...extras
      .filter(([, v]) => v > 0)
      .map(([k, v]) => ({
        key: k,
        text: `${k} ${v}`,
        cls: "bg-slate-100 text-slate-600",
      })),
  ];
  if (chips.length === 0) {
    return <span className="text-xs text-slate-400">—</span>;
  }
  return (
    <span className="flex flex-wrap gap-1">
      {chips.map((c) => (
        <span
          key={c.key}
          className={`rounded-full px-2 py-0.5 text-xs font-medium ${c.cls}`}
        >
          {c.text}
        </span>
      ))}
    </span>
  );
}

export default function TeacherPage() {
  const { user } = useAuth();
  const canAccess = user?.role === "teacher" || user?.role === "admin";

  const [quizzes, setQuizzes] = useState<TeacherQuizItem[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [banner, setBanner] = useState<string | null>(null);
  const [ok, setOk] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    if (!canAccess) return;
    let alive = true;
    (async () => {
      try {
        const list = await apiJson<TeacherQuizItem[]>("/api/teacher/quizzes");
        if (!alive) return;
        setQuizzes(list);
        setError(null);
      } catch (err) {
        if (!alive) return;
        setQuizzes([]);
        setError(err instanceof ApiError ? err.message : "卷子列表加载失败");
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, [canAccess, reloadKey]);

  const exportJson = async (quiz: TeacherQuizItem) => {
    if (busyId) return;
    setBusyId(`${quiz.id}:json`);
    setBanner(null);
    setOk(null);
    try {
      const data = await apiJson<TeacherExportJson>(
        `/api/teacher/quizzes/${quiz.id}/export?format=json`,
        { method: "POST" },
      );
      const blob = new Blob([JSON.stringify(data, null, 2)], {
        type: "application/json",
      });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `quiz-${safeFileName(quiz.title)}-${quiz.id.slice(0, 8)}.json`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      setOk(`已导出《${quiz.title}》（${data.questions?.length ?? 0} 题）为 JSON 文件`);
    } catch (err) {
      setBanner(err instanceof ApiError ? err.message : "导出 JSON 失败");
    } finally {
      setBusyId(null);
    }
  };

  const openPrint = async (quiz: TeacherQuizItem) => {
    if (busyId) return;
    setBusyId(`${quiz.id}:print`);
    setBanner(null);
    setOk(null);
    try {
      const data = await apiJson<TeacherExportPrint>(
        `/api/teacher/quizzes/${quiz.id}/export?format=print`,
        { method: "POST" },
      );
      const w = window.open("", "_blank");
      if (!w) {
        setBanner(
          "浏览器拦截了弹出窗口，无法打开打印视图。请在地址栏右侧允许本站弹出窗口后重试。",
        );
        return;
      }
      const doc = w.document;
      doc.open();
      doc.write(buildPrintHtml(data));
      doc.close();
      w.focus();
      w.print();
      setOk(`已在新窗口打开《${quiz.title}》的打印视图（含分页答案页）`);
    } catch (err) {
      setBanner(err instanceof ApiError ? err.message : "生成打印视图失败");
    } finally {
      setBusyId(null);
    }
  };

  if (!canAccess) return <NoAccess />;

  return (
    <div className="mx-auto w-full max-w-5xl space-y-4 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 className="text-base font-semibold text-slate-900">组卷导出</h2>
          <p className="mt-0.5 text-xs text-slate-500">
            把已生成的卷子导出为结构化 JSON，或直接打印成 A4
            试卷（题目页与答案页自动分页）。
          </p>
        </div>
        <button
          type="button"
          onClick={() => {
            setLoading(true);
            setError(null);
            setReloadKey((k) => k + 1);
          }}
          className="rounded-xl border border-slate-300 bg-white px-3.5 py-2 text-sm font-medium text-slate-700 hover:bg-slate-100"
        >
          刷新列表
        </button>
      </div>

      {banner && <ErrorBanner message={banner} onClose={() => setBanner(null)} />}
      {error && (
        <ErrorBanner
          message={error}
          onRetry={() => {
            setLoading(true);
            setError(null);
            setReloadKey((k) => k + 1);
          }}
        />
      )}
      {ok && (
        <div className="flex items-center justify-between gap-3 rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-2.5 text-sm text-emerald-700">
          <span className="min-w-0 break-words">{ok}</span>
          <button
            type="button"
            onClick={() => setOk(null)}
            className="shrink-0 text-emerald-400 hover:text-emerald-600"
            aria-label="关闭提示"
          >
            ✕
          </button>
        </div>
      )}

      <Card
        title="可导出的卷子"
        extra={
          <span className="text-xs text-slate-400">
            {quizzes ? `${quizzes.length} 份` : "—"}
          </span>
        }
      >
        {loading ? (
          <LoadingState text="卷子列表加载中…" />
        ) : !quizzes || quizzes.length === 0 ? (
          <EmptyState
            title="暂无可导出的卷子"
            description="先到「练习」页生成一份卷子，这里就会出现可导出的记录。"
          />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-160 text-left text-sm">
              <thead>
                <tr className="border-b border-slate-100 text-xs text-slate-400">
                  <th className="py-2 font-medium">标题</th>
                  <th className="py-2 font-medium">创建时间</th>
                  <th className="py-2 text-right font-medium">题数</th>
                  <th className="py-2 font-medium">难度分布</th>
                  <th className="py-2 font-medium">状态</th>
                  <th className="py-2 text-right font-medium">导出</th>
                </tr>
              </thead>
              <tbody>
                {quizzes.map((q) => (
                  <tr key={q.id} className="border-b border-slate-50 align-middle">
                    <td className="py-2.5 pr-3">
                      <span className="font-medium text-slate-800"><MathText text={q.title} /></span>
                      <span
                        className="ml-2 font-mono text-xs text-slate-400"
                        title={q.id}
                      >
                        {q.id.slice(0, 8)}
                      </span>
                    </td>
                    <td className="py-2.5 pr-3 whitespace-nowrap text-xs text-slate-500">
                      {fmtDateTime(q.created_at)}
                    </td>
                    <td className="py-2.5 pr-3 text-right font-mono text-slate-700">
                      {q.num_questions}
                    </td>
                    <td className="py-2.5 pr-3">
                      <DifficultyChips counts={q.difficulty_counts} />
                    </td>
                    <td className="py-2.5 pr-3">
                      <span
                        className={`rounded-full px-2 py-0.5 text-xs font-medium ${
                          q.status === "ready"
                            ? "bg-emerald-50 text-emerald-700"
                            : "bg-slate-100 text-slate-600"
                        }`}
                      >
                        {q.status}
                      </span>
                    </td>
                    <td className="py-2.5">
                      <div className="flex items-center justify-end gap-2">
                        {busyId?.startsWith(q.id) && <Spinner />}
                        <button
                          type="button"
                          disabled={busyId !== null}
                          onClick={() => void exportJson(q)}
                          className="rounded-lg border border-slate-300 bg-white px-2.5 py-1.5 text-xs font-medium whitespace-nowrap text-slate-700 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-50"
                        >
                          导出 JSON
                        </button>
                        <button
                          type="button"
                          disabled={busyId !== null}
                          onClick={() => void openPrint(q)}
                          className="rounded-lg bg-indigo-600 px-2.5 py-1.5 text-xs font-semibold whitespace-nowrap text-white hover:bg-indigo-700 disabled:cursor-not-allowed disabled:opacity-50"
                        >
                          打印视图
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="mt-3 text-xs text-slate-400">
          JSON 导出含题干 / 选项 / 答案 / 解析 / 难度 / 关联知识点，可直接喂给其他题库系统；
          打印视图为自包含 A4 页面（正文 12pt、题号加粗、选项缩进），答案与解析在独立分页上。
        </p>
      </Card>
    </div>
  );
}
