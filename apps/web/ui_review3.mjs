// 合流终检:落地页(双端) + 认证页 + 登录后全功能页截图,收集 console/page 错误
import { chromium } from "playwright";
import { mkdirSync } from "node:fs";

const [email, password, outdir] = process.argv.slice(2);
mkdirSync(outdir, { recursive: true });
const errors = {};
const browser = await chromium.launch({ args: ["--no-sandbox"] });

async function makePage(viewport) {
  const page = await (await browser.newContext({ viewport })).newPage();
  page.on("console", (m) => { if (m.type() === "error") (errors[page._cur ?? "?"] ??= []).push(m.text().slice(0, 200)); });
  page.on("pageerror", (e) => (errors[page._cur ?? "?"] ??= []).push("pageerror: " + String(e).slice(0, 200)));
  return page;
}

// 逐屏滚动到底再回顶,触发 IntersectionObserver 滚动入场后再全页截图
async function scrollThrough(page) {
  await page.evaluate(async () => {
    const step = window.innerHeight * 0.8;
    for (let y = 0; y < document.body.scrollHeight; y += step) {
      window.scrollTo(0, y);
      await new Promise((r) => setTimeout(r, 250));
    }
    window.scrollTo(0, 0);
  });
  await page.waitForTimeout(900);
}

async function shot(page, name, { full = false, settle = 1800 } = {}) {
  page._cur = name;
  await page.waitForTimeout(settle);
  await page.screenshot({ path: `${outdir}/${name}.png`, fullPage: full });
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  console.log("SHOT", name, overflow > 0 ? `!!!H-OVERFLOW ${overflow}px` : "");
}

try {
  // ── 未登录:落地页 桌面全页 + 移动全页 ──
  const desk = await makePage({ width: 1440, height: 900 });
  await desk.goto("http://localhost:3000/", { waitUntil: "networkidle", timeout: 60000 });
  await shot(desk, "00-landing-hero");
  await scrollThrough(desk);
  await shot(desk, "01-landing-full", { full: true, settle: 800 });

  const mob = await makePage({ width: 375, height: 812 });
  await mob.goto("http://localhost:3000/", { waitUntil: "networkidle", timeout: 60000 });
  await scrollThrough(mob);
  await shot(mob, "02-landing-mobile", { full: true });

  // 认证页
  await desk.goto("http://localhost:3000/login", { waitUntil: "networkidle" });
  await shot(desk, "03-login");
  await desk.goto("http://localhost:3000/register", { waitUntil: "networkidle" });
  await shot(desk, "04-register");

  // ── 登录 ──
  await desk.goto("http://localhost:3000/login", { waitUntil: "networkidle" });
  await desk.fill('input[type="email"]', email);
  await desk.fill('input[type="password"]', password);
  await desk.click('button:has-text("登录")');
  await desk.waitForURL(/\/(chat)?$/, { timeout: 20000 }).catch(() => {});
  await desk.waitForTimeout(2500);

  // chat:发一条问答看动效/时间线/引用
  await desk.goto("http://localhost:3000/chat", { waitUntil: "networkidle" });
  await shot(desk, "05-chat-empty");
  const input = desk.locator("textarea").first();
  await input.fill("什么是导数?请解释链式法则");
  await desk.keyboard.press("Enter");
  await desk.waitForTimeout(4000);
  await shot(desk, "06-chat-streaming", { settle: 0 });
  await desk.waitForTimeout(9000);
  await shot(desk, "07-chat-answer");

  for (const [path, name] of [
    ["/profile", "08-profile"],
    ["/reports", "09-reports"],
    ["/reviews", "10-reviews"],
    ["/errors", "11-errors"],
    ["/quiz", "12-quiz"],
    ["/plans", "13-plans"],
    ["/kb", "14-kb"],
    ["/tutor", "15-tutor"],
  ]) {
    await desk.goto(`http://localhost:3000${path}`, { waitUntil: "networkidle", timeout: 45000 });
    await shot(desk, name);
  }

  // 移动端抽查两页
  await mob.goto("http://localhost:3000/login", { waitUntil: "networkidle" });
  await shot(mob, "16-login-mobile");
} finally {
  await browser.close();
  console.log("ERRORS:", JSON.stringify(errors, null, 1));
}
