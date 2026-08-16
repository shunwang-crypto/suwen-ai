import type { TimelineItem } from "@/components/chat/timeline";
import type {ChatAttachmentOut, CitationData, DoneData, InterruptData} from "@/lib/types";

export interface ChatMsg {
  id: string;
  role: "user" | "assistant";
  content: string;
  timeline: TimelineItem[];
  citations: CitationData[];
  done?: DoneData;
  errorText?: string;
  interrupt?: InterruptData;
  streaming?: boolean;
  /** 当前 token 流的重生成序号：变化时清空重排（防拼接双答案） */
  attempt?: number;
  /** 追问 chips（followups 事件；kb 路径 done 前下发） */
  followups?: string[];
  /** 发送时绑定的知识库（「考考我」chip 跳转 /quiz 用） */
  kbId?: string;
  kbName?: string;
  /** 回答未完成（断流/手动停止；历史消息读 meta.truncated）→ 角标 + 重问入口 */
  truncated?: boolean;
  /** 45s 无事件软超时（客户端合成 stall 事件）：仍在等待上游 */
  stalled?: boolean;
  attachments?: ChatAttachmentOut[];
  chatMode?: ChatMode;
  /** 回答评价 (0031 M2): good/bad 已提交; 未评价为 undefined */
  rating?: "good" | "bad";
  ratingReason?: string | null;
}

export type ChatMode = "fast" | "think" | "web" | "research";

export const MODE_LABELS: Record<string, string> = {
  cache: "缓存命中",
  replay: "离线回放",
  live: "真实调用",
  record: "真实调用",
  stub: "演示模式",
};

export const SAMPLE_QUESTIONS = [
  "用泰勒展开解释 $e^x$ 的近似计算",
  "什么是贝叶斯定理？举一个生活中的例子",
  "帮我梳理二次函数 $y=ax^2+bx+c$ 的图像性质",
];
