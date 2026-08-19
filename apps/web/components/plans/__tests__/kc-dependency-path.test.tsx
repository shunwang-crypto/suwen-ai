import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { KcDependencyPath } from "@/components/plans/kc-dependency-path";

const dependency = {
  levels: [
    {
      index: 0,
      nodes: [
        {
          kc_id: "a",
          kc_name: "极限",
          task_ids: [],
          task_statuses: [],
          is_current_task: false,
          is_prerequisite: true,
          is_blocking: true,
          blocking_task_ids: ["00000000-0000-4000-8000-000000000001"],
        },
      ],
    },
    {
      index: 1,
      nodes: [
        {
          kc_id: "c",
          kc_name: "导数",
          task_ids: ["00000000-0000-4000-8000-000000000001"],
          task_statuses: [{ task_id: "00000000-0000-4000-8000-000000000001", status: "pending" as const }],
          is_current_task: true,
          is_prerequisite: false,
          is_blocking: false,
          blocking_task_ids: [],
        },
      ],
    },
  ],
  edges: [{ from_kc_id: "a", to_kc_id: "c" }],
};

describe("KcDependencyPath", () => {
  it("renders levels once, keeps the dependency area scrollable, and opens task nodes", () => {
    const onTaskOpen = (taskId: string) => {
      expect(taskId).toBe("00000000-0000-4000-8000-000000000001");
    };
    render(<KcDependencyPath dependency={dependency} onTaskOpen={onTaskOpen} />);

    expect(screen.getByRole("region", { name: "知识点学习路径" })).toBeInTheDocument();
    // 默认是图视图: 依赖关系画成真边, 而不是列间一个通用箭头
    const graph = screen.getByTestId("kc-dependency-graph-scroll");
    expect(graph).toHaveClass("overflow-x-auto");
    expect(graph.querySelectorAll("path[marker-end]")).toHaveLength(1);
    expect(screen.getByText("极限")).toBeInTheDocument();
    expect(screen.getByText("导数")).toBeInTheDocument();
    expect(screen.getByText("阻塞 1 个后续任务")).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: "查看计划任务知识点：导数，待学习" }),
    );

    // 列表视图是等价视图: 切过去仍然逐层列出同样的知识点
    fireEvent.click(screen.getByRole("button", { name: "列表" }));
    expect(screen.queryByTestId("kc-dependency-graph-scroll")).toBeNull();
    expect(screen.getByTestId("kc-dependency-scroll")).toHaveClass("overflow-x-auto");
    expect(screen.getByText("极限")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "查看知识点任务：导数" }));
  });

  it("progressively hides an empty dependency graph", () => {
    const { container } = render(<KcDependencyPath dependency={{ levels: [], edges: [] }} />);
    expect(container.firstChild).toBeNull();
  });
});
