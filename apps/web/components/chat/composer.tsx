"use client";

/**
 * 聊天输入区 (从 app/chat/page.tsx 机械拆出, 渲染逻辑零变更):
 * 附件上传/移除、知识库范围选择、四种 chatMode、多行切换、发送/停止。
 */

import type { ChangeEvent, RefObject } from "react";

import type { ChatAttachmentOut, KbOut } from "@/lib/types";
import type { ChatMode } from "@/components/chat/chat-msg";

export interface ComposerProps {
  input: string;
  setInput: (value: string) => void;
  send: () => void;
  stop: () => void;
  streaming: boolean;
  attachments: ChatAttachmentOut[];
  uploadingAttachment: boolean;
  uploadChatAttachments: (event: ChangeEvent<HTMLInputElement>) => void;
  removeAttachment: (attachment: ChatAttachmentOut) => void;
  attachmentCapabilities: {
    document_suffixes?: string[];
    image_suffixes?: string[];
    max_files_per_message?: number;
    thinking_enabled?: boolean;
    web_search_enabled?: boolean;
  } | null;
  chatAttachmentInputRef: RefObject<HTMLInputElement | null>;
  textareaRef: RefObject<HTMLTextAreaElement | null>;
  composerMultiline: boolean;
  setComposerMultiline: (value: boolean) => void;
  composerPanel: "menu" | "mode" | "scope" | null;
  setComposerPanel: React.Dispatch<React.SetStateAction<"menu" | "mode" | "scope" | null>>;
  kbIds: string[];
  setKbIds: React.Dispatch<React.SetStateAction<string[]>>;
  kbs: KbOut[];
  chatMode: ChatMode;
  setChatMode: React.Dispatch<React.SetStateAction<ChatMode>>;
  setBanner: (value: string | null) => void;
  selectedKbLabel: string;
  selectedModeLabel: string;
  handleComposerKey: (event: React.KeyboardEvent<HTMLTextAreaElement>) => void;
  handleComposerPaste: (event: React.ClipboardEvent<HTMLTextAreaElement>) => void;
}

