import { chromium } from "playwright";
import { mkdirSync } from "node:fs";

const [email, password, outdir] = process.argv.slice(2);
mkdirSync(outdir, { recursive: true });
const errors = {};
const browser = await chromium.launch({ args: ["--no-sandbox"] });
const page = await (await browser.newContext({
  viewport: { width: 1440, height: 900 },
})).newPage();
let current = "boot";
page.on("console", (m) => { if (m.type() === "error") (errors[current] ??= []).push(m.text().slice(0, 160)); });
page.on("pageerror", (e) => (errors[current] ??= []).push("pageerror: " + String(e).slice(0, 160)));

async function shot(name) {
  current = name;
  await page.waitForTimeout(1500);
  await page.screenshot({ path: `${outdir}/${name}.png`, fullPage: false });
  console.log("SHOT", name);
}

try {
  current = "login";
  await page.goto("http://localhost:3000/login", { waitUntil: "networkidle", timeout: 60000 });
  await page.fill('input[type="email"], input[name="email"]', email);
  await page.fill('input[type="password"], input[name="password"]', password);
  await page.click('button[type="submit"], button:has-text("登录")');
  await page.waitForTimeout(3000);

  for (const [path, name] of [
    ["/tutor", "10-tutor"],
    ["/errors", "11-errors"],
    ["/evals", "12-evals"],
    ["/teacher", "13-teacher"],
    ["/orgs", "14-orgs"],
    ["/reports", "15-reports"],
    ["/profile", "16-profile"],
    ["/plans", "17-plans"],
    ["/kb", "18-kb"],
    ["/quiz", "19-quiz"],
    ["/admin", "20-admin"],
  ]) {
    try {
      await page.goto(`http://localhost:3000${path}`, { waitUntil: "networkidle", timeout: 30000 });
      await shot(name);
    } catch (e) {
      console.log("NAV_FAIL", path, String(e).slice(0, 120));
    }
  }
} finally {
  console.log("CONSOLE_ERRORS", JSON.stringify(errors).slice(0, 2000));
  await browser.close();
}
