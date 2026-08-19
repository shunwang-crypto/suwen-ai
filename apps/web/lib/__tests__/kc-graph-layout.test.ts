import { describe, expect, it } from "vitest";

import {
  type KbKcEdge,
  type KbKcNode,
  layoutKbKcGraph,
  visibleKcIds,
} from "@/lib/kc-graph-layout";

function node(kc_id: string, chapter: string, extra: Partial<KbKcNode> = {}): KbKcNode {
  return {
    kc_id,
    kc_name: `KC ${kc_id}`,
    chapter,
    evidence_state: "unobserved",
    evidence_state_label: "未观测",
    band: null,
    ...extra,
  };
}

const nodes: KbKcNode[] = [
  node("a", "第一章"),
  node("b", "第一章"),
  node("c", "第二章"),
  node("d", "第二章"),
];
const edges: KbKcEdge[] = [
  { from_kc_id: "a", to_kc_id: "b" },
  { from_kc_id: "b", to_kc_id: "c" },
  { from_kc_id: "c", to_kc_id: "d" },
];

describe("库级知识图：章节可见集合", () => {
  it("选中章 = 本章 KC + 跨章直接前置（跨章依赖不被隐藏）", () => {
    const { ids, external } = visibleKcIds("第二章", nodes, edges);
    expect([...ids].sort()).toEqual(["b", "c", "d"]);
    expect([...external]).toEqual(["b"]);
  });

  it("第一章没有上游，可见集合就是本章", () => {
    const { ids, external } = visibleKcIds("第一章", nodes, edges);
    expect([...ids].sort()).toEqual(["a", "b"]);
    expect(external.size).toBe(0);
  });

  it("指向不存在节点的边不会带出幽灵前置", () => {
    const { external } = visibleKcIds(
      "第二章",
      nodes,
      [...edges, { from_kc_id: "ghost", to_kc_id: "c" }],
    );
    expect([...external]).toEqual(["b"]);
  });
});

describe("库级知识图：分层布局", () => {
  it("按给定边的最长路径定列，不新增关系", () => {
    const { ids } = visibleKcIds("第二章", nodes, edges);
    const layout = layoutKbKcGraph(nodes, edges, ids);
    const columnById = new Map(layout.nodes.map((item) => [item.node.kc_id, item.column]));
    expect(columnById.get("b")).toBe(0);
    expect(columnById.get("c")).toBe(1);
    expect(columnById.get("d")).toBe(2);
    expect(layout.unlayered).toBe(0);
  });

  it("只画两端可见的边，去重去自环", () => {
    const layout = layoutKbKcGraph(
      nodes,
      [
        ...edges,
        { from_kc_id: "a", to_kc_id: "a" },
        { from_kc_id: "b", to_kc_id: "c" },
        { from_kc_id: "a", to_kc_id: "c" },
      ],
      new Set(["b", "c"]),
    );
    expect(layout.edges.map((edge) => `${edge.from}->${edge.to}`)).toEqual(["b->c"]);
  });

  it("没有边时所有节点在第 0 列，不编造顺序", () => {
    const layout = layoutKbKcGraph(nodes, [], new Set(["a", "b"]));
    expect(layout.nodes.every((item) => item.column === 0)).toBe(true);
    expect(layout.edges).toHaveLength(0);
  });

  it("成环残留放末列并如实计数，不静默丢节点", () => {
    const cyclic: KbKcEdge[] = [
      { from_kc_id: "a", to_kc_id: "b" },
      { from_kc_id: "b", to_kc_id: "a" },
    ];
    const layout = layoutKbKcGraph(nodes, cyclic, new Set(["a", "b"]));
    expect(layout.nodes).toHaveLength(2);
    expect(layout.unlayered).toBe(2);
  });

  it("跨章前置被标记出来", () => {
    const { ids, external } = visibleKcIds("第二章", nodes, edges);
    const layout = layoutKbKcGraph(nodes, edges, ids, external);
    const byId = new Map(layout.nodes.map((item) => [item.node.kc_id, item]));
    expect(byId.get("b")?.external).toBe(true);
    expect(byId.get("c")?.external).toBe(false);
  });

  it("几何有效：无 NaN、节点不越界", () => {
    const { ids } = visibleKcIds("第二章", nodes, edges);
    const layout = layoutKbKcGraph(nodes, edges, ids);
    expect(layout.width).toBeGreaterThan(0);
    expect(layout.height).toBeGreaterThan(0);
    for (const edge of layout.edges) expect(edge.d).not.toContain("NaN");
    for (const item of layout.nodes) {
      expect(Number.isFinite(item.x) && Number.isFinite(item.y)).toBe(true);
    }
  });

  it("空可见集合返回空布局", () => {
    const layout = layoutKbKcGraph(nodes, edges, new Set());
    expect(layout.nodes).toHaveLength(0);
    expect(layout.width).toBe(0);
  });
});
