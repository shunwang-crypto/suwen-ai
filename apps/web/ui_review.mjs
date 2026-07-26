// UI 实测巡检: 真实浏览器走登录表单, SPA 内点击侧边栏导航逐页截图。
// 用法: node scripts/ui_review.mjs <email> <password> <outdir>
import { chromium } from "playwright";
import { mkdirSync } from "node:fs";

const [email, password, outdir] = process.argv.slice(2);
mkdirSync(outdir, { recursive: true });
const errors = {};

const browser = await chromium.launch({ args: ["--no-sandbox"] });
const page = await (await browser.newContext({
  viewport: { width: 1440, height: 900 },
  deviceScaleFactor: 1,
})).newPage();

let current = "boot";
page.on("console", (m) => {
  if (m.type() === "error") (errors[current] ??= []).push(m.text().slice(0, 200));
});
page.on("pageerror", (e) => (errors[current] ??= []).push("pageerror: " + String(e).slice(0, 200)));

async function shot(name) {
  current = name;
  await page.waitForTimeout(1200);
  await page.screenshot({ path: `${outdir}/${name}.png` });
  console.log("SHOT", name);
}

try {
  // 登录页
  current = "login";
  await page.goto("http://localhost:3000/login", { waitUntil: "networkidle", timeout: 60000 });
  await shot("01-login");

  // 真实表单登录
  await page.fill('input[type="email"], input[name="email"]', email);
  await page.fill('input[type="password"], input[name="password"]', password);
  await page.click('button[type="submit"], button:has-text("登录")');
  await page.waitForURL(/chat|\/$/, { timeout: 30000 }).catch(() => {});
  await shot("02-chat-initial");

  // 选择知识库 + 发送已录制问题 (replay 命中 -> 真实回答与引用)
  try {
    const select = page.locator("select").first();
    if (await select.count()) {
      const options = await select.locator("option").allTextContents();
      console.log("KB_OPTIONS", JSON.stringify(options));
      if (options.length > 1) await select.selectOption({ index: 1 });
    }
    const input = page.locator("textarea, input[placeholder*='问'], input[placeholder*='输入']").first();
    await input.fill("拉格朗日余项的形式是什么?");
    await page.keyboard.press("Enter");
    await page.waitForTimeout(9000); // 等流式完成
    await shot("03-chat-answer");
  } catch (e) {
    console.log("CHAT_INTERACT_SKIP", String(e).slice(0, 150));
    await shot("03-chat-answer");
  }

  // 侧边栏逐页 (SPA 导航保住内存 token)
  const navs = [
    ["知识库", "04-kb"],
    ["练习", "05-quiz"],
    ["学习计划", "06-plans"],
    ["复习", "07-reviews"],
    ["画像", "08-profile"],
    ["运营", "09-admin"],
  ];
  for (const [label, name] of navs) {
    try {
      await page.click(`a:has-text("${label}")`, { timeout: 8000 });
      await page.waitForTimeout(2500);
      await shot(name);
    } catch (e) {
      console.log("NAV_FAIL", label, String(e).slice(0, 120));
    }
  }
} finally {
  console.log("CONSOLE_ERRORS", JSON.stringify(errors, null, 1).slice(0, 3000));
  await browser.close();
}
