import { beforeEach, describe, expect, it } from "vitest";

import {
  readTutorSession,
  tutorSessionKey,
  writeTutorSession,
} from "@/components/tutor/tutor-workspace";

describe("tutor session persistence", () => {
  beforeEach(() => window.localStorage.clear());

  it("stores only safe draft and thread identifiers", () => {
    writeTutorSession("user-1", { draftId: "draft-1", threadId: "thread-1" });
    const raw = window.localStorage.getItem(tutorSessionKey("user-1"));
    expect(JSON.parse(raw!)).toEqual({ draftId: "draft-1", threadId: "thread-1" });
    expect(raw).not.toContain("problem");
    expect(raw).not.toContain("attachment");
    expect(raw).not.toContain("recognition");
  });

  it("restores an unconfirmed draft by safe id and discards legacy content snapshots", () => {
    writeTutorSession("user-1", { draftId: "draft-2" });
    expect(readTutorSession("user-1")).toEqual({ draftId: "draft-2" });

    window.localStorage.setItem(
      tutorSessionKey("user-1"),
      JSON.stringify({ threadId: "legacy", problem: "不应保留的题干" }),
    );
    expect(readTutorSession("user-1")).toBeNull();
    expect(window.localStorage.getItem(tutorSessionKey("user-1"))).toBeNull();
  });
});
