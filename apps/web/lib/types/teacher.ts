// 与 apps/api/src/eduagents_api/schemas.py 及 docs/api-contract.md 对齐的类型定义。

// ---- 教师组卷导出（对齐 routers/teacher.py）----

export interface TeacherQuizItem {
  id: string;
  title: string;
  status: string;
  created_at: string;
  kb_id: string | null;
  num_questions: number;
  /** easy/mid/hard → 题数 */
  difficulty_counts: Record<string, number>;
}

export interface TeacherExportQuestion {
  index: number;
  id: string;
  type: string;
  type_label: string;
  stem: string;
  options: string[];
  answer: string;
  explanation: string;
  difficulty: string;
  difficulty_label: string;
  kc_ids: string[];
}

export interface TeacherExportJson {
  format: "json";
  exported_at: string;
  exported_by: string;
  quiz: {
    id: string;
    title: string;
    status: string;
    created_at: string;
    kb_id: string | null;
    blueprint: Record<string, unknown>;
  };
  questions: TeacherExportQuestion[];
}

export interface TeacherPrintItem {
  index: number;
  type: string;
  type_label: string;
  stem: string;
  options: string[];
  difficulty: string;
  difficulty_label: string;
  points: number;
}

export interface TeacherPrintAnswer {
  index: number;
  answer: string;
  explanation: string;
}

export interface TeacherExportPrint {
  format: "print";
  exported_at: string;
  exported_by: string;
  quiz: { id: string; title: string; created_at: string; total: number };
  paper: { title: string; instructions: string; items: TeacherPrintItem[] };
  answer_key: { title: string; items: TeacherPrintAnswer[] };
}
