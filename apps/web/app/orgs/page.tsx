"use client";

// 机构（企业后台，Wave3）：建机构 / 凭邀请码加入 / 成员列表 / 机构库管理。
// 权限：机构库仅 admin 可建可写；member 只读（对话页联合检索可用）。

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
  // 机构列表（orgsReload 自增触发重新加载：effect 内联 IIFE 模式，同 kb 页）
  const [orgs, setOrgs] = useState<OrgOut[] | null>(null);
  const [orgsReload, setOrgsReload] = useState(0);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [banner, setBanner] = useState<string | null>(null);

  // 建机构 / 加入
  const [newName, setNewName] = useState("");
  const [creating, setCreating] = useState(false);
  const [joinCode, setJoinCode] = useState("");
  const [joining, setJoining] = useState(false);

  // 选中机构详情（detailOrgId 标记数据归属：切机构自然呈现加载态）
  const [members, setMembers] = useState<OrgMemberOut[] | null>(null);
  const [orgKbs, setOrgKbs] = useState<KbOut[] | null>(null);
  const [detailOrgId, setDetailOrgId] = useState<string | null>(null);
  const [newKbName, setNewKbName] = useState("");
  const [newKbSubject, setNewKbSubject] = useState("");
  const [creatingKb, setCreatingKb] = useState(false);
  const [copied, setCopied] = useState(false);

  const selected = orgs?.find((o) => o.id === selectedId) ?? null;
  const detailReady = detailOrgId === selectedId;

  const reloadOrgs = useCallback(() => setOrgsReload((k) => k + 1), []);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const list = await apiJson<OrgOut[]>("/api/orgs");
        if (!alive) return;
        setOrgs(list);
        setSelectedId((prev) =>
          prev && list.some((o) => o.id === prev) ? prev : (list[0]?.id ?? null),
        );
      } catch (err) {
        if (!alive) return;
        setOrgs((prev) => prev ?? []);
        setBanner(errText(err, "机构列表加载失败"));
      }
    })();
    return () => {
      alive = false;
    };
  }, [orgsReload]);

  // 选中机构 → 拉成员列表 + 该机构的库（从 /api/kb 过滤 org_id）
  useEffect(() => {
    if (!selectedId) return;
    let alive = true;
    (async () => {
      const [membersRes, kbsRes] = await Promise.allSettled([
        apiJson<OrgMemberOut[]>(`/api/orgs/${selectedId}/members`),
        apiJson<KbOut[]>("/api/kb"),
      ]);
      if (!alive) return;
      if (membersRes.status === "fulfilled") setMembers(membersRes.value);
      else setBanner("成员列表加载失败");
      if (kbsRes.status === "fulfilled") {
        setOrgKbs(kbsRes.value.filter((k) => k.org_id === selectedId));
      } else {
        setBanner("机构库列表加载失败");
      }
      setDetailOrgId(selectedId);
    })();
    return () => {
      alive = false;
    };
  }, [selectedId, orgsReload]);

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
      setOrgKbs((prev) => [...(prev ?? []), kb]);
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
              className="w-full rounded-lg border border-slate-300 px-3 py-1.5 text-sm outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
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
              className="w-full rounded-lg border border-slate-300 px-3 py-1.5 font-mono text-sm tracking-widest outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
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
            <p className="py-3 text-center text-xs text-slate-400">
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
                        : "text-slate-600 hover:bg-slate-100"
                    }`}
                  >
                    <span className="flex items-center gap-1.5">
                      <span className="min-w-0 flex-1 truncate">{o.name}</span>
                      <span
                        className={`shrink-0 rounded-full px-1.5 py-0.5 text-[10px] font-medium ${
                          o.role === "admin"
                            ? "bg-indigo-100 text-indigo-700"
                            : "bg-slate-100 text-slate-500"
                        }`}
                      >
                        {o.role === "admin" ? "管理员" : "成员"}
                      </span>
                    </span>
                    <span className="block text-xs font-normal text-slate-400">
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
                  <code className="rounded-lg bg-slate-100 px-4 py-2 font-mono text-lg font-semibold tracking-[0.3em] text-slate-800">
                    {selected.invite_code}
                  </code>
                  <button
                    type="button"
                    onClick={() => void copyCode()}
                    className="rounded-lg border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-600 hover:bg-slate-100"
                  >
                    {copied ? "已复制 ✓" : "复制"}
                  </button>
                  <span className="text-xs text-slate-400">
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
                      className="min-w-0 flex-1 rounded-lg border border-slate-300 px-3 py-1.5 text-sm outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
                    />
                    <input
                      value={newKbSubject}
                      onChange={(e) => setNewKbSubject(e.target.value)}
                      maxLength={64}
                      placeholder="学科（可选）"
                      className="w-32 rounded-lg border border-slate-300 px-3 py-1.5 text-sm outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
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
                  <p className="py-2 text-center text-xs text-slate-400">
                    {selected.role === "admin"
                      ? "还没有机构库，建库后到「知识库」页上传教材"
                      : "管理员还没有创建机构库"}
                  </p>
                ) : (
                  <ul className="divide-y divide-slate-100">
                    {orgKbs.map((kb) => (
                      <li key={kb.id} className="flex items-center gap-3 py-2.5">
                        <span className="min-w-0 flex-1">
                          <span className="block truncate text-sm font-medium text-slate-800">
                            {kb.name}
                          </span>
                          <span className="block text-xs text-slate-400">
                            {kb.subject} · v{kb.kb_version}
                          </span>
                        </span>
                        <a
                          href="/kb"
                          className="shrink-0 rounded-lg px-2.5 py-1 text-xs font-medium text-indigo-600 hover:bg-indigo-50"
                        >
                          {kb.writable ? "管理文档" : "查看文档"}
                        </a>
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
                      <tr className="border-b border-slate-200 text-xs text-slate-400">
                        <th className="py-2 pr-3 font-medium">邮箱</th>
                        <th className="py-2 pr-3 font-medium">角色</th>
                        <th className="py-2 font-medium">加入时间</th>
                      </tr>
                    </thead>
                    <tbody>
                      {members.map((m) => (
                        <tr key={m.user_id} className="border-b border-slate-100">
                          <td className="max-w-60 truncate py-2.5 pr-3 text-slate-800">
                            {m.email}
                          </td>
                          <td className="py-2.5 pr-3">
                            <span
                              className={`rounded-full px-2 py-0.5 text-xs font-medium ${
                                m.role === "admin"
                                  ? "bg-indigo-100 text-indigo-700"
                                  : "bg-slate-100 text-slate-500"
                              }`}
                            >
                              {m.role === "admin" ? "管理员" : "成员"}
                            </span>
                          </td>
                          <td className="whitespace-nowrap py-2.5 text-xs text-slate-400">
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
