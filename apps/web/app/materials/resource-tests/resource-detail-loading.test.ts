import { describe, expect, it } from "vitest";

import {
  isTutorManagedResource,
  isPdfResource,
  pdfResourceSummary,
  resourceDetailKey,
  shouldShowResourceDetailSkeleton,
  tutorDraftHref,
} from "@/components/resources/resource-library";

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

  it("detects PDF resources for the page-count card", () => {
    expect(isPdfResource({ mime_type: "application/pdf", source_filename: "paper.bin" })).toBe(true);
    expect(isPdfResource({ mime_type: "text/plain", source_filename: "paper.pdf" })).toBe(true);
    expect(isPdfResource({ mime_type: "text/plain", source_filename: "notes.txt" })).toBe(false);
    expect(pdfResourceSummary({ mime_type: "application/pdf", source_filename: "paper.pdf", page_count: 3, question_count: 7 }))
      .toBe("PDF · 共 3 页 · 已识别 7 题");
    expect(pdfResourceSummary({ mime_type: "text/plain", source_filename: "notes.txt", page_count: null, question_count: 7 })).toBeNull();
  });
});
