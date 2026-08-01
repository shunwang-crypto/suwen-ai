"use client";

import Image from "next/image";
import { useEffect, useState } from "react";

import { ApiError, apiFetch } from "@/lib/api";
import type { LearningResourceOut } from "@/lib/types";

function extensionOf(filename: string | null): string {
  return filename?.slice(filename.lastIndexOf(".")).toLowerCase() ?? "";
}

function isImage(resource: LearningResourceOut): boolean {
  return resource.mime_type?.startsWith("image/") === true ||
    [".png", ".jpg", ".jpeg", ".webp"].includes(extensionOf(resource.source_filename));
}

function isPdf(resource: LearningResourceOut): boolean {
  return resource.mime_type === "application/pdf" || extensionOf(resource.source_filename) === ".pdf";
}

function filenameFor(resource: LearningResourceOut): string {
  return resource.source_filename?.trim() || "题目资料原文件";
}

export function SourcePreview({ resource }: { resource: LearningResourceOut }) {
  const sourceKey = `${resource.id}:${resource.source_url ?? ""}`;
  const [preview, setPreview] = useState<{ key: string; url: string } | null>(null);
  const [loadError, setLoadError] = useState<{ key: string; message: string } | null>(null);

  useEffect(() => {
    let alive = true;
    let createdUrl: string | null = null;

    if (!resource.source_url) return () => undefined;

    void apiFetch(resource.source_url)
      .then((response) => response.blob())
      .then((blob) => {
        if (!alive) return;
        createdUrl = URL.createObjectURL(blob);
        setPreview({ key: sourceKey, url: createdUrl });
      })
      .catch((reason: unknown) => {
        if (!alive) return;
        setLoadError({ key: sourceKey, message: reason instanceof ApiError ? reason.message : "原始文件暂时无法读取" });
      });

    return () => {
      alive = false;
      if (createdUrl) URL.revokeObjectURL(createdUrl);
    };
  }, [resource.source_url, sourceKey]);

  const filename = filenameFor(resource);
  const image = isImage(resource);
  const pdf = isPdf(resource);
  const objectUrl = preview?.key === sourceKey ? preview.url : null;
  const error = loadError?.key === sourceKey ? loadError.message : null;
  const loading = Boolean(resource.source_url) && !objectUrl && !error;

  if (!resource.source_url) {
    return (
      <div className="flex min-h-64 items-center justify-center rounded-[12px] border border-dashed border-line-strong bg-[#fafaf9] px-5 text-center text-[12px] text-ink-muted">
        这是手动创建的资料，还没有原始文件。你可以直接在右侧添加题目。
      </div>
    );
  }

  return (
    <section className="rounded-[12px] border border-line bg-white p-3 sm:p-4">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="text-[10px] font-medium uppercase tracking-[0.08em] text-ink-muted">原始来源</p>
          <p className="mt-1 truncate text-[12px] font-medium text-ink" title={filename}>{filename}</p>
        </div>
        {objectUrl && (
          <a
            href={objectUrl}
            download={filename}
            target={image || pdf ? "_blank" : undefined}
            rel={image || pdf ? "noreferrer" : undefined}
            className="shrink-0 rounded-[7px] border border-line-strong px-3 py-1.5 text-[10px] font-medium text-ink-secondary transition-colors hover:border-ink-muted hover:text-ink"
          >
            {image || pdf ? "打开原文件" : "下载原文件"}
          </a>
        )}
      </div>

      {loading && (
        <div className="mt-3 flex min-h-64 items-center justify-center rounded-[9px] bg-surface-muted text-[11px] text-ink-muted">
          正在加载来源预览…
        </div>
      )}
      {error && !loading && (
        <p className="mt-3 rounded-[8px] border border-danger-200 bg-danger-50 px-3 py-2 text-[11px] text-danger-700">{error}</p>
      )}
      {!loading && objectUrl && image && (
        <div className="mt-3 flex min-h-64 items-center justify-center overflow-hidden rounded-[9px] bg-[#fafaf9] p-2">
          <Image
            src={objectUrl}
            alt={filename}
            width={1200}
            height={900}
            unoptimized
            className="max-h-[58dvh] w-full rounded-[6px] object-contain"
          />
        </div>
      )}
      {!loading && objectUrl && pdf && (
        <iframe
          title={`${filename} PDF 预览`}
          src={objectUrl}
          className="mt-3 h-[58dvh] min-h-64 w-full rounded-[9px] border border-line bg-[#fafaf9]"
        />
      )}
      {!loading && objectUrl && !image && !pdf && (
        <div className="mt-3 flex min-h-64 flex-col items-center justify-center rounded-[9px] border border-dashed border-line-strong bg-[#fafaf9] px-5 text-center">
          <span className="flex h-12 w-12 items-center justify-center rounded-full bg-brand-50 text-[13px] font-semibold text-brand-700">
            {extensionOf(filename).replace(".", "").toUpperCase() || "FILE"}
          </span>
          <p className="mt-3 text-[12px] font-medium text-ink">{filename}</p>
          <p className="mt-1 text-[11px] text-ink-muted">Word、PPT 文件可安全下载后查看。</p>
        </div>
      )}
    </section>
  );
}
