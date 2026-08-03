import { describe, expect, it } from "vitest";

import {
  resourceDetailKey,
  shouldShowResourceDetailSkeleton,
} from "@/app/resources/page";

describe("resource detail polling state", () => {
  it("only shows the full skeleton before the first detail load", () => {
    expect(shouldShowResourceDetailSkeleton(true, false)).toBe(true);
    expect(shouldShowResourceDetailSkeleton(true, true)).toBe(false);
    expect(shouldShowResourceDetailSkeleton(false, true)).toBe(false);
  });

  it("keeps a stable review key while polling updates resource data", () => {
    expect(resourceDetailKey("resource-1")).toBe("resource-1");
    expect(resourceDetailKey("resource-1")).toBe(resourceDetailKey("resource-1"));
  });
});
