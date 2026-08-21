"use client";

// 检索命中测试分区（admin 检索 tab）：四阶段召回/融合/重排全过程验证。

import { useEffect, useState } from "react";

import {
  Card,
  EmptyState,
  ErrorBanner,
  LoadingState,
} from "@/components/common";
import { RetrieveDebug } from "@/components/kb/retrieve-debug";
import type {KbOut} from "@/lib/types";
import { useApiResource } from "@/lib/use-api-resource";

/** 检索命中测试（管理端）：选可见库或手输任意 kb_id + query → 四阶段命中对比。 */
export function RetrievalTestSection() {
  // 知识库列表（A5 收敛: useApiResource + 单向同步回填下拉默认值）
  const kbListRes = useApiResource<KbOut[]>("/api/kb", {
    errorMessage: "知识库列表加载失败",
  });
  const kbs = kbListRes.data;
  const kbError = kbListRes.error;
  const [selectedId, setSelectedId] = useState("");
  const [manualId, setManualId] = useState("");

  /* eslint-disable react-hooks/set-state-in-effect -- 外部资源到达 → 下拉默认值回填, 无纯派生替代 */
  useEffect(() => {
    const list = kbListRes.data;
    if (!list) return;
    setSelectedId((prev) => prev || (list[0]?.id ?? ""));
  }, [kbListRes.data]);
  /* eslint-enable react-hooks/set-state-in-effect */

  // 手输 kb_id 优先（admin 可测任意库），否则用下拉选中的可见库
  const kbId = manualId.trim() || selectedId;

  return (
    <Card
      title="检索命中测试"
      extra={
        <span className="rounded-full bg-indigo-50 px-2 py-0.5 text-xs text-indigo-600">
          dense / sparse / RRF / rerank 四阶段全透明
        </span>
      }
    >
      <div className="space-y-3">
        {kbError && <ErrorBanner message={kbError} />}

        <div className="grid gap-2 sm:grid-cols-2">
          <label className="block">
            <span className="mb-1 block text-xs text-ink-muted">选择可见知识库</span>
            <select
              value={selectedId}
              onChange={(e) => setSelectedId(e.target.value)}
              className="w-full rounded-xl border border-line-strong bg-white px-3 py-2 text-sm text-ink outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
            >
              {(kbs ?? []).length === 0 && <option value="">（暂无可见知识库）</option>}
              {(kbs ?? []).map((kb) => (
                <option key={kb.id} value={kb.id}>
                  {kb.name}（{kb.subject} · v{kb.kb_version}）
                </option>
              ))}
            </select>
          </label>
          <label className="block">
            <span className="mb-1 block text-xs text-ink-muted">
              或直接输入 kb_id（优先生效）
            </span>
            <input
              value={manualId}
              onChange={(e) => setManualId(e.target.value)}
              placeholder="粘贴任意知识库 UUID"
              className="w-full rounded-xl border border-line-strong px-3 py-2 font-mono text-sm text-ink outline-none placeholder:font-sans placeholder:text-ink-muted focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
            />
          </label>
        </div>

        {kbs === null ? (
          <LoadingState text="知识库列表加载中…" />
        ) : !kbId ? (
          <EmptyState
            title="选择或输入一个知识库"
            description="然后输入 query，观察四阶段召回与 rerank 如何改变排序"
          />
        ) : (
          <RetrieveDebug key={kbId} kbId={kbId} />
        )}
      </div>
    </Card>
  );
}
