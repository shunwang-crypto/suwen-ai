// 真实规模 UX 审计截图: 桌面 1440 全页 8 页 + 手机 390 抽查 profile/reports
import { chromium } from "playwright";
import { mkdirSync } from "node:fs";

const [email, password, outdir] = process.argv.slice(2);
mkdirSync(outdir, { recursive: true });
const errors = {};
const browser = await chromium.launch({ args: ["--no-sandbox"] });

async function makePage(viewport) {
  const ctx = await browser.newContext({ viewport });
  const page = await ctx.newPage();
  page.on("console", (m) => { if (m.type() === "error") (errors[page._cur ?? "?"] ??= []).push(m.text().slice(0, 200)); });
  page.on("pageerror", (e) => (errors[page._cur ?? "?"] ??= []).push("pageerror: " + String(e).slice(0, 200)));
  return page;
}

async function login(page) {
  await page.goto("http://localhost:3000/login", { waitUntil: "networkidle" });
  await page.fill('input[type="email"]', email);
  await page.fill('input[type="password"]', password);
  await page.click('button:has-text("登录")');
  await page.waitForTimeout(3000);
}

// app-shell 的 main 是内部滚动容器 (min-h-0 flex-1 overflow-y-auto):
// 先滚它到底触发懒加载, 再把它和祖先全部展开成自然高度, body 才能 fullPage。
async function scrollThrough(page) {
  await page.evaluate(async () => {
    const el = document.querySelector("main") ?? document.scrollingElement;
    const step = window.innerHeight * 0.8;
    for (let y = 0; y < el.scrollHeight; y += step) {
      el.scrollTo(0, y);
      await new Promise((r) => setTimeout(r, 180));
    }
    el.scrollTo(0, 0);
  });
  await page.waitForTimeout(600);
}

async function expandScrollContainers(page) {
  await page.evaluate(() => {
    const main = document.querySelector("main");
    if (!main) return;
    let el = main;
    while (el && el !== document.body) {
      el.style.overflow = "visible";
      el.style.height = "auto";
      el.style.maxHeight = "none";
      el.style.minHeight = "0";
      el = el.parentElement;
    }
    document.body.style.height = "auto";
    document.documentElement.style.height = "auto";
    // 左侧 sticky 侧栏在展开后会拉长, 固定它避免整页重复
    const aside = document.querySelector("aside");
    if (aside) { aside.style.position = "fixed"; aside.style.height = "100vh"; }
  });
  await page.waitForTimeout(400);
}

async function shot(page, name, { full = true, settle = 2200 } = {}) {
  page._cur = name;
  await page.waitForTimeout(settle);
  if (full) {
    await scrollThrough(page);
    await expandScrollContainers(page);
  }
  await page.screenshot({ path: `${outdir}/${name}.png`, fullPage: full });
  const h = await page.evaluate(() => document.body.scrollHeight);
  console.log("SHOT", name, `pageHeight=${h}`);
}

try {
  const desk = await makePage({ width: 1440, height: 900 });
  await login(desk);

  for (const [path, name] of [
    ["/profile", "01-profile-desktop"],
    ["/reports", "02-reports-desktop"],
    ["/reviews", "03-reviews-desktop"],
    ["/errors", "04-errors-desktop"],
    ["/quiz", "05-quiz-desktop"],
    ["/plans", "06-plans-desktop"],
    ["/chat", "07-chat-firstscreen-desktop"],
    ["/settings", "08-settings-desktop"],
  ]) {
    await desk.goto(`http://localhost:3000${path}`, { waitUntil: "networkidle", timeout: 60000 });
    await shot(desk, name, { full: path !== "/chat" });
  }

  const mob = await makePage({ width: 390, height: 844 });
  await login(mob);
  for (const [path, name] of [
    ["/profile", "09-profile-mobile"],
    ["/reports", "10-reports-mobile"],
  ]) {
    await mob.goto(`http://localhost:3000${path}`, { waitUntil: "networkidle", timeout: 60000 });
    await shot(mob, name);
  }
} finally {
  await browser.close();
  console.log("ERRORS:", JSON.stringify(errors, null, 1));
}
