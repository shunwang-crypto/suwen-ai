// End-to-end production acceptance flow for the Suwen learning journey.
// The script intentionally mutates the supplied test account: it creates a
// dedicated knowledge base, uploads one document, starts a tutor session,
// creates a quiz, submits answers, and verifies learning-state propagation.
import { chromium } from "playwright";
import { mkdirSync, writeFileSync } from "node:fs";
import { basename, resolve } from "node:path";

const baseUrl = (process.env.EDU_PROD_URL ?? "").replace(/\/$/, "");
const email = process.env.EDU_PROD_EMAIL ?? "";
const password = process.env.EDU_PROD_PASSWORD ?? "";
const inputFile = resolve(process.env.EDU_FLOW_INPUT ?? "");
const outdir = resolve(
  process.env.EDU_FLOW_OUTDIR ??
    "../../docs/evidence/production-2026-08-09/full-flow",
);
const runName =
  process.env.EDU_FLOW_NAME ??
  `线上验收·二次函数·${new Date().toISOString().slice(11, 16).replace(":", "")}`;
const existingKbId = process.env.EDU_FLOW_EXISTING_KB_ID ?? "";
const editQuizBlueprint = process.env.EDU_FLOW_EDIT_BLUEPRINT !== "0";
const runManualLearningLoop = process.env.EDU_FLOW_MANUAL_LOOP === "1";

