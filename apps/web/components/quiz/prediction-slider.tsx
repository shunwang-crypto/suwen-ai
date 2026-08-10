"use client";

const LABELS: Record<number, string> = {
  1: "很没把握",
  2: "不太确定",
  3: "一半把握",
  4: "比较有把握",
  5: "很有把握",
};

export function PredictionSlider({
  questionNumber,
  value,
  disabled = false,
  onChange,
}: {
  questionNumber: number;
  value?: number;
  disabled?: boolean;
  onChange: (value: number | undefined) => void;
}) {
  const selected = value != null;
  return (
    <fieldset className="mt-3 rounded-[9px] border border-dashed border-brand-200 bg-brand-50/35 px-3 py-2.5">
      <legend className="text-body-sm font-medium text-ink-secondary">
        作答前，你觉得自己有多大把握？
      </legend>
      {selected && (
        <div className="flex justify-end">
          <button
            type="button"
            disabled={disabled}
            onClick={() => onChange(undefined)}
            className="text-caption font-medium text-ink-muted underline-offset-2 hover:text-brand-700 hover:underline disabled:opacity-50"
          >
            清除预测
          </button>
        </div>
      )}
      <div className="mt-2 flex items-center gap-3">
        <span className="text-caption text-ink-muted">没把握</span>
        <input
          type="range"
          min={1}
          max={5}
          step={1}
          value={value ?? 3}
          disabled={disabled}
          aria-label={`第 ${questionNumber} 题作答前信心预测`}
          aria-valuetext={selected ? LABELS[value ?? 3] : "未预测"}
          onChange={(event) => {
            if (!disabled) onChange(Number(event.target.value));
          }}
          className={`min-w-0 flex-1 accent-indigo-600 ${selected ? "" : "opacity-45"}`}
        />
        <span className="text-caption text-ink-muted">很有把握</span>
        <output
          aria-live="polite"
          className="w-16 text-right text-caption font-medium text-brand-700"
        >
          {selected ? LABELS[value ?? 3] : "可跳过"}
        </output>
      </div>
      <p className="mt-1.5 text-caption leading-4 text-ink-muted">
        这是校准自我判断的预测，可跳过，不影响本题评分和掌握度。
      </p>
    </fieldset>
  );
}
