import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { TutorProblemInput } from "@/components/tutor/tutor-problem-input";
import type { TutorProblemDraft } from "@/lib/types";

const { apiFetch, apiJson } = vi.hoisted(() => ({
  apiFetch: vi.fn(),
  apiJson: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch, apiJson };
});

function draft(overrides: Partial<TutorProblemDraft> = {}): TutorProblemDraft {
  return {
    id: "11111111-1111-4111-8111-111111111111",
    status: "review",
    selected_pages: [1],
    selected_question_index: 0,
    stem: "第一道题",
    options: ["选项 A", "选项 B"],
    known_conditions: "x > 0",
    current_thought: "不会变形",
    confirmed_problem_text: "",
    confirmed_page_no: null,
    thread_local_id: null,
    warnings: [],
    error_code: null,
    recognition_attempts: 1,
    questions: [
      {
        index: 0,
        question_no: "1",
        page_no: 1,
        type: "single",
        stem: "第一道题",
        options: ["选项 A", "选项 B"],
        confidence: 0.9,
      },
      {
        index: 1,
        question_no: "2",
        page_no: 2,
        type: "short",
        stem: "第二道题",
        options: [],
        confidence: 0.8,
      },
    ],
    attachment: null,
    vision_uses_platform_quota: false,
    created_at: "2026-08-02T00:00:00Z",
    updated_at: "2026-08-02T00:00:00Z",
    ...overrides,
  };
}

beforeEach(() => {
  apiFetch.mockReset();
  apiJson.mockReset();
});

describe("TutorProblemInput attachments", () => {
  it("uploads an image chosen from the file picker", async () => {
    const uploaded = draft({ status: "recognizing", questions: [] });
    apiFetch.mockResolvedValue({ json: async () => uploaded });
    const { container } = render(<TutorProblemInput onConfirmed={() => {}} />);
    const file = new File(["image"], "question.png", { type: "image/png" });

    fireEvent.change(container.querySelector('input[type="file"]')!, { target: { files: [file] } });

    await waitFor(() => expect(apiFetch).toHaveBeenCalledTimes(1));
    const [, init] = apiFetch.mock.calls[0];
    expect(init.body).toBeInstanceOf(FormData);
    expect(((init.body as FormData).get("file") as File).name).toBe("question.png");
  });

  it("uploads a dropped document", async () => {
    apiFetch.mockResolvedValue({ json: async () => draft({ status: "recognizing", questions: [] }) });
    render(<TutorProblemInput onConfirmed={() => {}} />);
    const file = new File(["%PDF-1.7"], "paper.pdf", { type: "application/pdf" });
    const input = screen.getByRole("textbox", { name: "题目题干" });

    fireEvent.drop(input.parentElement!, { dataTransfer: { files: [file] } });

    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith(
      "/api/tutor/drafts",
      expect.objectContaining({ method: "POST" }),
    ));
  });

  it("uploads a pasted clipboard image without inserting binary text", async () => {
    apiFetch.mockResolvedValue({ json: async () => draft({ status: "recognizing", questions: [] }) });
    render(<TutorProblemInput onConfirmed={() => {}} />);
    const image = new File(["pixels"], "pasted.webp", { type: "image/webp" });
    const input = screen.getByRole("textbox", { name: "题目题干" });

    fireEvent.paste(input, { clipboardData: { files: [image] } });

    await waitFor(() => expect(apiFetch).toHaveBeenCalledTimes(1));
    expect(input).not.toHaveValue("[object File]");
  });
});

describe("TutorProblemInput review", () => {
  it("selects one of multiple questions, edits fields, and confirms a locked snapshot", async () => {
    const user = userEvent.setup();
    const initial = draft();
    const selected = draft({
      selected_question_index: 1,
      stem: "第二道题",
      options: [],
    });
    const confirmed = draft({
      ...selected,
      status: "confirmed",
      confirmed_problem_text: "校对后的第二题",
      confirmed_page_no: 2,
      thread_local_id: "44444444-4444-4444-8444-444444444444",
    });
    apiJson.mockImplementation(async (path: string, options?: { method?: string }) => {
      if (path.endsWith(initial.id) && !options) return initial;
      if (options?.method === "PATCH") return selected;
      if (path.endsWith("/confirm")) return confirmed;
      throw new Error(`unexpected ${path}`);
    });
    const onConfirmed = vi.fn();
    render(<TutorProblemInput initialDraftId={initial.id} onConfirmed={onConfirmed} />);
    await user.click(await screen.findByRole("button", { name: "校对题目" }));
    await user.click(screen.getByRole("radio", { name: /第 2 题/ }));
    await waitFor(() => expect(screen.getByLabelText("题干")).toHaveValue("第二道题"));
    await user.clear(screen.getByLabelText("题干"));
    await user.type(screen.getByLabelText("题干"), "校对后的第二题");
    await user.clear(screen.getByLabelText("已知条件"));
    await user.type(screen.getByLabelText("已知条件"), "a = 2");
    await user.clear(screen.getByLabelText("你当前的思路或卡点"));
    await user.type(screen.getByLabelText("你当前的思路或卡点"), "卡在第二步");
    await user.click(screen.getByRole("button", { name: "确认并开始辅导" }));

    await waitFor(() => expect(onConfirmed).toHaveBeenCalledWith(confirmed));
    expect(apiJson).toHaveBeenCalledWith(
      `/api/tutor/drafts/${initial.id}/confirm`,
      expect.objectContaining({ body: expect.objectContaining({ stem: "校对后的第二题" }) }),
    );
  });

  it("traps focus, closes with Escape, and restores focus", async () => {
    const user = userEvent.setup();
    apiJson.mockResolvedValue(draft());
    render(<TutorProblemInput initialDraftId={draft().id} onConfirmed={() => {}} />);
    const opener = await screen.findByRole("button", { name: "校对题目" });
    await user.click(opener);
    expect(screen.getByRole("button", { name: "关闭校对" })).toHaveFocus();
    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(opener).toHaveFocus();
  });
});
