export function UnderConstruction({ title }: { title: string }) {
  return (
    <div className="flex h-full items-center justify-center p-6">
      <div className="flex w-full max-w-md flex-col items-center gap-3 rounded-2xl border border-dashed border-slate-300 bg-white px-8 py-12 text-center">
        <span className="flex h-12 w-12 items-center justify-center rounded-2xl bg-indigo-50 text-indigo-500">
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
            <path d="M14.7 6.3a4 4 0 0 0-5.06 5.06L3 18v3h3l6.64-6.64a4 4 0 0 0 5.06-5.06l-2.85 2.85-2.12-2.12L14.7 6.3Z" />
          </svg>
        </span>
        <h2 className="text-lg font-semibold text-slate-900">{title}</h2>
        <p className="text-sm text-slate-500">建设中，敬请期待。</p>
      </div>
    </div>
  );
}
