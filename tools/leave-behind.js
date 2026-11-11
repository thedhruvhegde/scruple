// renders pilot/leave-behind.html to pilot/scruple-pilot.pdf, one A4 page.
// usage: node tools/leave-behind.js   (needs playwright with chromium; NODE_PATH=/opt/node22/lib/node_modules if global)
// served over http from a temporary server, so ../site.css and the web fonts load as on the site.
const { chromium } = require("playwright");
const http = require("http");
const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "..");
const TYPES = { ".html": "text/html", ".css": "text/css", ".svg": "image/svg+xml", ".png": "image/png", ".js": "text/javascript" };

const server = http.createServer((req, res) => {
  const file = path.join(ROOT, decodeURIComponent(new URL(req.url, "http://x").pathname));
  if (!file.startsWith(ROOT) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) { res.writeHead(404); res.end(); return; }
  res.writeHead(200, { "content-type": TYPES[path.extname(file)] || "application/octet-stream" });
  fs.createReadStream(file).pipe(res);
});

server.listen(0, "127.0.0.1", async () => {
  const browser = await chromium.launch({ args: ["--disable-dev-shm-usage"] });
  const page = await browser.newPage();
  await page.goto(`http://127.0.0.1:${server.address().port}/pilot/leave-behind.html`, { waitUntil: "networkidle" });
  await page.evaluate(() => document.fonts.ready);
  const fonts = await page.evaluate(() => [...document.fonts].filter((f) => f.status === "loaded").map((f) => f.family));
  if (!fonts.some((f) => f.includes("Martian"))) console.warn("warning: Martian Mono did not load; the pdf uses fallback fonts");
  const out = path.join(ROOT, "pilot", "scruple-pilot.pdf");
  await page.pdf({ path: out, format: "A4", printBackground: true, margin: { top: 0, right: 0, bottom: 0, left: 0 }, pageRanges: "1" });
  // one page only: the sheet clips, so check nothing inside it was cut off
  await page.emulateMedia({ media: "print" });
  const spill = await page.evaluate(() => { const s = document.querySelector(".sheet"); return s.scrollHeight - s.clientHeight; });
  console.log(`wrote ${path.relative(ROOT, out)}${spill > 1 ? ` (warning: ${spill}px taller than the page, cut off)` : ""}`);
  await browser.close();
  server.close();
});
