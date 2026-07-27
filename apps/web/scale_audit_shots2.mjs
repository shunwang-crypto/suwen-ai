// 补拍: 计划详情日历 + profile 掌握度明细滚动盒中段
import { chromium } from "playwright";

const [email, password, outdir] = process.argv.slice(2);
const browser = await chromium.launch({ args: ["--no-sandbox"] });
const page = await (await browser.newContext({ viewport: { width: 1440, height: 900 } })).newPage();

await page.goto("http://localhost:3000/login", { waitUntil: "networkidle" });
await page.fill('input[type="email"]', email);
await page.fill('input[type="password"]', password);
await page.click('button:has-text("登录")');
await page.waitForTimeout(3000);

// 1) 计划详情 (点击"我的计划"里的第一项)
await page.goto("http://localhost:3000/plans", { waitUntil: "networkidle" });
await page.waitForTimeout(1500);
await page.click("text=期末三科总复习");
await page.waitForTimeout(2500);
await page.evaluate(() => {
  const main = document.querySelector("main");
  let el = main;
  while (el && el !== document.body) {
    el.style.overflow = "visible"; el.style.height = "auto"; el.style.maxHeight = "none"; el.style.minHeight = "0";
    el = el.parentElement;
  }
  document.body.style.height = "auto"; document.documentElement.style.height = "auto";
  const aside = document.querySelector("aside");
  if (aside) { aside.style.position = "fixed"; aside.style.height = "100vh"; }
});
await page.waitForTimeout(500);
await page.screenshot({ path: `${outdir}/11-plans-detail-desktop.png`, fullPage: true });
console.log("SHOT 11-plans-detail-desktop");

// 2) profile 掌握度明细滚动盒: 滚到中段截视口 (展示 152 行挤在 max-h-96 里)
await page.goto("http://localhost:3000/profile", { waitUntil: "networkidle" });
await page.waitForTimeout(2500);
const box = page.locator("ul.max-h-96").first();
await box.evaluate((el) => { el.scrollTop = el.scrollHeight * 0.45; });
await page.waitForTimeout(600);
const info = await box.evaluate((el) => ({ items: el.children.length, boxH: el.clientHeight, contentH: el.scrollHeight }));
console.log("mastery scrollbox:", JSON.stringify(info));
await page.screenshot({ path: `${outdir}/12-profile-mastery-scrollbox.png` });
console.log("SHOT 12-profile-mastery-scrollbox");

await browser.close();
