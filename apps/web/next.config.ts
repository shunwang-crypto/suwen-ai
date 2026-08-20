import type { NextConfig } from "next";

// 开发态同源代理: 前端与 API 同源, refresh cookie 不再受跨站 SameSite 限制
// Local development keeps browser requests same-origin through this proxy.
const API_ORIGIN = process.env.EDU_API_ORIGIN ?? "http://127.0.0.1:8000";

const nextConfig: NextConfig = {
  // 手机端会覆盖聊天输入框左侧“＋”；错误覆盖层仍会正常显示。
  devIndicators: false,
  // Next 16 blocks dev HMR when the app is opened through 127.0.0.1.
  // Keep both local URLs documented by the project fully interactive.
  allowedDevOrigins: ["localhost", "127.0.0.1"],
  rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${API_ORIGIN}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
