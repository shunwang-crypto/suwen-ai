"use client";

import { useCallback, useEffect, useState, useSyncExternalStore } from "react";

import type { KbOut } from "@/lib/types";

export const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

function subscribeSearch(onStoreChange: () => void) {
  window.addEventListener("popstate", onStoreChange);
  return () => window.removeEventListener("popstate", onStoreChange);
}

function getSearch() {
  return window.location.search;
}

function getServerSearch() {
  return "";
}

function accessibleUrlKb(search: string): string | null {
  const raw = new URLSearchParams(search).get("kb_id");
  return raw && UUID_RE.test(raw) ? raw : null;
}

function withoutKb(search: string): string {
  const params = new URLSearchParams(search);
  params.delete("kb_id");
  const query = params.toString();
  return `${window.location.pathname}${query ? `?${query}` : ""}${window.location.hash}`;
}

/**
 * The URL is a navigation input, while the returned value is the only filter
 * source used for requests.  The visible KB list is the authorization
 * boundary for restoring a URL value after back/forward navigation.
 */
export function useKnowledgeBaseFilter(
  kbs: KbOut[],
  kbListLoaded: boolean,
): { kbFilter: string | null; selectKb: (next: string | null) => void } {
  const search = useSyncExternalStore(subscribeSearch, getSearch, getServerSearch);
  const urlKbFilter = accessibleUrlKb(search);
  const [kbFilter, setKbFilter] = useState<string | null>(urlKbFilter);

  useEffect(() => {
    if (!kbListLoaded) return;
    const next = urlKbFilter && kbs.some((kb) => kb.id === urlKbFilter) ? urlKbFilter : null;
    if (new URLSearchParams(search).has("kb_id") && next === null) {
      window.history.replaceState({}, "", withoutKb(search));
      window.dispatchEvent(new PopStateEvent("popstate"));
    }
    queueMicrotask(() => {
      setKbFilter((previous) => (previous === next ? previous : next));
    });
  }, [kbListLoaded, kbs, search, urlKbFilter]);

  const selectKb = useCallback(
    (next: string | null) => {
      if (next !== null && !kbs.some((kb) => kb.id === next)) return;
      setKbFilter(next);
      const params = new URLSearchParams(window.location.search);
      if (next) params.set("kb_id", next);
      else params.delete("kb_id");
      const query = params.toString();
      const href = `${window.location.pathname}${query ? `?${query}` : ""}${window.location.hash}`;
      window.history.pushState({}, "", href);
      window.dispatchEvent(new PopStateEvent("popstate"));
    },
    [kbs],
  );

  return { kbFilter, selectKb };
}

export interface ErrorQueryInput {
  page: number;
  pageSize: number;
  state?: string | null;
  cause?: string | null;
  kbId?: string | null;
  query?: string | null;
  linked?: "none";
}

/** Build the exact API query so null KB state cannot fall back to the URL. */
export function buildErrorQuery(input: ErrorQueryInput): string {
  const params = new URLSearchParams({
    page: String(input.page),
    page_size: String(input.pageSize),
  });
  if (input.state) params.set("state", input.state);
  if (input.cause) params.set("cause", input.cause);
  if (input.kbId) params.set("kb_id", input.kbId);
  if (input.query) params.set("q", input.query);
  if (input.linked) params.set("linked", input.linked);
  return params.toString();
}
