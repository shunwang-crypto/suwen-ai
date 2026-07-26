// 骨架屏占位块：shimmer 流光动画（样式基元 .skeleton 见 globals.css）。
// 契约 API（其他模块直接依赖，勿改签名）：
//   import { Skeleton } from "@/components/ui/skeleton";
//   <Skeleton className="h-4 w-32" />

export function Skeleton({ className = "" }: { className?: string }) {
  return <div aria-hidden="true" className={`skeleton ${className}`.trim()} />;
}
