// renders every asset in tools/assets.html to assets/, and rasterises the logo svgs.
// usage: node tools/render.js   (needs playwright with chromium available)
const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "..");
const OUT = {
  "github-social": "banners/github-social-1280x640.png",
  og: "banners/og-1200x630.png",
  "x-header": "banners/x-header-1500x500.png",
  linkedin: "banners/linkedin-1584x396.png",
  "post-claim": "posts/claim-1080x1080.png",
  "post-wonders": "posts/wonders-1080x1080.png",
  "post-definition": "posts/definition-1080x1350.png",
  story: "posts/story-1080x1920.png",
  thumbnail: "thumbnails/thumbnail-1280x720.png",
  "readme-hero": "readme/hero.png",
  avatar: "avatars/avatar-400.png",
  "avatar-ascii": "avatars/avatar-ascii-400.png",
};

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ deviceScaleFactor: 1 });
  await page.goto("file://" + path.join(ROOT, "tools", "assets.html"));
  for (const [name, rel] of Object.entries(OUT)) {
    const [w, h] = await page.evaluate((n) => window.renderAsset(n), name);
    await page.setViewportSize({ width: w, height: h });
    const file = path.join(ROOT, "assets", rel);
    fs.mkdirSync(path.dirname(file), { recursive: true });
    await page.screenshot({ path: file, clip: { x: 0, y: 0, width: w, height: h } });
    console.log(rel);
  }
  // logos: svg to png, at least 1024 px on the long side
  const logos = path.join(ROOT, "assets", "logos");
  for (const way of fs.readdirSync(logos)) {
    for (const f of fs.readdirSync(path.join(logos, way)).filter((x) => x.endsWith(".svg"))) {
      const svg = fs.readFileSync(path.join(logos, way, f), "utf8");
      const [, w, h] = svg.match(/width="(\d+)" height="(\d+)"/).map(Number);
      const scale = Math.max(1, Math.ceil(1024 / Math.max(w, h)));
      await page.setViewportSize({ width: w * scale, height: h * scale });
      await page.setContent(`<body style="margin:0;background:transparent">${svg.replace(/width="\d+" height="\d+"/, `width="${w * scale}" height="${h * scale}"`)}</body>`);
      await page.screenshot({ path: path.join(logos, way, f.replace(".svg", ".png")), omitBackground: true, clip: { x: 0, y: 0, width: w * scale, height: h * scale } });
    }
  }
  await browser.close();
})();
