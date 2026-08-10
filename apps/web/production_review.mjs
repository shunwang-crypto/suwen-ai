// Production browser review and screenshot capture.
// Credentials are read from environment variables and are never written to disk.
import { chromium } from "playwright";
import { mkdirSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

const baseUrl = (process.env.EDU_PROD_URL ?? "").replace(/\/$/, "");
const email = process.env.EDU_PROD_EMAIL ?? "";
const password = process.env.EDU_PROD_PASSWORD ?? "";
const outdir = resolve(
  process.env.EDU_PROD_OUTDIR ??
    "../../docs/evidence/production-2026-08-09/screenshots",
);

if (!baseUrl || !email || !password) {
  throw new Error(
    "Set EDU_PROD_URL, EDU_PROD_EMAIL, and EDU_PROD_PASSWORD before running.",
  );
}

mkdirSync(outdir, { recursive: true });

const browser = await chromium.launch({
  headless: true,
  args: ["--no-sandbox"],
});
const context = await browser.newContext({
  viewport: { width: 1440, height: 1024 },
  ignoreHTTPSErrors: true,
  locale: "zh-CN",
  timezoneId: "Asia/Shanghai",
});
const page = await context.newPage();
const issues = [];
const checks = [];
let currentStep = "startup";

function recordIssue(kind, detail) {
  issues.push({ step: currentStep, kind, detail: String(detail).slice(0, 500) });
}

page.on("console", (message) => {
  if (message.type() === "error" || message.type() === "warning") {
    recordIssue(`console:${message.type()}`, message.text());
  }
});
page.on("pageerror", (error) => recordIssue("pageerror", error));
page.on("requestfailed", (request) =>
  recordIssue(
    "requestfailed",
    `${request.method()} ${request.url()} ${request.failure()?.errorText ?? ""}`,
  ),
);
page.on("response", (response) => {
  if (response.status() >= 400) {
    recordIssue(
      `http:${response.status()}`,
      `${response.request().method()} ${response.url()}`,
    );
  }
});

async function settle(milliseconds = 1400) {
  await page.waitForTimeout(milliseconds);
  await page.evaluate(() => window.scrollTo(0, 0));
}

async function capture(name, { fullPage = true } = {}) {
  currentStep = name;
  await settle();
  const metrics = await page.evaluate(() => ({
    title: document.title,
    h1: [...document.querySelectorAll("h1")]
      .map((element) => element.textContent?.trim())
      .filter(Boolean),
    horizontalOverflow:
      document.documentElement.scrollWidth - document.documentElement.clientWidth,
  }));
  await page.screenshot({ path: resolve(outdir, `${name}.png`), fullPage });
  checks.push({
    step: name,
    url: page.url(),
    ...metrics,
  });
  console.log(
    `SHOT ${name} ${page.url()}${
      metrics.horizontalOverflow > 0
        ? ` HORIZONTAL_OVERFLOW=${metrics.horizontalOverflow}px`
        : ""
    }`,
  );
}

async function visit(path, name, options = {}) {
  currentStep = name;
  const response = await page.goto(`${baseUrl}${path}`, {
    waitUntil: "domcontentloaded",
    timeout: 60_000,
  });
  await settle(options.settle ?? 1400);
  if (!response || response.status() >= 400) {
    recordIssue("navigation", `${path} returned ${response?.status() ?? "no response"}`);
  }
  if (new URL(page.url()).pathname === "/login" && path !== "/login") {
    recordIssue("auth-redirect", `${path} redirected to login`);
  }
  await capture(name, options);
}

async function login() {
  await visit("/login", "00-login");
  await page.locator('input[type="email"]').fill(email);
  await page.locator('input[type="password"]').fill(password);
  await Promise.all([
    page.waitForURL(/\/today(?:\?|$)/, { timeout: 30_000 }),
    page.getByRole("button", { name: /^登录$/ }).click(),
  ]);
  await capture("01-today");
}

async function reviewKnowledgeBase() {
  await visit("/kb", "02-knowledge-base");
  const preview = page.getByRole("button", { name: "分块预览" }).first();
  if (await preview.isVisible().catch(() => false)) {
    await preview.click();
    await page.getByRole("button", { name: "关闭分块预览" }).waitFor({
      state: "visible",
      timeout: 15_000,
    });
    await capture("03-knowledge-base-chunks");
    await page.getByRole("button", { name: "关闭分块预览" }).click();
  } else {
    recordIssue("missing-control", "No visible 分块预览 button");
  }
}

async function reviewChat() {
  await visit("/chat", "04-chat-new");

  // Bind the real personal knowledge base so the response exercises RAG and citations.
  await page.getByRole("button", { name: "添加内容" }).click();
  await page.getByRole("button", { name: /选择知识库/ }).click();
  const kbChoice = page.getByLabel("高数", { exact: true });
  if (await kbChoice.isVisible().catch(() => false)) {
    await kbChoice.check();
    await page.getByRole("button", { name: "使用所选资料" }).click();
  } else {
    recordIssue("missing-control", "Knowledge base 高数 was not selectable");
    await page.getByRole("button", { name: "关闭输入菜单" }).click();
  }

  const prompt = "泰勒公式的拉格朗日余项是什么形式？请用两句话回答。";
  const composer = page.locator('textarea[placeholder="给苏问发消息"]');
  await composer.fill(prompt);
  await page.getByRole("button", { name: "发送" }).click();

  const stop = page.getByRole("button", { name: "停止生成" });
  const started = await stop
    .waitFor({ state: "visible", timeout: 15_000 })
    .then(() => true)
    .catch(() => false);
  if (!started) recordIssue("chat", "Streaming indicator did not appear within 15s");
  await page.waitForTimeout(1800);
  await capture("05-chat-streaming");

  if (started) {
    await stop.waitFor({ state: "hidden", timeout: 120_000 }).catch(() => {
      recordIssue("chat", "Streaming did not finish within 120s");
    });
  }
  await page.waitForTimeout(1800);

  const timeline = page.getByRole("button", { name: /过程时间线/ }).last();
  if (await timeline.isVisible().catch(() => false)) await timeline.click();
  const citations = page.getByRole("button", { name: /条知识库引用/ }).last();
  if (await citations.isVisible().catch(() => false)) await citations.click();
  else recordIssue("chat", "Completed answer had no visible knowledge-base citation button");
  await capture("06-chat-answer");
}

async function reviewPages() {
  for (const [path, name] of [
    ["/tutor", "07-tutor"],
    ["/quiz", "08-quiz"],
    ["/reviews", "09-reviews"],
    ["/errors", "10-errors"],
    ["/resources", "11-resources"],
    ["/plans", "12-plans"],
    ["/profile", "13-profile"],
    ["/reports", "14-reports"],
    ["/orgs", "15-organizations"],
    ["/billing", "16-billing"],
  ]) {
    await visit(path, name, { settle: path === "/reports" ? 3000 : 1400 });
  }
}

async function reviewMobile() {
  await page.setViewportSize({ width: 390, height: 844 });
  await visit("/today", "17-today-mobile");
  await visit("/chat", "18-chat-mobile");
}

try {
  await login();
  await reviewKnowledgeBase();
  await reviewChat();
  await reviewPages();
  await reviewMobile();
} finally {
  const uniqueIssues = [
    ...new Map(
      issues.map((issue) => [JSON.stringify(issue), issue]),
    ).values(),
  ];
  writeFileSync(
    resolve(outdir, "review-results.json"),
    `${JSON.stringify(
      {
        reviewedAt: new Date().toISOString(),
        baseUrl,
        checks,
        issues: uniqueIssues,
      },
      null,
      2,
    )}\n`,
  );
  console.log(`RESULT ${resolve(outdir, "review-results.json")}`);
  console.log(`ISSUES ${uniqueIssues.length}`);
  await browser.close();
}
