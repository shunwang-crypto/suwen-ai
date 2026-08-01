# EduAgents Web

这是 EduAgents 的 Next.js 前端，包含学生学习界面以及教师、管理员专项页面。项目级说明见
[根 README](../../README.md)，本地开发、测试和环境要求见
[开发手册](../../docs/development.md)。

## 修改前必读

本目录使用的 Next.js 版本可能包含超出模型训练数据的约定。任何开发者或 AI 编程代理都
必须先完整阅读 [AGENTS.md](AGENTS.md)，再按其要求查阅本地
`node_modules/next/dist/docs/` 中与目标功能相关的文档。

## 命令

在本目录运行：

~~~bash
npm ci
npm run dev
npm run lint
npm run build
npm run start
~~~

- `dev`：本地开发服务器。
- `lint`：ESLint。
- `build`：TypeScript 与 Next.js production build，是提交前必跑项。
- `start`：运行已经构建的生产 bundle。

本地默认访问 `http://127.0.0.1:3000`。API 通过 Next.js 同源代理访问根仓库中运行的
FastAPI；完整三进程启动方式见[开发手册](../../docs/development.md)。

## 边界

- Web 不保存权威业务状态，所有权限和状态机必须由 API 再校验。
- 前端隐藏按钮不是授权；教师和管理员端点必须继续使用服务端角色依赖。
- API 枚举、错误码和 PUT/PATCH 语义变化必须同步
  [API 契约](../../docs/api-contract.md)与 `apps/web/lib/types.ts`。
- `apps/web/CLAUDE.md` 和 `AGENTS.md` 是编程代理约束，不是产品文档。
