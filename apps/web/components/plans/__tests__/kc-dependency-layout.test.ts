import { describe, expect, it } from "vitest";

import {
  COL_GAP,
  NODE_W,
  kcNodeKind,
  kcTaskProgress,
  layoutKcDependency,
} from "@/components/plans/kc-dependency-layout";
import type { PlanKcDependency, PlanKcDependencyNode } from "@/lib/types";

function node(kc_id: string, extra: Partial<PlanKcDependencyNode> = {}): PlanKcDependencyNode {
  return {
    kc_id,
    kc_name: `KC ${kc_id}`,
    task_ids: [],
    task_statuses: [],
    is_current_task: false,
    is_prerequisite: true,
    is_blocking: false,
    blocking_task_ids: [],
    ...extra,
  };
}

const twoLayers: PlanKcDependency = {
  levels: [
    { index: 0, nodes: [node("a"), node("b")] },
    {
      index: 1,
      nodes: [
        node("c", {
          task_ids: ["t1"],
          task_statuses: [{ task_id: "t1", status: "pending" }],
          is_current_task: true,
          is_prerequisite: false,
        }),
      ],
    },
  ],
  edges: [
    { from_kc_id: "a", to_kc_id: "c" },
    { from_kc_id: "b", to_kc_id: "c" },
  ],
};

describe("知识点依赖图布局", () => {
  it("空数据不产出任何几何", () => {
    expect(layoutKcDependency(undefined).nodes).toHaveLength(0);
    expect(layoutKcDependency({ levels: [], edges: [] }).nodes).toHaveLength(0);
  });

  it("层映射为列，层内顺序沿用后端顺序（不重排）", () => {
    const layout = layoutKcDependency(twoLayers);
    expect(layout.nodes.map((item) => item.node.kc_id)).toEqual(["a", "b", "c"]);
    expect(layout.nodes.map((item) => item.column)).toEqual([0, 0, 1]);
    const [first, second] = layout.nodes;
    expect(first.x).toBe(second.x); // 同层同列
    expect(second.y).toBeGreaterThan(first.y); // 层内按给定顺序往下排
    expect(layout.nodes[2].x).toBe(first.x + NODE_W + COL_GAP);
  });

  it("只画后端给的边，且两端节点都必须在渲染集合里", () => {
    const layout = layoutKcDependency({
      ...twoLayers,
      edges: [
        ...twoLayers.edges,
        { from_kc_id: "a", to_kc_id: "ghost" }, // 目标不在任何层里
        { from_kc_id: "ghost", to_kc_id: "c" }, // 源不在任何层里
      ],
    });
    expect(layout.edges.map((edge) => `${edge.from}->${edge.to}`)).toEqual(["a->c", "b->c"]);
  });

  it("不从层级顺序推断依赖：没有边就没有连线", () => {
    const layout = layoutKcDependency({ ...twoLayers, edges: [] });
    expect(layout.nodes).toHaveLength(3);
    expect(layout.edges).toHaveLength(0);
  });

  it("自环与重复边被丢弃，剩下的边都有可渲染 path", () => {
    const layout = layoutKcDependency({
      ...twoLayers,
      edges: [
        { from_kc_id: "a", to_kc_id: "a" },
        { from_kc_id: "a", to_kc_id: "c" },
        { from_kc_id: "a", to_kc_id: "c" },
      ],
    });
    expect(layout.edges).toHaveLength(1);
    expect(layout.edges[0].d.startsWith("M ")).toBe(true);
    expect(layout.edges[0].d).not.toContain("NaN");
  });

  it("同一 KC 在多层重复出现时只画一次（后端理论上不给，但不能画重影）", () => {
    const layout = layoutKcDependency({
      levels: [
        { index: 0, nodes: [node("a")] },
        { index: 1, nodes: [node("a"), node("b")] },
      ],
      edges: [],
    });
    expect(layout.nodes.map((item) => item.node.kc_id)).toEqual(["a", "b"]);
  });

  it("回指/同列边仍然画出来，不隐瞒也不改层", () => {
    const layout = layoutKcDependency({
      levels: [{ index: 0, nodes: [node("a"), node("b")] }],
      edges: [{ from_kc_id: "b", to_kc_id: "a" }],
    });
    expect(layout.edges).toHaveLength(1);
    expect(layout.edges[0].d).not.toContain("NaN");
  });

  it("画布尺寸容纳最宽的层", () => {
    const layout = layoutKcDependency(twoLayers);
    expect(layout.columnCount).toBe(2);
    expect(layout.width).toBeGreaterThan(NODE_W * 2);
    expect(layout.height).toBeGreaterThan(0);
    for (const item of layout.nodes) {
      expect(item.x).toBeGreaterThanOrEqual(0);
      expect(item.x + NODE_W).toBeLessThanOrEqual(layout.width);
    }
  });
});

describe("节点档位与进度", () => {
  it("阻塞优先于计划任务，无任务的是递归先修", () => {
    expect(kcNodeKind(node("a", { is_blocking: true, is_current_task: true }))).toBe("blocking");
    expect(kcNodeKind(node("a", { is_current_task: true, task_ids: ["t"] }))).toBe("current_task");
    expect(kcNodeKind(node("a"))).toBe("prerequisite");
  });

  it("任务状态汇总：全完成/部分/待学习/未安排", () => {
    expect(kcTaskProgress(node("a"))).toBe("none");
    expect(
      kcTaskProgress(node("a", { task_statuses: [{ task_id: "t", status: "done" }] })),
    ).toBe("done");
    expect(
      kcTaskProgress(
        node("a", {
          task_statuses: [
            { task_id: "t1", status: "done" },
            { task_id: "t2", status: "pending" },
          ],
        }),
      ),
    ).toBe("partial");
    expect(
      kcTaskProgress(node("a", { task_statuses: [{ task_id: "t", status: "pending" }] })),
    ).toBe("pending");
  });

  it("跳过的任务算部分完成，不算全完成（不夸大进度）", () => {
    expect(
      kcTaskProgress(node("a", { task_statuses: [{ task_id: "t", status: "skipped" }] })),
    ).toBe("partial");
  });
});
