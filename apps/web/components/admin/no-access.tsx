"use client";

// 无权限提示（admin 域版本；evals/teacher 各有自己的同名组件，互不相干）。

export function NoAccess() {
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
            <path d="M12 15v2m-6.4h12a2 2 0 0 0 2-2v-6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v6a2 2 0 0 0 2 2Zm10-10V7a4 4 0 0 0-8 0v4h8Z" />
          </svg>
        </span>
        <h2 className="text-lg font-semibold text-slate-900">没有访问权限</h2>
        <p className="mt-1 text-sm text-slate-500">
          运营面板仅对管理员开放，请使用管理员账号登录。
        </p>
      </div>
    </div>
  );
}
