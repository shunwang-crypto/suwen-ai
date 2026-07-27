// Langfuse 看板截图 (M3-D1 证据): 登录 → traces 列表 → 首条 trace 详情
import { chromium } from "playwright";
import { mkdirSync } from "node:fs";

const [email, password, outdir] = process.argv.slice(2);
mkdirSync(outdir, { recursive: true });
const browser = await chromium.launch({ args: ["--no-sandbox"] });
const page = await (await browser.newContext({ viewport: { width: 1440, height: 900 } })).newPage();

try {
  await page.goto("http://127.0.0.1:13000/auth/sign-in", { waitUntil: "networkidle", timeout: 60000 });
  await page.fill('input[name="email"]', email);
  await page.fill('input[type="password"]', password);
  await page.click('button[type="submit"]');
  await page.waitForTimeout(4000);
  await page.screenshot({ path: `${outdir}/lf-1-home.png` });

  await page.goto("http://127.0.0.1:13000/project/eduagents-dev/traces", { waitUntil: "networkidle", timeout: 60000 });
  await page.waitForTimeout(2500);
  await page.screenshot({ path: `${outdir}/lf-2-traces.png` });

  // 点开首条 trace
  const row = page.locator("tbody tr").first();
  if (await row.count()) {
    await row.click();
    await page.waitForTimeout(9000);
    await page.screenshot({ path: `${outdir}/lf-3-trace-detail.png` });
  }
  console.log("DONE");
} finally {
  await browser.close();
}
