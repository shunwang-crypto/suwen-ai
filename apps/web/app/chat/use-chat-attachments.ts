"use client";

// 对话页附件域（A 瘦身自 page.tsx 拆出）：
// 附件上传（数量/单文件/总量校验按服务端 capabilities）、粘贴图片转附件、
// 移除与"发送前清空未用附件"。上限兜底值与历史行为一致（4 个 / 20MB）。

import { useCallback, useState } from "react";
import type { ChangeEvent, ClipboardEvent, Dispatch, SetStateAction } from "react";

import { ChatMode } from "@/components/chat/chat-msg";
import { ApiError, apiFetch, apiJson } from "@/lib/api";
import type { ChatAttachmentCapabilities, ChatAttachmentOut } from "@/lib/types";

export interface UseChatAttachmentsOptions {
  attachmentCapabilities: ChatAttachmentCapabilities | null;
  setBanner: Dispatch<SetStateAction<string | null>>;
  setComposerPanel: Dispatch<SetStateAction<"menu" | "mode" | "scope" | null>>;
  setChatMode: Dispatch<SetStateAction<ChatMode>>;
}

export function useChatAttachments(options: UseChatAttachmentsOptions) {
  const { attachmentCapabilities, setBanner, setComposerPanel, setChatMode } =
    options;

  const [attachments, setAttachments] = useState<ChatAttachmentOut[]>([]);
  const [uploadingAttachment, setUploadingAttachment] = useState(false);

  const discardPendingAttachments = useCallback(() => {
    if (attachments.length === 0) return;
    const pending = attachments;
    setAttachments([]);
    void Promise.allSettled(
      pending.map((attachment) =>
        apiJson<void>(`/api/chat/attachments/${attachment.id}`, {
          method: "DELETE",
        }),
      ),
    );
  }, [attachments]);

  const removeAttachment = useCallback(
    async (attachment: ChatAttachmentOut) => {
      setAttachments((prev) => prev.filter((item) => item.id !== attachment.id));
      try {
        await apiJson<void>(`/api/chat/attachments/${attachment.id}`, {
          method: "DELETE",
        });
      } catch (err) {
        setBanner(err instanceof ApiError ? err.message : "移除附件失败");
      }
    },
    [setBanner],
  );

  const uploadChatAttachmentFiles = async (files: File[]) => {
    if (files.length === 0 || uploadingAttachment) return;
    const maxCount = attachmentCapabilities?.max_files_per_message ?? 4;
    const available = maxCount - attachments.length;
    if (available <= 0 || files.length > available) {
      setBanner(`每条消息最多添加 ${maxCount} 个附件`);
      return;
    }
    const maxBytes = (attachmentCapabilities?.max_file_mb ?? 20) * 1024 * 1024;
    if (files.some((file) => file.size > maxBytes)) {
      setBanner(`当前对话单个附件不能超过 ${attachmentCapabilities?.max_file_mb ?? 20}MB`);
      return;
    }
    const existingBytes = attachments.reduce((sum, item) => sum + item.size_bytes, 0);
    if (existingBytes + files.reduce((sum, file) => sum + file.size, 0) > maxBytes) {
      setBanner(`本次消息的附件总大小不能超过 ${attachmentCapabilities?.max_file_mb ?? 20}MB`);
      return;
    }
    setUploadingAttachment(true);
    setComposerPanel(null);
    const uploaded: ChatAttachmentOut[] = [];
    const failures: string[] = [];
    for (const file of files) {
      try {
        const form = new FormData();
        form.append("file", file);
        const response = await apiFetch("/api/chat/attachments", {
          method: "POST",
          body: form,
        });
        uploaded.push((await response.json()) as ChatAttachmentOut);
      } catch (err) {
        failures.push(
          `${file.name}：${err instanceof ApiError ? err.message : "上传失败"}`,
        );
      }
    }
    setAttachments((prev) => [...prev, ...uploaded]);
    if (uploaded.length > 0) {
      // 联网/研究模式不支持附件：上传成功后自动回落到可用模式
      setChatMode((mode) =>
        mode === "web" ? "fast" : mode === "research" ? "think" : mode,
      );
    }
    setUploadingAttachment(false);
    setBanner(failures.length > 0 ? failures.join("；") : null);
  };

  const uploadChatAttachments = (event: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files ?? []);
    event.target.value = "";
    void uploadChatAttachmentFiles(files);
  };

  const handleComposerPaste = (event: ClipboardEvent<HTMLTextAreaElement>) => {
    const suffixByMime: Record<string, string> = {
      "image/png": "png",
      "image/jpeg": "jpg",
      "image/webp": "webp",
    };
    const files = Array.from(event.clipboardData.items)
      .filter((item) => item.kind === "file" && item.type.startsWith("image/"))
      .map((item) => item.getAsFile())
      .filter((file): file is File => file !== null)
      .map((file, index) => {
        const suffix = suffixByMime[file.type];
        if (!suffix || file.name.includes(".")) return file;
        return new File(
          [file],
          `粘贴图片-${Date.now()}-${index + 1}.${suffix}`,
          { type: file.type, lastModified: file.lastModified },
        );
      });
    if (files.length === 0) return;
    // textarea 无法直接承载位图；图片交给附件区。若剪贴板同时有文字，仍让浏览器正常粘贴文字。
    if (!event.clipboardData.getData("text/plain")) event.preventDefault();
    void uploadChatAttachmentFiles(files);
  };

  return {
    attachments,
    setAttachments,
    uploadingAttachment,
    discardPendingAttachments,
    removeAttachment,
    uploadChatAttachmentFiles,
    uploadChatAttachments,
    handleComposerPaste,
  };
}
