"use client";

import { useEffect, useState } from "react";

import { apiJson } from "@/lib/api";

/** Membership belongs to the signed-in user; an old user's result is never reused. */
export function useOrgMembership(userId: string | undefined): boolean | null {
  const [membership, setMembership] = useState<{
    userId: string;
    hasOrgs: boolean;
  } | null>(null);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- retire membership when the authentication scope changes
    setMembership(null);
    if (!userId) return;
    let alive = true;
    void apiJson<unknown[]>("/api/orgs")
      .then((list) => {
        if (alive) setMembership({ userId, hasOrgs: Array.isArray(list) && list.length > 0 });
      })
      .catch(() => {
        // An unavailable membership service must not hide the institutions entry.
      });
    return () => { alive = false; };
  }, [userId]);

  return membership && membership.userId === userId ? membership.hasOrgs : null;
}
