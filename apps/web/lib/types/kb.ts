// 与 apps/api/src/eduagents_api/schemas.py 及 docs/api-contract.md 对齐的类型定义。

// ---- 知识库（对齐 routers/kb.py）----

export interface DocumentOut {
  id: string;
  kb_id: string;
  filename: string;
  size_bytes: number;
  status: string;
  parse_backend: string;
  error_msg: string | null;
  recognition_meta_json: {
    /** 持久化的知识增强任务 claim；存在时即使 status=enriched 也仍在刷新。 */
    enrich_job_id?: string;
    enrich_previous_status?: string;
    enrich_started_at?: string;
    enrich_job_state?: "queued" | "running";
    page_count?: number;
    pages?: Array<{
      page_number: number;
      extraction_method: "native_text" | "scanned" | "mixed" | "failed";
      recognition_status: "ready" | "failed";
      native_text_chars: number;
      image_count: number;
      vision_used: boolean;
      warning_code?: string;
      error_code?: string;
    }>;
    warnings?: string[];
  };
  version: number;
  created_at: string;
  updated_at: string;
}

export interface ChunkOut {
  id: string;
  parent_id: string | null;
  text: string;
  heading_path: string;
  page_start: number | null;
  page_end: number | null;
  kc_ids: unknown[];
  embedding_status: string;
}

// ---- 学习指南（GET /api/kb/{kb_id}/study-guide，对齐 worker/study_guide.py）----

export interface StudyGuideOutlineItem {
  title: string;
  summary: string;
  kc_names: string[];
}

export interface StudyGuideTerm {
  term: string;
  definition: string;
}

export interface StudyGuide {
  outline: StudyGuideOutlineItem[];
  glossary: StudyGuideTerm[];
  suggested_questions: string[];
  version: number;
  generated_at: string;
  doc_count: number;
}

export interface StudyGuideOut {
  kb_id: string;
  /** 未生成时为 null（前端据此显示「生成学习指南」按钮） */
  guide: StudyGuide | null;
  generation: {
    status: "idle" | "queued" | "running" | "completed" | "failed";
    job_id?: string;
    requested_at?: string;
    started_at?: string;
    finished_at?: string;
    message?: string;
  };
  writable: boolean;
}

/** 引用回原文定位（GET /api/kb/{kb_id}/chunks/{chunk_id}/context）。 */
export interface ChunkContextItem {
  id: string;
  text: string;
  heading_path: string;
  page_start: number | null;
  page_end: number | null;
}

export interface ChunkContextOut {
  chunk: ChunkContextItem;
  doc_id: string;
  doc_name: string;
  prev: ChunkContextItem[];
  next: ChunkContextItem[];
  parent_text: string | null;
  parent_heading_path: string | null;
}

export interface RetrieveDebugChunk {
  chunk_id: string;
  score: number;
  rerank_score: number | null;
  source: string;
  heading_path: string;
  page_start: number | null;
  text: string;
}

/** 四阶段命中测试的单阶段条目（score 语义随阶段而变：cos/bm25/rrf/rerank）。 */
export interface RetrieveStageEntry {
  chunk_id: string;
  score: number | null;
}

export interface RetrieveChunkMeta {
  text: string;
  heading_path: string;
  page_start: number | null;
}

export interface RetrieveDebugStages {
  dense: RetrieveStageEntry[];
  sparse: RetrieveStageEntry[];
  fused: RetrieveStageEntry[];
  reranked: RetrieveStageEntry[];
  chunk_meta: Record<string, RetrieveChunkMeta>;
}

export interface RetrieveDebugResult {
  query: string;
  dense_hits: number;
  sparse_hits: number;
  used_sparse: boolean;
  used_rerank: boolean;
  confident: boolean;
  chunks: RetrieveDebugChunk[];
  stages?: RetrieveDebugStages;
}
