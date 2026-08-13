import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { buildErrorQuery, useKnowledgeBaseFilter } from "@/lib/error-filters";
import type { KbOut } from "@/lib/types";

const KB_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const KB_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const KB_C = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";

const kbs: KbOut[] = [
  { id: KB_A, name: "数学", subject: "数学", kb_version: 1, created_at: "", owner_type: "user", org_id: null, org_name: null, writable: true },
  { id: KB_B, name: "物理", subject: "物理", kb_version: 1, created_at: "", owner_type: "user", org_id: null, org_name: null, writable: true },
];

function FilterHarness() {
  const { kbFilter, selectKb } = useKnowledgeBaseFilter(kbs, true);
  return (
    <div>
      <output data-testid="selected">{kbFilter ?? "全部"}</output>
      <button type="button" onClick={() => selectKb(KB_A)}>数学</button>
      <button type="button" onClick={() => selectKb(KB_B)}>物理</button>
      <button type="button" onClick={() => selectKb(null)}>全部科目</button>
    </div>
  );
}

describe("useKnowledgeBaseFilter", () => {
  it("initializes from a visible URL KB and clears it without a URL fallback", async () => {
    window.history.replaceState({}, "", `/reviews?view=errors&kb_id=${KB_A}&state=active&cause=misread`);
    render(<FilterHarness />);
    expect(screen.getByTestId("selected")).toHaveTextContent(KB_A);

    await userEvent.click(screen.getByRole("button", { name: "全部科目" }));
    expect(screen.getByTestId("selected")).toHaveTextContent("全部");
    expect(window.location.search).toBe("?view=errors&state=active&cause=misread");
  });

  it("updates the URL, keeps other filters, and restores state with back/forward", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", "/reviews?view=errors&state=new&q=函数");
    render(<FilterHarness />);

    await user.click(screen.getByRole("button", { name: "物理" }));
    expect(window.location.search).toBe(`?view=errors&state=new&q=%E5%87%BD%E6%95%B0&kb_id=${KB_B}`);
    expect(screen.getByTestId("selected")).toHaveTextContent(KB_B);

    window.history.back();
    await waitFor(() => expect(screen.getByTestId("selected")).toHaveTextContent("全部"));
    expect(window.location.search).toBe("?view=errors&state=new&q=%E5%87%BD%E6%95%B0");
    window.history.forward();
    await waitFor(() => expect(screen.getByTestId("selected")).toHaveTextContent(KB_B));
  });

  it("rejects malformed and invisible URL KB values", async () => {
    window.history.replaceState({}, "", `/reviews?view=errors&kb_id=${KB_C}`);
    render(<FilterHarness />);
    await waitFor(() => expect(screen.getByTestId("selected")).toHaveTextContent("全部"));
    expect(window.location.search).toBe("?view=errors");

    window.history.replaceState({}, "", "/reviews?view=errors&kb_id=not-a-uuid");
    window.dispatchEvent(new PopStateEvent("popstate"));
    await waitFor(() => expect(screen.getByTestId("selected")).toHaveTextContent("全部"));
    expect(window.location.search).toBe("?view=errors");
  });
});

describe("buildErrorQuery", () => {
  it("omits a cleared KB while retaining state, cause, and search", () => {
    expect(buildErrorQuery({ page: 2, pageSize: 10, state: "new", cause: "misread", kbId: null, query: "函数" })).toBe(
      "page=2&page_size=10&state=new&cause=misread&q=%E5%87%BD%E6%95%B0",
    );
  });
});