export function Composer(props: ComposerProps) {
  const {
    input,
    setInput,
    send,
    stop,
    streaming,
    attachments,
    uploadingAttachment,
    uploadChatAttachments,
    removeAttachment,
    attachmentCapabilities,
    chatAttachmentInputRef,
    textareaRef,
    composerMultiline,
    composerPanel,
    setComposerPanel,
    kbIds,
    setKbIds,
    kbs,
    chatMode,
    setChatMode,
    setBanner,
    selectedKbLabel,
    selectedModeLabel,
    handleComposerKey,
    handleComposerPaste,
  } = props;
  return (
    <div className="shrink-0 bg-canvas px-4 pb-6 pt-3">
      <div
        className={`relative mx-auto flex w-full max-w-[720px] flex-col border border-line bg-white px-2 py-2 shadow-subtle transition-[border-color,border-radius] focus-within:border-line-strong ${
          attachments.length > 0 || kbIds.length > 0 || composerMultiline
            ? "rounded-[24px]"
            : "rounded-full"
        }`}
      >
        <input
          ref={chatAttachmentInputRef}
          type="file"
          multiple
          accept={[
            ...(attachmentCapabilities?.document_suffixes ?? [
              ".pdf",
              ".docx",
              ".pptx",
              ".md",
              ".txt",
            ]),
            ...(attachmentCapabilities?.image_suffixes ?? []),
          ].join(",")}
          className="hidden"
          onChange={(event) => void uploadChatAttachments(event)}
        />

        {(attachments.length > 0 || kbIds.length > 0) && (
          <div className="flex flex-wrap gap-1.5 px-2 pb-2 pt-1">
            {kbIds.length > 0 && (
              <span className="inline-flex max-w-64 items-center gap-1.5 rounded-[7px] border border-line bg-surface-muted px-2 py-1 text-body-sm text-ink-secondary">
                <button
                  type="button"
                  onClick={() => setComposerPanel("scope")}
                  className="max-w-52 truncate hover:text-ink"
                  title={selectedKbLabel}
                >
                  知识库 · {selectedKbLabel}
                </button>
                <button
                  type="button"
                  onClick={() => setKbIds([])}
                  className="text-ink-muted hover:text-ink"
                  aria-label="清除知识库范围"
                >
                  ×
                </button>
              </span>
            )}
            {attachments.map((attachment) => (
              <span
                key={attachment.id}
                className="inline-flex max-w-64 items-center gap-1.5 rounded-[7px] border border-line bg-surface-muted px-2 py-1 text-body-sm text-ink-secondary"
              >
                <span aria-hidden>
                  {attachment.kind === "image" ? "图片" : "文件"}
                </span>
                <span className="truncate" title={attachment.filename}>
                  {attachment.filename}
                </span>
                <button
                  type="button"
                  onClick={() => void removeAttachment(attachment)}
                  className="shrink-0 text-ink-muted hover:text-ink"
                  aria-label={`移除 ${attachment.filename}`}
                >
                  ×
                </button>
              </span>
            ))}
          </div>
        )}

        <div className="flex min-h-10 items-end gap-1.5">
          <div className="relative">
            <button
              type="button"
              onClick={() =>
                setComposerPanel((value) => (value ? null : "menu"))
              }
              disabled={streaming || uploadingAttachment}
              className="flex h-10 w-10 items-center justify-center rounded-full text-2xl font-light text-ink-secondary transition-colors hover:bg-surface-muted disabled:opacity-40"
              aria-label="添加内容"
              aria-expanded={composerPanel === "menu"}
            >
              {uploadingAttachment ? (
                <span className="h-4 w-4 animate-spin rounded-full border-2 border-slate-300 border-t-slate-700" />
              ) : (
                "+"
              )}
            </button>
          </div>

          <textarea
            ref={textareaRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleComposerKey}
            onPaste={handleComposerPaste}
            rows={1}
            maxLength={8000}
            placeholder={streaming ? "正在生成回答…" : "给苏问发消息"}
            disabled={streaming}
            className="chat-composer-input my-2 min-h-6 min-w-0 flex-1 resize-none border-0 bg-transparent p-0 text-[15px] leading-6 text-slate-900 outline-none placeholder:text-slate-400 focus-visible:outline-none disabled:text-slate-400"
          />

          <div className="relative mb-0.5 shrink-0">
            <button
              type="button"
              onClick={() =>
                setComposerPanel((value) => (value === "mode" ? null : "mode"))
              }
              disabled={streaming}
              className="flex h-9 max-w-[190px] items-center gap-1.5 rounded-full px-2.5 text-xs font-medium text-ink-secondary transition-colors hover:bg-surface-muted disabled:opacity-40"
              aria-label="选择回答模式"
              aria-expanded={composerPanel === "mode" || composerPanel === "scope"}
              title={selectedModeLabel}
            >
              <span className="truncate">{selectedModeLabel}</span>
              <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" className="h-3.5 w-3.5 shrink-0" aria-hidden>
                <path d="m5 6 3 3 3-3" />
              </svg>
            </button>
          </div>

          {streaming ? (
            <button
              type="button"
              onClick={stop}
              title="停止生成"
              aria-label="停止生成"
              className="mb-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-full border border-slate-300 text-slate-600 transition-colors hover:bg-slate-100"
            >
              <span className="h-2.5 w-2.5 rounded-sm bg-slate-500" />
            </button>
          ) : (
            <button
              type="button"
              onClick={() => void send()}
              disabled={!input.trim() && attachments.length === 0}
              title="发送"
              aria-label="发送"
              className="mb-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-brand-600 text-white transition-colors hover:bg-brand-700 disabled:cursor-not-allowed disabled:opacity-40"
            >
              <svg
                viewBox="0 0 16 16"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.6"
                strokeLinecap="round"
                strokeLinejoin="round"
                className="h-4 w-4"
                aria-hidden
              >
                <path d="M8 12V4M4.5 7.5 8 4l3.5 3.5" />
              </svg>
            </button>
          )}
        </div>

        {composerPanel && (
          <>
            <button
              type="button"
              className="fixed inset-0 z-20 cursor-default"
              aria-label="关闭输入菜单"
              onClick={() => setComposerPanel(null)}
            />

            {composerPanel === "menu" && (
              <div className="absolute bottom-[58px] left-0 z-30 w-[280px] rounded-2xl border border-line bg-white p-2 shadow-floating">
                <button
                  type="button"
                  onClick={() => chatAttachmentInputRef.current?.click()}
                  className="flex w-full items-start gap-3 rounded-xl px-3 py-2.5 text-left hover:bg-surface-muted"
                >
                  <span className="mt-0.5 flex h-7 w-7 items-center justify-center rounded-lg border border-line text-lg" aria-hidden>＋</span>
                  <span>
                    <span className="block text-[13px] font-medium text-ink">添加图片和文件</span>
                    <span className="block text-body-sm leading-5 text-ink-muted">选择文件，或在输入框直接粘贴截图</span>
                    <span className="block text-caption leading-4 text-ink-muted">最多 4 个，合计 20MB</span>
                  </span>
                </button>
                <button
                  type="button"
                  onClick={() => setComposerPanel("scope")}
                  className="flex w-full items-start gap-3 rounded-xl px-3 py-2.5 text-left hover:bg-surface-muted"
                >
                  <span className="mt-0.5 flex h-7 w-7 items-center justify-center rounded-lg border border-line text-xs" aria-hidden>▤</span>
                  <span className="min-w-0 flex-1">
                    <span className="block text-[13px] font-medium text-ink">选择知识库</span>
                    <span className="block truncate text-body-sm leading-5 text-ink-muted">
                      {kbIds.length > 0 ? `当前：${selectedKbLabel}` : "基于指定资料回答"}
                    </span>
                  </span>
                </button>
              </div>
            )}

            {composerPanel === "mode" && (
              <div className="absolute bottom-[58px] right-0 z-30 w-[300px] rounded-2xl border border-line bg-white p-2 shadow-floating">
                <p className="px-3 pb-1.5 pt-1 text-body-sm font-medium text-ink-muted">回答方式</p>
                {([
                  ["fast", "极速", "快速直接回答"],
                  ["think", "深度思考", "用于复杂推理与综合分析"],
                  ["web", "网页搜索", "搜索实时信息并标注来源"],
                  ["research", "深度搜索", "深度思考并联网检索"],
                ] as const).map(([mode, title, description]) => {
                  const disabled =
                    ((mode === "think" || mode === "research") &&
                      !attachmentCapabilities?.thinking_enabled) ||
                    ((mode === "web" || mode === "research") &&
                      (!attachmentCapabilities?.web_search_enabled ||
                        attachments.length > 0));
                  return (
                    <button
                      key={mode}
                      type="button"
                      disabled={disabled}
                      onClick={() => {
                        setChatMode(mode);
                        if (mode === "web" || mode === "research") setKbIds([]);
                        setComposerPanel(null);
                        setBanner(null);
                      }}
                      className="flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-left hover:bg-surface-muted disabled:cursor-not-allowed disabled:opacity-40"
                    >
                      <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-surface-muted text-xs text-ink-secondary" aria-hidden>
                        {mode === "fast"
                          ? "↗"
                          : mode === "think"
                            ? "◇"
                            : mode === "web"
                              ? "◎"
                              : "✦"}
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="block text-[13px] font-medium text-ink">{title}</span>
                        <span className="block text-body-sm leading-5 text-ink-muted">
                          {(mode === "web" || mode === "research") &&
                          attachments.length > 0
                            ? "移除附件后可使用"
                            : disabled
                              ? "当前部署未配置"
                              : description}
                        </span>
                      </span>
                      {chatMode === mode && <span className="text-sm text-brand-700">✓</span>}
                    </button>
                  );
                })}
              </div>
            )}

            {composerPanel === "scope" && (
              <div className="absolute bottom-[58px] left-0 z-30 max-h-[380px] w-[310px] overflow-y-auto rounded-2xl border border-line bg-white p-2 shadow-floating">
                <button
                  type="button"
                  onClick={() => setComposerPanel("menu")}
                  className="mb-1 px-2 py-1 text-xs text-ink-muted hover:text-ink"
                >
                  ← 选择知识库
                </button>
                {kbs.length === 0 ? (
                  <p className="px-3 py-5 text-center text-xs leading-5 text-ink-muted">还没有可用的知识库，请先在知识库页面创建并上传资料。</p>
                ) : (
                  (["user", "org"] as const).map((ownerType) => {
                    const group = kbs.filter((kb) => kb.owner_type === ownerType);
                    if (group.length === 0) return null;
                    return (
                      <div key={ownerType} className="mt-1">
                        <p className="px-2.5 py-1 text-caption text-ink-muted">{ownerType === "user" ? "个人知识库" : "机构知识库"}</p>
                        {group.map((kb) => (
                          <label key={kb.id} className="flex cursor-pointer items-center gap-2 rounded-lg px-2.5 py-2 text-[13px] text-ink-secondary hover:bg-surface-muted">
                            <input
                              type="checkbox"
                              checked={kbIds.includes(kb.id)}
                              onChange={(event) => {
                                setKbIds((prev) =>
                                  event.target.checked
                                    ? [...prev, kb.id]
                                    : prev.filter((id) => id !== kb.id),
                                );
                                if (event.target.checked) {
                                  if (chatMode === "web") setChatMode("fast");
                                  if (chatMode === "research") setChatMode("think");
                                }
                              }}
                              className="h-3.5 w-3.5 accent-slate-900"
                            />
                            <span className="min-w-0 flex-1 truncate">{kb.name}</span>
                          </label>
                        ))}
                      </div>
                    );
                  })
                )}
                <div className="mt-2 flex gap-2">
                  {kbIds.length > 0 && (
                    <button
                      type="button"
                      onClick={() => {
                        setKbIds([]);
                        setComposerPanel(null);
                      }}
                      className="h-9 flex-1 rounded-lg border border-line text-xs text-ink-secondary hover:bg-surface-muted"
                    >
                      清除范围
                    </button>
                  )}
                  <button
                    type="button"
                    disabled={kbIds.length === 0}
                    onClick={() => {
                      if (chatMode === "web") setChatMode("fast");
                      if (chatMode === "research") setChatMode("think");
                      setComposerPanel(null);
                      setBanner(null);
                    }}
                    className="h-9 flex-1 rounded-lg bg-ink text-xs font-medium text-white disabled:cursor-not-allowed disabled:opacity-40"
                  >
                    使用所选资料
                  </button>
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
