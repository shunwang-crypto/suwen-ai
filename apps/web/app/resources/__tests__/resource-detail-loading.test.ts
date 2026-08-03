import { describe, expect, it } from "vitest";

import {
  isTutorManagedResource,
  resourceDetailKey,
  shouldShowResourceDetailSkeleton,
  tutorDraftHref,
} from "@/app/resources/page";

const resource = (metadata: Record<string, unknown>) => ({ metadata } as never);

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

  it("recognizes tutor-managed resources so regular resource actions can be omitted", () => {
    expect(isTutorManagedResource(resource({ tutor_draft_id: "draft-1" }))).toBe(true);
    expect(isTutorManagedResource(resource({}))).toBe(false);
  });

  it("links to the selected tutor draft instead of the last local session", () => {
    expect(tutorDraftHref("draft/1")).toBe("/tutor?draft=draft%2F1");
  });
});