if (!baseUrl || !email || !password || !process.env.EDU_FLOW_INPUT) {
  throw new Error(
    "Set EDU_PROD_URL, EDU_PROD_EMAIL, EDU_PROD_PASSWORD, and EDU_FLOW_INPUT.",
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
const steps = [];
const issues = [];
const sseTranscripts = [];
const sseCaptureTasks = [];
let currentStep = "startup";
let kbId = "";

function issue(kind, detail) {
  const row = { step: currentStep, kind, detail: String(detail).slice(0, 700) };
  issues.push(row);
  console.log(`ISSUE ${kind} ${row.detail}`);
}

page.on("console", (message) => {
  if (["error", "warning"].includes(message.type())) {
    issue(`console:${message.type()}`, message.text());
  }
});
page.on("pageerror", (error) => issue("pageerror", error));
page.on("requestfailed", (request) =>
  issue(
    "requestfailed",
    `${request.method()} ${request.url()} ${request.failure()?.errorText ?? ""}`,
  ),
);
page.on("response", (response) => {
  if (response.status() >= 400) {
    issue(
      `http:${response.status()}`,
      `${response.request().method()} ${response.url()}`,
    );
  }
  if (
    response.url().includes("/api/quiz") ||
    response.url().includes("/api/threads/")
  ) {
    const task = (async () => {
      try {
        await response.finished();
        const contentType = response.headers()["content-type"] ?? "";
        if (contentType.includes("text/event-stream")) {
          sseTranscripts.push({
            url: response.url(),
            status: response.status(),
            body: (await response.text()).slice(0, 80_000),
          });
        }
      } catch (error) {
        sseTranscripts.push({
          url: response.url(),
          status: response.status(),
          captureError: String(error),
        });
      }
    })();
    sseCaptureTasks.push(task);
  }
});

async function shot(name, { fullPage = true, settle = 1000 } = {}) {
  currentStep = name;
  await page.waitForTimeout(settle);
  const snapshot = await page.evaluate(() => ({
    title: document.title,
    h1: [...document.querySelectorAll("h1")]
      .map((node) => node.textContent?.trim())
      .filter(Boolean),
    text: document.body.innerText.replace(/\s+/g, " ").slice(0, 900),
    horizontalOverflow:
      document.documentElement.scrollWidth - document.documentElement.clientWidth,
  }));
  await page.screenshot({ path: resolve(outdir, `${name}.png`), fullPage });
  steps.push({ step: name, url: page.url(), ...snapshot });
  console.log(`SHOT ${name}`);
}

async function goto(path, name, settle = 1000) {
  currentStep = name;
  const response = await page.goto(`${baseUrl}${path}`, {
    waitUntil: "domcontentloaded",
    timeout: 60_000,
  });
  if (!response || response.status() >= 400) {
    issue("navigation", `${path}: ${response?.status() ?? "no response"}`);
  }
  await shot(name, { settle });
}

async function waitUntil(fn, timeout, description, interval = 1500) {
  const deadline = Date.now() + timeout;
  let lastValue;
  while (Date.now() < deadline) {
    try {
      lastValue = await fn();
      if (lastValue) return lastValue;
    } catch {
      // The page may be replacing a transient DOM node; retry until timeout.
    }
    await page.waitForTimeout(interval);
  }
  throw new Error(`${description} timed out after ${timeout}ms (last=${lastValue})`);
}

async function login() {
  await goto("/login", "00-login");
  await page.locator('input[type="email"]').fill(email);
  await page.locator('input[type="password"]').fill(password);
  await Promise.all([
    page.waitForURL(/\/today(?:\?|$)/, { timeout: 30_000 }),
    page.getByRole("button", { name: /^登录$/ }).click(),
  ]);
  await shot("01-login-success");
}

async function readBalance(name) {
  await goto("/billing", name, 1500);
  const text = await page.locator("body").innerText();
  const match = text.match(/当前余额\s*([0-9.]+)\s*额度/);
  return match ? Number(match[1]) : null;
}

async function createAndUploadKnowledgeBase() {
  await goto("/kb", "03-kb-before-create", 1800);
  await page.getByRole("button", { name: "新建知识库" }).first().click();
  await page.getByPlaceholder("知识库名称").fill(runName);
  await page.getByPlaceholder("学科（可选）").fill("数学·线上验收");
  await page.getByRole("button", { name: /^创建$/ }).click();
  await page.getByRole("heading", { name: runName, exact: true }).waitFor({
    state: "visible",
    timeout: 30_000,
  });
  kbId = await page.locator('select[aria-label="切换知识库"]').inputValue();
  if (!kbId) throw new Error("Created knowledge base has no selected ID");
  await shot("04-kb-created");

  const filename = basename(inputFile);
  const fileInput = page.locator('input[type="file"]').first();
  await fileInput.setInputFiles(inputFile);
  const row = page.locator("tr").filter({ hasText: filename });
  await row.waitFor({ state: "visible", timeout: 30_000 });
  await shot("05-kb-uploaded-processing");

  const finalStatus = await waitUntil(
    async () => {
      const text = await row.innerText();
      if (text.includes("失败")) return `FAILED:${text}`;
      if (text.includes("已增强")) return "enriched";
      if (text.includes("已完成")) return "ready";
      return "";
    },
    180_000,
    "knowledge-base ingestion",
    2000,
  );
  if (String(finalStatus).startsWith("FAILED:")) {
    throw new Error(`Knowledge-base ingestion failed: ${finalStatus}`);
  }
  await shot("06-kb-ingestion-ready");

  await row.getByRole("button", { name: "分块预览" }).click();
  await page.getByRole("button", { name: "关闭分块预览" }).waitFor({
    state: "visible",
    timeout: 20_000,
  });
  await page.getByText(/第一章|极限与连续|导数与微分|高等数学讲义/).first().waitFor({
    state: "visible",
    timeout: 30_000,
  });
  await shot("07-kb-chunks");
  await page.getByRole("button", { name: "关闭分块预览" }).click();

  const advanced = page.locator("summary").filter({ hasText: "检索检查" });
  await advanced.click();
  const retrieveInput = page.getByPlaceholder(/输入检索 query/);
  await retrieveInput.fill("二次函数 y=x²-4x+3 的顶点和零点");
  await page.getByRole("button", { name: "检索" }).click();
  await page.waitForTimeout(3000);
  await shot("08-kb-retrieval-debug");
}

async function waitForStreamingToFinish() {
  const stop = page.getByRole("button", { name: "停止生成" });
  const started = await stop
    .waitFor({ state: "visible", timeout: 20_000 })
    .then(() => true)
    .catch(() => false);
  if (!started) issue("stream", "Streaming indicator did not appear");
  if (started) {
    await stop.waitFor({ state: "hidden", timeout: 150_000 }).catch(() => {
      issue("stream", "Streaming did not finish within 150 seconds");
    });
  }
  await page.waitForTimeout(1500);
}

async function testRagChat() {
  await goto(
    `/chat?new=1&kb=${encodeURIComponent(kbId)}`,
    "09-chat-ready",
    1800,
  );
  const prompt =
    "根据刚上传的线上验收讲义，函数 y=x^2-4x+3 的顶点和两个零点分别是什么？";
  await page.locator('textarea[placeholder="给苏问发消息"]').fill(prompt);
  await page.getByRole("button", { name: "发送" }).click();
  await page.waitForTimeout(1700);
  await shot("10-chat-streaming", { settle: 0 });
  await waitForStreamingToFinish();

  const body = await page.locator("body").innerText();
  if (!body.includes("(2,-1)") && !body.includes("(2, -1)")) {
    issue("rag-assertion", "Expected vertex (2,-1) was not visible in the answer");
  }
  if (!body.includes("x=1") && !body.includes("x = 1")) {
    issue("rag-assertion", "Expected zero x=1 was not visible in the answer");
  }
  await shot("11-chat-answer");

  const citationButton = page.getByRole("button", { name: /条知识库引用/ }).last();
  if (await citationButton.isVisible().catch(() => false)) {
    await citationButton.click();
    await shot("12-chat-citations");
  } else {
    issue("rag-citations", "No citation button appeared for the bound knowledge base");
  }

  const timeline = page.getByRole("button", { name: /过程时间线/ }).last();
  if (await timeline.isVisible().catch(() => false)) {
    await timeline.click();
    await shot("13-chat-process-timeline");
  } else {
    issue("rag-timeline", "No process timeline button appeared");
  }
}

async function testTutor() {
  const problem = "已知 y=x^2-4x+3，请用配方法求顶点，并引导我自己完成。";
  await goto(
    `/tutor?kb=${encodeURIComponent(kbId)}&problem=${encodeURIComponent(problem)}`,
    "14-tutor-problem",
    1800,
  );
  await page.getByRole("button", { name: "校对并确认" }).click();
  await page.getByRole("button", { name: "确认并开始辅导" }).waitFor({
    state: "visible",
    timeout: 20_000,
  });
  await shot("15-tutor-confirmation");
  await page.getByRole("button", { name: "确认并开始辅导" }).click();

  const busyText = page.getByText("老师正在整理下一步提示…", { exact: true });
  await busyText.waitFor({ state: "visible", timeout: 20_000 }).catch(() => {});
  await busyText.waitFor({ state: "hidden", timeout: 150_000 }).catch(() => {
    issue("tutor", "Initial tutor response did not finish within 150 seconds");
  });
  await shot("16-tutor-first-guidance");

  const hintButton = page.getByRole("button", { name: "需要提示", exact: true });
  if (await hintButton.isVisible().catch(() => false)) {
    await hintButton.click();
    await busyText.waitFor({ state: "visible", timeout: 20_000 }).catch(() => {});
    await busyText.waitFor({ state: "hidden", timeout: 150_000 }).catch(() => {
      issue("tutor", "Tutor hint did not finish within 150 seconds");
    });
    await shot("17-tutor-next-hint");
  } else {
    issue("tutor", "Need-hint button was not available after first response");
  }
}

async function testQuiz() {
  await goto(
    `/quiz?kb=${encodeURIComponent(kbId)}&topic=${encodeURIComponent("二次函数基础")}`,
    "18-quiz-config",
    1800,
  );
  const form = page.locator("form").filter({ hasText: "练习主题" }).first();
  await form.locator("select").selectOption(kbId);
  const numberInputs = form.locator('input[type="number"]');
  await numberInputs.nth(0).fill("1");
  await numberInputs.nth(1).fill("1");
  await numberInputs.nth(2).fill("0");
  await shot("19-quiz-configured");
  await form.getByRole("button", { name: "开始出题" }).click();

  const confirmBlueprint = page.getByRole("button", {
    name: "确认并生成题目",
  });
  await confirmBlueprint.waitFor({ state: "visible", timeout: 150_000 });
  await shot("20-quiz-blueprint");

  // The first run edits the blueprint to exercise learner control. A retry can
  // disable this with EDU_FLOW_EDIT_BLUEPRINT=0 to isolate the resume branch.
  const firstAdjust = page.getByRole("button", { name: "调整" }).first();
  if (editQuizBlueprint && (await firstAdjust.isVisible().catch(() => false))) {
    await firstAdjust.click();
    await page.locator('label:has-text("题型") select').first().selectOption("blank");
  }
  await shot("21-quiz-blueprint-edited");
  await confirmBlueprint.click();

  const submit = page.getByRole("button", { name: "提交答卷" });
  await submit.waitFor({ state: "visible", timeout: 180_000 });
  await shot("22-quiz-generated");

  const questionCards = page.locator("ol.space-y-4 > li");
  const questionCount = await questionCards.count();
  for (let index = 0; index < questionCount; index += 1) {
    const card = questionCards.nth(index);
    const blank = card.locator('input[placeholder="填写答案"]');
    const short = card.locator('textarea[placeholder*="简答题"]');
    const radio = card.locator('input[type="radio"]');
    const checkbox = card.locator('input[type="checkbox"]');
    if (await blank.count()) await blank.fill(`故意错误答案-${index + 1}`);
    else if (await short.count()) await short.fill(`线上验收作答-${index + 1}`);
    else if (await radio.count()) await radio.first().check();
    else if (await checkbox.count()) await checkbox.first().check();
    else issue("quiz-answer", `Question ${index + 1} had no supported answer control`);
  }
  await shot("23-quiz-answered");
  await submit.click();
  await page.getByText(/正确率\s*\d+%/).first().waitFor({
    state: "visible",
    timeout: 150_000,
  });
  await shot("24-quiz-results");
}

async function verifyLearningLoop() {
  await goto("/errors", "25-errors-after-quiz", 1800);
  await goto("/reviews", "26-reviews-after-quiz", 1800);
  await goto("/profile", "27-profile-after-quiz", 2200);
  const refreshProfile = page.getByRole("button", { name: "刷新画像" });
  if (await refreshProfile.isVisible().catch(() => false)) {
    await refreshProfile.click();
    await page.waitForTimeout(1800);
    await shot("28-profile-refreshed");
  }
  await goto("/reports", "29-reports-after-quiz", 4500);
  await goto("/today", "30-today-after-flow", 1800);
}

async function testManualErrorAndReview() {
  const stem =
    "线上验收题：函数 y=2x^2-8x+5 的对称轴是什么？（2026-08-09）";
  await goto("/errors", "32-errors-before-manual-entry", 1800);
  await page.getByRole("button", { name: "录入错题" }).click();
  await page.getByRole("button", { name: "手动录入" }).click();
  const panel = page.locator("section").filter({ hasText: "录入错题" }).last();
  await panel.locator('label:has-text("题型") select').selectOption("blank");
  await panel.locator('label:has-text("难度") select').selectOption("easy");
  await panel.locator('label:has-text("错因") select').selectOption("knowledge_gap");
  await panel.getByPlaceholder("完整输入题干和条件").fill(stem);
  await panel.getByPlaceholder("当时写下的答案或解题过程").fill("x=4");
  await panel.getByPlaceholder("不确定时可以暂时留空").fill("x=2");
  await panel
    .getByPlaceholder("记录老师讲解或自己的订正思路")
    .fill("对称轴公式为 x=-b/(2a)，代入 a=2、b=-8 得 x=2。");
  if (kbId) {
    await panel.locator('label:has-text("关联资料范围") select').selectOption(kbId);
  }
  await panel.getByPlaceholder(/例如：极限/).fill("二次函数对称轴");
  await panel.getByText("我已核对正确答案", { exact: true }).click();
  await shot("33-error-entry-verified");
  await panel.getByRole("button", { name: "确认并加入复习" }).click();
  await page.getByText(stem, { exact: false }).waitFor({
    state: "visible",
    timeout: 30_000,
  });
  await shot("34-error-book-after-entry");

  await goto("/reviews", "35-review-queue", 2000);
  await page.getByText(stem, { exact: false }).waitFor({
    state: "visible",
    timeout: 30_000,
  });
  const answer = page.locator('input[placeholder="填写答案"]').first();
  await answer.fill("x=2");
  await shot("36-review-answered");
  await page.getByRole("button", { name: "提交答案" }).click();
  await page.getByText(/回答正确|回答错误/).first().waitFor({
    state: "visible",
    timeout: 60_000,
  });
  await shot("37-review-result");
  const finish = page.getByRole("button", { name: "完成今日复习" });
  if (await finish.isVisible().catch(() => false)) {
    await finish.click();
    await shot("38-review-completed");
  }

  await goto("/profile", "39-profile-after-review", 2500);
  await goto("/reports", "40-reports-after-review", 4500);
  await goto("/today", "41-today-after-review", 1800);
}

let balanceBefore = null;
let balanceAfter = null;
let fatalError = null;
try {
  await login();
  balanceBefore = await readBalance("02-billing-before");
  if (existingKbId) {
    kbId = existingKbId;
  } else {
    await createAndUploadKnowledgeBase();
    await testRagChat();
    await testTutor();
  }
  if (runManualLearningLoop) {
    await testManualErrorAndReview();
  } else {
    await testQuiz();
    await verifyLearningLoop();
  }
  balanceAfter = await readBalance("31-billing-after");
} catch (error) {
  fatalError = String(error?.stack ?? error);
  issue("fatal", fatalError);
  await shot("99-failure-state").catch(() => {});
} finally {
  await Promise.allSettled(sseCaptureTasks);
  const uniqueIssues = [
    ...new Map(issues.map((row) => [JSON.stringify(row), row])).values(),
  ];
  const report = {
    reviewedAt: new Date().toISOString(),
    baseUrl,
    runName,
    kbId,
    inputFile: basename(inputFile),
    balanceBefore,
    balanceAfter,
    balanceSpent:
      balanceBefore != null && balanceAfter != null
        ? Number((balanceBefore - balanceAfter).toFixed(4))
        : null,
    completed: !fatalError,
    fatalError,
    steps,
    issues: uniqueIssues,
    sseTranscripts,
  };
  writeFileSync(
    resolve(outdir, "full-flow-results.json"),
    `${JSON.stringify(report, null, 2)}\n`,
  );
  console.log(`RESULT ${resolve(outdir, "full-flow-results.json")}`);
  console.log(`COMPLETED ${report.completed}`);
  console.log(`ISSUES ${uniqueIssues.length}`);
  await browser.close();
}

if (fatalError) process.exitCode = 1;
