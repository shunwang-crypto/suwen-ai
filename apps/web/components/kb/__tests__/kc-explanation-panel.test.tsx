import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { KcExplanationPanel } from "@/components/kb/kc-explanation-panel";

const apiJson = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api", () => ({
  apiJson,
  ApiError: class ApiError extends Error {},
}));

const READY = {
  available: true,
  kc_id: "kc-1",
  kc_name: "导数",
  band: "unobserved",
  stale: false,
  stale_reason: null,
  blocks: [
    {
      type: "concept",
      title: "导数的定义",
      content: "瞬时变化率。",
      source_refs: ["chunk-1"],
    },
    {
      type: "diagram",
      title: "求导流程",
      content: "",
      source_refs: ["chunk-1"],
      data: {
        nodes: [
          { id: "a", label: "原函数" },
          { id: "b", label: "导函数" },
        ],
        edges: [{ source: "a", target: "b" }],
      },
    },
  ],
  sources: [
    {
      id: "chunk-1",
      document: "微积分讲义.md",
      heading_path: "第二章 导数 > 2.1 定义",
      page_start: 12,
      text: "…",
    },
  ],
};

describe("知识点讲解面板", () => {
  beforeEach(() => {
    apiJson.mockReset();
  });

  it("渲染分块内容、类型标签与可核对的出处", async () => {
    apiJson.mockResolvedValue(READY);
    render(<KcExplanationPanel kbId="kb-1" kcId="kc-1" />);

    await waitFor(() => expect(screen.getByText("导数的定义")).toBeInTheDocument());
    expect(screen.getByText("概念")).toBeInTheDocument();
    expect(screen.getByText("结构图示")).toBeInTheDocument();
    // 出处必须写明文档、章节与页码，学生能回原文核对；
    // 两个 block 都引了同一片段 -> 两处各自标注出处（不合并、不省略）
    expect(
      screen.getAllByText(/出处：《微积分讲义\.md》 · 第二章 导数 > 2\.1 定义 · 第 12 页/),
    ).toHaveLength(2);
    // diagram 以节点 + 关系文字呈现，关系用节点标签而不是裸 id
    expect(screen.getByText("原函数 → 导函数")).toBeInTheDocument();
  });

  it("没有讲解时不伪造内容，给出生成入口", async () => {
    apiJson.mockResolvedValue({
      available: false,
      kc_id: "kc-1",
      kc_name: "导数",
      band: "unobserved",
      source_count: 3,
    });
    render(<KcExplanationPanel kbId="kb-1" kcId="kc-1" />);

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "生成讲解" })).toBeInTheDocument(),
    );
    expect(screen.getByText(/还没有这个知识点的讲解/)).toBeInTheDocument();
  });

  it("资料里没有对应片段时说清为什么不能生成", async () => {
    apiJson.mockResolvedValue({
      available: false,
      kc_id: "kc-1",
      kc_name: "孤立点",
      band: "unobserved",
      source_count: 0,
    });
    render(<KcExplanationPanel kbId="kb-1" kcId="kc-1" />);

    await waitFor(() =>
      expect(screen.getByText(/在资料里没有对应片段/)).toBeInTheDocument(),
    );
  });

  it("stale 时提示按旧资料生成，但内容仍然可读（不清空）", async () => {
    apiJson.mockResolvedValue({ ...READY, stale: true, stale_reason: "material_changed" });
    render(<KcExplanationPanel kbId="kb-1" kcId="kc-1" />);

    await waitFor(() => expect(screen.getByText(/资料已更新/)).toBeInTheDocument());
    expect(screen.getByText("导数的定义")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重新生成" })).toBeInTheDocument();
  });

  it("教学契约变更用不同的过期说法", async () => {
    apiJson.mockResolvedValue({
      ...READY,
      stale: true,
      stale_reason: "teaching_contract_changed",
    });
    render(<KcExplanationPanel kbId="kb-1" kcId="kc-1" />);

    await waitFor(() => expect(screen.getByText(/旧版教学契约/)).toBeInTheDocument());
  });

  it("讲解按当前学情标注讲法依据", async () => {
    apiJson.mockResolvedValue({ ...READY, band: "near" });
    render(<KcExplanationPanel kbId="kb-1" kcId="kc-1" />);
    await waitFor(() => expect(screen.getByText(/接近掌握/)).toBeInTheDocument());
  });

  it("读取失败时显示暂不可用，不展示任何伪造内容", async () => {
    apiJson.mockRejectedValue(new Error("boom"));
    render(<KcExplanationPanel kbId="kb-1" kcId="kc-1" />);

    await waitFor(() => expect(screen.getByText(/讲解暂不可用/)).toBeInTheDocument());
    expect(screen.queryByText("导数的定义")).toBeNull();
  });
});
