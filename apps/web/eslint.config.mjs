import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

const eslintConfig = defineConfig([
  ...nextVitals,
  ...nextTs,
  // Override default ignores of eslint-config-next.
  globalIgnores([
    // Default ignores of eslint-config-next:
    ".next/**",
    "out/**",
    "build/**",
    "next-env.d.ts",
  ]),
  // 设计系统一致性护栏（防止裸 Tailwind 灰阶 / 任意像素圆角回归）。
  // 作用域与一次性 codemod 相同：app/ + components/ 的非测试源文件——
  // 测试文件刻意保留 className 断言作为安全网，故排除。
  {
    files: ["app/**/*.{ts,tsx}", "components/**/*.{ts,tsx}"],
    ignores: ["**/*.test.*", "**/*.spec.*", "**/__tests__/**", "**/tests/**"],
    rules: {
      "no-restricted-syntax": [
        "error",
        {
          selector:
            "Literal[value=/(?:slate|gray|zinc|neutral|stone)-(?:950|900|800|700|600|500|400|300|200|100|50)\\b/]",
          message:
            "用语义色 token（ink / ink-secondary / ink-muted / line / line-strong / surface-muted）替代裸灰阶，见 globals.css @theme。",
        },
        {
          selector:
            "TemplateElement[value.raw=/(?:slate|gray|zinc|neutral|stone)-(?:950|900|800|700|600|500|400|300|200|100|50)\\b/]",
          message:
            "用语义色 token（ink / ink-secondary / ink-muted / line / line-strong / surface-muted）替代裸灰阶，见 globals.css @theme。",
        },
        {
          selector:
            "Literal[value=/rounded(?:-(?:t|b|l|r|s|e|tl|tr|bl|br|ss|se|es|ee))?-\\[(?:6|7|8|9|10|11|12|13|14|16)px\\]/]",
          message: "圆角用三档 token（rounded-control / rounded-overlay / rounded-card），勿写任意像素值。",
        },
        {
          selector:
            "TemplateElement[value.raw=/rounded(?:-(?:t|b|l|r|s|e|tl|tr|bl|br|ss|se|es|ee))?-\\[(?:6|7|8|9|10|11|12|13|14|16)px\\]/]",
          message: "圆角用三档 token（rounded-control / rounded-overlay / rounded-card），勿写任意像素值。",
        },
      ],
    },
  },
]);

export default eslintConfig;
