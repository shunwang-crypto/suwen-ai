"use client";

// 机构（企业后台，Wave3）：建机构 / 凭邀请码加入 / 成员列表 / 机构库管理。
// 权限：机构库仅 admin 可建可写；member 只读（对话页联合检索可用）。

import Link from "next/link";
import { useCallback, useEffect, useState, type FormEvent } from "react";

import {
  Card,
  EmptyState,
  ErrorBanner,
  LoadingState,
  Spinner,
  fmtDateTime,
} from "@/components/common";
import { ApiError, apiJson } from "@/lib/api";
import type { KbOut, OrgMemberOut, OrgOut } from "@/lib/types";
import { useApiResource } from "@/lib/use-api-resource";

function errText(err: unknown, fallback: string): string {
  if (err instanceof ApiError) {
    const map: Record<string, string> = {
      INVITE_CODE_INVALID: "邀请码无效，请检查后重试",
      ALREADY_MEMBER: "你已经是该机构成员",
      ORG_ADMIN_REQUIRED: "仅机构管理员可执行此操作",
    };
    return map[err.code] ?? err.message;
  }
  return err instanceof Error ? err.message : fallback;
}

export default function OrgsPage() {
  // 机构列表 (useApiResource: 过期防护与重载由 hook 承担; 到达后回填 selectedId 见下方同步 effect)
  const orgsRes = useApiResource<OrgOut[]>("/api/orgs", { errorMessage: "机构列表加载失败" });
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [banner, setBanner] = useState<string | null>(null);

  // 建机构 / 加入
  const [newName, setNewName] = useState("");
  const [creating, setCreating] = useState(false);
  const [joinCode, setJoinCode] = useState("");
  const [joining, setJoining] = useState(false);

  // 选中机构详情 (条件加载: 未选中不发请求); orgKbs 为按选中机构过滤的派生值,
  // detailOrgId 标记数据归属：切机构自然呈现加载态
  const membersRes = useApiResource<OrgMemberOut[]>(
    selectedId ? `/api/orgs/${selectedId}/members` : null,
    { errorMessage: "成员列表加载失败" },
  );
  const kbRes = useApiResource<KbOut[]>(selectedId ? "/api/kb" : null, {
    errorMessage: "机构库列表加载失败",
  });
  const [detailOrgId, setDetailOrgId] = useState<string | null>(null);
  const [newKbName, setNewKbName] = useState("");
  const [newKbSubject, setNewKbSubject] = useState("");
  const [creatingKb, setCreatingKb] = useState(false);
  const [copied, setCopied] = useState(false);

  const orgs = orgsRes.data;
  const members = membersRes.data;
  const orgKbs = kbRes.data ? kbRes.data.filter((k) => k.org_id === selectedId) : null;
  const selected = orgs?.find((o) => o.id === selectedId) ?? null;
  const detailReady = detailOrgId === selectedId;

  const { reload: reloadOrgList } = orgsRes;
  const { reload: reloadMembers } = membersRes;
  const { reload: reloadKbs } = kbRes;
  // reload 均为 hook 内 useCallback 稳定引用
  const reloadOrgs = useCallback(() => {
    reloadOrgList();
    reloadMembers();
    reloadKbs();
  }, [reloadOrgList, reloadMembers, reloadKbs]);

  /* eslint-disable react-hooks/set-state-in-effect -- 外部资源到达/失败 → 本地表单或横幅的单向同步, 无纯派生替代 */
  // 列表到达后回填选中项: 保留仍存在的当前选中, 否则取第一个
  useEffect(() => {
    const list = orgsRes.data;
    if (!list) return;
    setSelectedId((prev) =>
      prev && list.some((o) => o.id === prev) ? prev : (list[0]?.id ?? null),
    );
  }, [orgsRes.data]);
  /* eslint-enable react-hooks/set-state-in-effect */

  /* eslint-disable react-hooks/set-state-in-effect -- 外部资源到达/失败 → 本地表单或横幅的单向同步, 无纯派生替代 */
  // 失败兜底同原实现: 保留已有列表 (无则空列表) 并以横幅提示
  useEffect(() => {
    if (!orgsRes.error) return;
    setBanner(orgsRes.error);
    orgsRes.setData((prev) => prev ?? []);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- setData 为稳定 setState 引用
  }, [orgsRes.error]);
  /* eslint-enable react-hooks/set-state-in-effect */

  // 成员/机构库任一失败 → 横幅 (原 allSettled 语义: 部分成功照常渲染)
  /* eslint-disable react-hooks/set-state-in-effect -- 外部资源到达/失败 → 本地表单或横幅的单向同步, 无纯派生替代 */
  useEffect(() => {
    if (membersRes.error) setBanner(membersRes.error);
  }, [membersRes.error]);
  useEffect(() => {
    if (kbRes.error) setBanner(kbRes.error);
  }, [kbRes.error]);
  /* eslint-enable react-hooks/set-state-in-effect */

  /* eslint-disable react-hooks/set-state-in-effect -- 外部资源到达/失败 → 本地表单或横幅的单向同步, 无纯派生替代 */
  // 两路均落定 (无论成败) 后标记详情归属 (原 setDetailOrgId(selectedId) 语义)
  useEffect(() => {
    if (!selectedId || membersRes.loading || kbRes.loading) return;
    setDetailOrgId(selectedId);
  }, [selectedId, membersRes.loading, kbRes.loading]);
  /* eslint-enable react-hooks/set-state-in-effect */

  const createOrg = async (e: FormEvent) => {
    e.preventDefault();
    const name = newName.trim();
    if (!name || creating) return;
    setCreating(true);
    setBanner(null);
    try {
      const org = await apiJson<OrgOut>("/api/orgs", { body: { name } });
      setNewName("");
      setSelectedId(org.id);
      reloadOrgs();
    } catch (err) {
      setBanner(errText(err, "创建机构失败"));
    } finally {
      setCreating(false);
    }
  };

  const joinOrg = async (e: FormEvent) => {
    e.preventDefault();
    const code = joinCode.trim().toUpperCase();
    if (!code || joining) return;
    setJoining(true);
    setBanner(null);
    try {
      const org = await apiJson<OrgOut>("/api/orgs/join", {
        body: { invite_code: code },
      });
      setJoinCode("");
      setSelectedId(org.id);
      reloadOrgs();
    } catch (err) {
      setBanner(errText(err, "加入机构失败"));
    } finally {
      setJoining(false);
    }
  };

  const createOrgKb = async (e: FormEvent) => {
    e.preventDefault();
    const name = newKbName.trim();
    if (!name || !selectedId || creatingKb) return;
    setCreatingKb(true);
    setBanner(null);
    try {
      const kb = await apiJson<KbOut>(`/api/orgs/${selectedId}/kb`, {
        body: { name, subject: newKbSubject.trim() || "general" },
      });
      setNewKbName("");
      setNewKbSubject("");
      kbRes.setData((prev) => [...(prev ?? []), kb]);
    } catch (err) {
      setBanner(errText(err, "创建机构库失败"));
    } finally {
      setCreatingKb(false);
    }
  };

  const copyCode = async () => {
    if (!selected?.invite_code) return;
    try {
      await navigator.clipboard.writeText(selected.invite_code);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // clipboard 不可用时静默（邀请码本身可见可手抄）
    }
  };

  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-4 p-4 lg:flex-row lg:items-start">
      {/* 左：我的机构 + 建/加入 */}
      <aside className="w-full shrink-0 space-y-4 lg:w-72">
        <Card title="创建机构">
          <form onSubmit={createOrg} className="space-y-2">
            <input
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              maxLength={255}
              placeholder="机构名称（如：高数教研组）"
              className="w-full rounded-lg border border-line-strong px-3 py-1.5 text-sm outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
            />
            <button
              type="submit"
              disabled={!newName.trim() || creating}
              className="flex w-full items-center justify-center gap-1.5 rounded-control bg-ink py-1.5 text-sm font-semibold text-white hover:bg-black disabled:cursor-not-allowed disabled:opacity-50"
            >
              {creating && (
                <Spinner className="h-3.5 w-3.5 border-white/40 border-t-white" />
              )}
              创建（你将成为管理员）
            </button>
          </form>
        </Card>

        <Card title="凭邀请码加入">
          <form onSubmit={joinOrg} className="space-y-2">
            <input
              value={joinCode}
              onChange={(e) => setJoinCode(e.target.value.toUpperCase())}
              maxLength={16}
              placeholder="输入 8 位邀请码"
              className="w-full rounded-lg border border-line-strong px-3 py-1.5 font-mono text-sm tracking-widest outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
            />
            <button
              type="submit"
              disabled={!joinCode.trim() || joining}
              className="flex w-full items-center justify-center gap-1.5 rounded-lg border border-indigo-200 bg-indigo-50 py-1.5 text-sm font-semibold text-indigo-700 hover:bg-indigo-100 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {joining && <Spinner className="h-3.5 w-3.5" />}
              加入机构
            </button>
          </form>
        </Card>

        <Card title="我的机构">
          {orgs === null ? (
            <LoadingState text="机构列表加载中…" />
          ) : orgs.length === 0 ? (
            <p className="py-3 text-center text-xs text-ink-muted">
              还没有加入任何机构
            </p>
          ) : (
            <ul className="space-y-1">
              {orgs.map((o) => (
                <li key={o.id}>
                  <button
                    type="button"
                    onClick={() => setSelectedId(o.id)}
                    className={`w-full rounded-lg px-3 py-2 text-left text-sm transition-colors ${
                      o.id === selectedId
                        ? "bg-indigo-50 font-medium text-indigo-700"
                        : "text-ink-secondary hover:bg-surface-muted"
                    }`}
                  >
                    <span className="flex items-center gap-1.5">
                      <span className="min-w-0 flex-1 truncate">{o.name}</span>
                      <span
                        className={`shrink-0 rounded-full px-1.5 py-0.5 text-caption font-medium ${
                          o.role === "admin"
                            ? "bg-indigo-100 text-indigo-700"
                            : "bg-surface-muted text-ink-muted"
                        }`}
                      >
                        {o.role === "admin" ? "管理员" : "成员"}
                      </span>
                    </span>
                    <span className="block text-xs font-normal text-ink-muted">
                      {o.member_count} 名成员
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </aside>

      {/* 右：选中机构详情 */}
      <div className="min-w-0 flex-1 space-y-4">
        {banner && <ErrorBanner message={banner} onClose={() => setBanner(null)} />}

        {!selected ? (
          <EmptyState
            title="创建或加入一个机构"
            description="机构管理员上传统一教材，全体成员即可共享同一知识底座进行联合检索问答"
          />
        ) : (
          <>
            {selected.role === "admin" && selected.invite_code && (
              <Card title="邀请码">
                <div className="flex flex-wrap items-center gap-3">
                  <code className="rounded-lg bg-surface-muted px-4 py-2 font-mono text-lg font-semibold tracking-[0.3em] text-ink">
                    {selected.invite_code}
                  </code>
                  <button
                    type="button"
                    onClick={() => void copyCode()}
                    className="rounded-lg border border-line-strong px-3 py-1.5 text-sm font-medium text-ink-secondary hover:bg-surface-muted"
                  >
                    {copied ? "已复制 ✓" : "复制"}
                  </button>
                  <span className="text-xs text-ink-muted">
                    分享给同事/学生，凭码加入后即自动挂载全部机构库（只读）
                  </span>
                </div>
              </Card>
            )}

            <Card
              title={`机构库 · ${selected.name}`}
              extra={
                <span className="rounded-full bg-amber-50 px-2 py-0.5 text-xs text-amber-700">
                  成员只读 · 管理员可写
                </span>
              }
            >
              <div className="space-y-3">
                {selected.role === "admin" && (
                  <form onSubmit={createOrgKb} className="flex flex-wrap gap-2">
                    <input
                      value={newKbName}
                      onChange={(e) => setNewKbName(e.target.value)}
                      maxLength={255}
                      placeholder="新机构库名称（如：统一教材库）"
                      className="min-w-0 flex-1 rounded-lg border border-line-strong px-3 py-1.5 text-sm outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
                    />
                    <input
                      value={newKbSubject}
                      onChange={(e) => setNewKbSubject(e.target.value)}
                      maxLength={64}
                      placeholder="学科（可选）"
                      className="w-32 rounded-lg border border-line-strong px-3 py-1.5 text-sm outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
                    />
                    <button
                      type="submit"
                      disabled={!newKbName.trim() || creatingKb}
                      className="flex items-center gap-1.5 rounded-control bg-ink px-3.5 py-1.5 text-sm font-semibold text-white hover:bg-black disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      {creatingKb && (
                        <Spinner className="h-3.5 w-3.5 border-white/40 border-t-white" />
                      )}
                      建库
                    </button>
                  </form>
                )}
                {!detailReady || orgKbs === null ? (
                  <LoadingState text="机构库加载中…" />
                ) : orgKbs.length === 0 ? (
                  <p className="py-2 text-center text-xs text-ink-muted">
                    {selected.role === "admin"
                      ? "还没有机构库，建库后到「知识库」页上传教材"
                      : "管理员还没有创建机构库"}
                  </p>
                ) : (
                  <ul className="divide-y divide-line">
                    {orgKbs.map((kb) => (
                      <li key={kb.id} className="flex items-center gap-3 py-2.5">
                        <span className="min-w-0 flex-1">
                          <span className="block truncate text-sm font-medium text-ink">
                            {kb.name}
                          </span>
                          <span className="block text-xs text-ink-muted">
                            {kb.subject} · v{kb.kb_version}
                          </span>
                        </span>
                        <Link
                          href="/courses"
                          className="shrink-0 rounded-lg px-2.5 py-1 text-xs font-medium text-indigo-600 hover:bg-indigo-50"
                        >
                          {kb.writable ? "管理文档" : "查看文档"}
                        </Link>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </Card>

            <Card title="成员">
              {!detailReady || members === null ? (
                <LoadingState text="成员加载中…" />
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full min-w-100 text-left text-sm">
                    <thead>
                      <tr className="border-b border-line text-xs text-ink-muted">
                        <th className="py-2 pr-3 font-medium">邮箱</th>
                        <th className="py-2 pr-3 font-medium">角色</th>
                        <th className="py-2 font-medium">加入时间</th>
                      </tr>
                    </thead>
                    <tbody>
                      {members.map((m) => (
                        <tr key={m.user_id} className="border-b border-line">
                          <td className="max-w-60 truncate py-2.5 pr-3 text-ink">
                            {m.email}
                          </td>
                          <td className="py-2.5 pr-3">
                            <span
                              className={`rounded-full px-2 py-0.5 text-xs font-medium ${
                                m.role === "admin"
                                  ? "bg-indigo-100 text-indigo-700"
                                  : "bg-surface-muted text-ink-muted"
                              }`}
                            >
                              {m.role === "admin" ? "管理员" : "成员"}
                            </span>
                          </td>
                          <td className="whitespace-nowrap py-2.5 text-xs text-ink-muted">
                            {fmtDateTime(m.joined_at)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </Card>
          </>
        )}
      </div>
    </div>
  );
}
