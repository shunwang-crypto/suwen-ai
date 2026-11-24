import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { MemoryCorrectionForm } from "@/components/settings/memory-correction-form";

describe("memory correction", () => {
  it("sends the learner's corrected text and current understanding", () => {
    const onSave = vi.fn();
    render(<MemoryCorrectionForm id="memory" text="旧的困惑" kind="confused" busy={false} onSave={onSave} onCancel={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("更正内容"), { target: { value: "  已经理解导数定义  " } });
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "understood" } });
    fireEvent.click(screen.getByRole("button", { name: "保存纠正" }));
    expect(onSave).toHaveBeenCalledWith({ text: "已经理解导数定义", kind: "understood" });
  });

  it("blocks blank edits and cancels without saving", () => {
    const onSave = vi.fn(), onCancel = vi.fn();
    render(<MemoryCorrectionForm id="memory" text="旧的困惑" kind="confused" busy={false} onSave={onSave} onCancel={onCancel} />);
    fireEvent.change(screen.getByLabelText("更正内容"), { target: { value: "  " } });
    expect(screen.getByRole("button", { name: "保存纠正" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(onCancel).toHaveBeenCalledOnce();
    expect(onSave).not.toHaveBeenCalled();
  });
});
