import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { apiJson } from "@/lib/api";
import { useOrgMembership } from "@/lib/use-org-membership";

vi.mock("@/lib/api", () => ({ apiJson: vi.fn() }));

describe("user-scoped institution membership", () => {
  it("loads each signed-in user's membership and does not reuse the previous result", async () => {
    vi.mocked(apiJson).mockResolvedValueOnce([]).mockResolvedValueOnce([{ id: "org-b" }]);
    const { result, rerender } = renderHook(({ userId }) => useOrgMembership(userId), {
      initialProps: { userId: "a" as string | undefined },
    });
    await waitFor(() => expect(result.current).toBe(false));
    rerender({ userId: undefined });
    expect(result.current).toBeNull();
    rerender({ userId: "b" });
    expect(result.current).toBeNull();
    await waitFor(() => expect(result.current).toBe(true));
    expect(apiJson).toHaveBeenCalledTimes(2);
  });

  it("ignores a late result for an account that has been switched away from", async () => {
    let finishA!: (value: unknown[]) => void;
    vi.mocked(apiJson).mockReturnValueOnce(new Promise((resolve) => { finishA = resolve; }))
      .mockResolvedValueOnce([{ id: "org-b" }]);
    const { result, rerender } = renderHook(({ userId }) => useOrgMembership(userId), {
      initialProps: { userId: "a" },
    });
    rerender({ userId: "b" });
    await waitFor(() => expect(result.current).toBe(true));
    await act(async () => { finishA([]); });
    expect(result.current).toBe(true);
  });

  it("keeps membership unknown when the replacement account's request fails", async () => {
    vi.mocked(apiJson).mockResolvedValueOnce([]).mockRejectedValueOnce(new Error("offline"));
    const { result, rerender } = renderHook(({ userId }) => useOrgMembership(userId), {
      initialProps: { userId: "a" },
    });
    await waitFor(() => expect(result.current).toBe(false));
    rerender({ userId: "b" });
    await act(async () => {});
    expect(result.current).toBeNull();
  });

  it("reloads membership when the same user signs in again", async () => {
    vi.mocked(apiJson).mockResolvedValueOnce([]).mockRejectedValueOnce(new Error("offline"));
    const { result, rerender } = renderHook(({ userId }) => useOrgMembership(userId), {
      initialProps: { userId: "a" as string | undefined },
    });
    await waitFor(() => expect(result.current).toBe(false));
    rerender({ userId: undefined });
    rerender({ userId: "a" });
    await act(async () => {});
    expect(result.current).toBeNull();
    expect(apiJson).toHaveBeenCalledTimes(2);
  });
});
