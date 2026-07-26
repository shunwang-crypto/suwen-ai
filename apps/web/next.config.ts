import type { NextConfig } from "next";

// 开发态同源代理: 前端与 API 同源, refresh cookie 不再受跨站 SameSite 限制
// (生产由 Caddy 做同一件事, 拓扑同构; 见 deploy/Caddyfile)
const API_ORIGIN = process.env.EDU_API_ORIGIN ?? "http://127.0.0.1:8000";

const nextConfig: NextConfig = {
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
