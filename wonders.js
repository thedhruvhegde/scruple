/* wonders.js: the new seven wonders of the world, drawn in characters.
 *
 * each wonder is painted as vector shapes onto a hidden canvas. every shape carries a material
 * (stone, opening, water, foliage, rock, ground, sky, sun, gold) and a brightness, so lit faces,
 * shadow faces and cast shadows read as depth. the canvas is read back one character cell at a
 * time: brightness picks a glyph, edges inside a cell become line glyphs, and material plus
 * brightness pick the colour. a step function moves what should move: water ripples and
 * reflects, clouds and birds drift, foliage sways, stars and city lights twinkle, stone shimmers.
 *
 * api: Wonders.list, Wonders.palette, Wonders.make(index, cols, rows, cellW, cellH), Wonders.step(grid, seconds)
 * a grid exposes code (Uint16Array) and col (Uint8Array), one entry per cell, row-major.
 */
(function () {
  "use strict";
  // 0 bone 1 dim 2 pine 3 brass 4 vermilion 5 sky 6 blue 7 rose 8 stone 9 green
  // 10 stone-shade 11 rose-shade 12 brass-shade 13 deep 14 mist 15 dusk
  const PALETTE = ["#E6E3D6", "#55617D", "#6CC39B", "#E8C46A", "#F27B55", "#93B3EA", "#4F6FB4", "#E3967A",
    "#B9B09A", "#3F9A6E", "#857D6A", "#B0634C", "#AD8838", "#34497A", "#7486AD", "#C9779F"];
  const M = { SKY: 0, ST: 1, DARK: 2, WATER: 3, LEAF: 4, GROUND: 5, ROCK: 6, SUN: 7, GOLD: 8 };
  const RAMP = " .:-=+*#%@";

  function hash(a, b) { let h = Math.imul(a ^ 0x9e3779b9, 0x85ebca6b) ^ Math.imul(b + 0x632be5ab, 0xc2b2ae35); h ^= h >>> 15; h = Math.imul(h, 0x2c1b3c6d); h ^= h >>> 12; return (h >>> 0) / 4294967296; }
  function vnoise(x, y) {
    const xi = Math.floor(x), yi = Math.floor(y), xf = x - xi, yf = y - yi, u = xf * xf * (3 - 2 * xf), v = yf * yf * (3 - 2 * yf);
    const a = hash(xi, yi), b = hash(xi + 1, yi), c = hash(xi, yi + 1), d = hash(xi + 1, yi + 1);
    return a + (b - a) * u + (c - a) * v + (a - b - c + d) * u * v;
  }
  const fbm = (x, y) => 0.55 * vnoise(x, y) + 0.3 * vnoise(x * 2.1, y * 2.1) + 0.15 * vnoise(x * 4.3, y * 4.3);

  /* ---------- drawing kit, in design units: 440 tall, x = 0 at the centre ---------- */
  function kit(g) {
    const style = (mat, lum) => `rgb(${Math.max(0, Math.min(255, lum | 0))},${mat * 20 + 10},0)`;
    const f = (mat, lum) => { g.fillStyle = style(mat, lum); g.strokeStyle = g.fillStyle; };
    const rect = (x, y, w, h, mat, lum) => { f(mat, lum); g.fillRect(x, y, w, h); };
    const poly = (pts, mat, lum) => { f(mat, lum); g.beginPath(); g.moveTo(pts[0], pts[1]); for (let i = 2; i < pts.length; i += 2) g.lineTo(pts[i], pts[i + 1]); g.closePath(); g.fill(); };
    const line = (x1, y1, x2, y2, w, mat, lum) => { f(mat, lum); g.lineWidth = w; g.beginPath(); g.moveTo(x1, y1); g.lineTo(x2, y2); g.stroke(); };
    const circle = (x, y, r, mat, lum) => { f(mat, lum); g.beginPath(); g.arc(x, y, r, 0, Math.PI * 2); g.fill(); };
    const ellipse = (x, y, rx, ry, mat, lum) => { f(mat, lum); g.beginPath(); g.ellipse(x, y, rx, ry, 0, 0, Math.PI * 2); g.fill(); };
    const arch = (x, y, w, h, mat, lum) => { f(mat, lum); g.beginPath(); g.moveTo(x, y + h); g.lineTo(x, y + w / 2); g.arc(x + w / 2, y + w / 2, w / 2, Math.PI, 0); g.lineTo(x + w, y + h); g.closePath(); g.fill(); };
    const pointed = (x, y, w, h, mat, lum) => { f(mat, lum); g.beginPath(); g.moveTo(x, y + h); g.lineTo(x, y + w * 0.55); g.quadraticCurveTo(x, y, x + w / 2, y - w * 0.1); g.quadraticCurveTo(x + w, y, x + w, y + w * 0.55); g.lineTo(x + w, y + h); g.closePath(); g.fill(); };
    const onion = (cx, base, w, h, mat, lum, lit) => {
      f(mat, lum); g.beginPath(); g.moveTo(cx - w * 0.42, base);
      g.bezierCurveTo(cx - w * 0.62, base - h * 0.45, cx - w * 0.5, base - h * 0.78, cx, base - h);
      g.bezierCurveTo(cx + w * 0.5, base - h * 0.78, cx + w * 0.62, base - h * 0.45, cx + w * 0.42, base); g.closePath(); g.fill();
      if (lit) { // a lit crescent on one side gives the dome volume
        f(mat, Math.min(255, lum + 30)); g.beginPath(); g.moveTo(cx - w * 0.42, base);
        g.bezierCurveTo(cx - w * 0.62, base - h * 0.45, cx - w * 0.5, base - h * 0.78, cx, base - h);
        g.bezierCurveTo(cx - w * 0.2, base - h * 0.7, cx - w * 0.3, base - h * 0.4, cx - w * 0.12, base); g.closePath(); g.fill();
      }
    };
    const ridge = (pts, bottom, mat, lum) => { f(mat, lum); g.beginPath(); g.moveTo(pts[0], bottom); for (let i = 0; i < pts.length; i += 2) g.lineTo(pts[i], pts[i + 1]); g.lineTo(pts[pts.length - 2], bottom); g.closePath(); g.fill(); };
    const sky = (top, low, horizon) => { const gr = g.createLinearGradient(0, 0, 0, horizon); gr.addColorStop(0, style(M.SKY, top)); gr.addColorStop(1, style(M.SKY, low)); g.fillStyle = gr; g.fillRect(-2000, 0, 4000, horizon); };
    const glow = (x, y, r, lum, base) => { const gr = g.createRadialGradient(x, y, 0, x, y, r); gr.addColorStop(0, style(M.SKY, lum)); gr.addColorStop(1, style(M.SKY, base)); g.fillStyle = gr; g.beginPath(); g.arc(x, y, r, 0, Math.PI * 2); g.fill(); };
    const pine = (x, y, s, lum) => { line(x, y, x, y - 18 * s, 2 * s, M.LEAF, lum - 50); ellipse(x, y - 20 * s, 16 * s, 5 * s, M.LEAF, lum); ellipse(x - 3 * s, y - 23 * s, 11 * s, 4 * s, M.LEAF, lum + 25); };
    const cypress = (x, y, h, w, lum) => { poly([x - w, y, x, y - h, x + w, y], M.LEAF, lum); poly([x - w, y, x, y - h, x - w * 0.1, y], M.LEAF, lum + 30); };
    return { f, rect, poly, line, circle, ellipse, arch, pointed, onion, ridge, sky, glow, pine, cypress };
  }
  const span = (W) => W / 2 + 20;

  /* ---------- the seven ---------- */
  const LIST = [
    {
      name: "taj mahal", place: "agra, india",
      colors: { st: 0, stHi: 0, stShade: 14, dark: 13, rock: 11, rockFar: 13, ground: 9, leaf: 9, leafHi: 2, water: 5, sun: 3, gold: 3, skyTop: 13, skyMid: 6, skyLow: 15 },
      horizon: 324, clouds: { y0: 30, y1: 130, d: 0.6, speed: 0.5 }, stars: 0.02, birds: 3,
      draw(k, W) {
        const S = span(W);
        k.sky(14, 120, 324); k.glow(0, 330, 360, 150, 60);
        k.circle(250, 70, 18, M.SUN, 240);
        for (const s of [-1, 1]) {   // flanking red sandstone mosque and guest house
          const cx = s * 410;
          k.rect(cx - 70, 248, 140, 56, M.ROCK, 150); k.rect(cx - 70, 248, 140, 6, M.ROCK, 200);
          for (let i = -2; i <= 2; i++) k.pointed(cx + i * 26 - 8, 262, 16, 40, M.DARK, 40);
          for (const d of [-40, 0, 40]) k.onion(cx + d, 250, d ? 26 : 40, d ? 24 : 36, M.ST, 215, true);
        }
        k.ridge([-S, 304, -300, 300, -200, 304, 200, 304, 300, 300, S, 304], 330, M.LEAF, 70);
        k.rect(-270, 302, 540, 22, M.ST, 175); k.line(-270, 302, 270, 302, 1.5, M.ST, 240);
        k.poly([-270, 324, 270, 324, 300, 330, -300, 330], M.ST, 110);
        for (const s of [-1, 1]) {   // four minarets, the outer pair nearer
          for (const [x, top, w] of [[246, 128, 13], [196, 150, 10]]) {
            k.rect(s * x - w / 2, top, w, 302 - top, M.ST, x > 200 ? 210 : 170);
            k.rect(s * x + (s > 0 ? w / 2 - 3 : -w / 2), top, 3, 302 - top, M.ST, 140);
            for (const b of [0.28, 0.55, 0.8]) k.rect(s * x - w / 2 - 3, top + (302 - top) * b, w + 6, 3, M.ST, 245);
            k.onion(s * x, top + 2, w * 1.5, 16, M.ST, 220, true); k.line(s * x, top - 14, s * x, top - 24, 1.2, M.GOLD, 220);
          }
        }
        k.rect(-132, 200, 264, 102, M.ST, 205);
        for (const s of [-1, 1]) k.poly([s * 132, 200, s * 152, 214, s * 152, 302, s * 132, 302], M.ST, s > 0 ? 130 : 165);
        k.pointed(-34, 214, 68, 88, M.DARK, 35);
        k.pointed(-26, 222, 52, 80, M.DARK, 55);
        k.rect(-44, 208, 88, 3, M.ST, 250);
        for (const s of [-1, 1]) for (const [x, y] of [[84, 214], [84, 260], [116, 214], [116, 260]]) k.pointed(s * x - 11, y, 22, 34, M.DARK, 50);
        k.rect(-60, 182, 120, 20, M.ST, 190);
        k.onion(0, 186, 150, 132, M.ST, 215, true);
        k.line(0, 56, 0, 30, 2, M.GOLD, 240); k.circle(0, 46, 3, M.GOLD, 250);
        for (const s of [-1, 1]) { k.rect(s * 104 - 14, 172, 28, 30, M.ST, 190); k.onion(s * 104, 174, 34, 30, M.ST, 215, true); k.line(s * 104, 146, s * 104, 136, 1.2, M.GOLD, 220); }
        k.rect(-S, 330, 2 * S, 110, M.GROUND, 60);
        for (let i = 0; i < 6; i++) k.line(-S, 336 + i * 18, S, 336 + i * 18, 1, M.GROUND, 95);
        k.poly([-48, 326, 48, 326, 150, 440, -150, 440], M.WATER, 80);
        k.poly([-48, 326, -52, 326, -158, 440, -150, 440], M.ST, 200); k.poly([48, 326, 52, 326, 158, 440, 150, 440], M.ST, 200);
        for (const s of [-1, 1]) for (let i = 0; i < 7; i++) k.cypress(s * (64 + i * 20), 342 + i * 16, 16 + i * 8, 4 + i * 1.2, 110 + i * 10);
      },
    },
    {
      name: "colosseum", place: "rome, italy",
      colors: { st: 3, stHi: 0, stShade: 12, dark: 1, rock: 8, rockFar: 14, ground: 8, leaf: 9, leafHi: 2, water: 5, sun: 3, gold: 3, skyTop: 6, skyMid: 5, skyLow: 3 },
      horizon: 330, clouds: { y0: 20, y1: 140, d: 0.52, speed: 0.9 }, stars: 0, birds: 5,
      draw(k, W) {
        const S = span(W);
        k.sky(40, 150, 330); k.glow(-300, 80, 160, 150, 70);
        k.circle(-300, 80, 22, M.SUN, 250);
        k.ridge([-S, 300, -380, 292, -250, 298, 300, 296, S, 300], 335, M.ROCK, 70);
        for (let x = -S; x < S; x += 23) k.rect(x, 288 - hash(x | 0, 2) * 18, 16, 50, M.ROCK, 80 + hash(x | 0, 3) * 30);
        k.rect(-S, 330, 2 * S, 110, M.GROUND, 120);
        for (let i = -12; i <= 12; i++) k.line(i * 26, 330, i * 90, 440, 1, M.GROUND, 80);
        for (let i = 0; i < 6; i++) k.line(-S, 340 + i * i * 3, S, 340 + i * i * 3, 1, M.GROUND, 85);
        const top = (x) => (x < 110 ? 108 + Math.pow(x / 340, 2) * 30 : 170 + (x - 110) * 0.1 + Math.sin(x * 0.09) * 10);
        const pts = []; for (let x = -340; x <= 340; x += 10) pts.push(x, top(x));
        k.ridge([140, 150, 200, 158, 260, 170, 330, 190], 330, M.ST, 120);
        for (let x = 150; x < 330; x += 22) k.arch(x, 200, 12, 30, M.DARK, 40);
        k.ridge(pts, 330, M.ST, 190);
        k.poly([-340, 330, 340, 330, 390, 345, -300, 345], M.GROUND, 60);
        const tiers = [[262, 330], [212, 262], [162, 212]];
        for (let i = -16; i <= 16; i++) {
          const a = i / 16 * 1.25, x = Math.sin(a) * 330, w = Math.cos(a) * 26, lit = 205 - i * 3.5;
          if (w < 5) continue;
          k.rect(x - w * 0.5, Math.max(162, top(x)), w, 330 - Math.max(162, top(x)), M.ST, lit);
          tiers.forEach(([y0, y1], ti) => { if (top(x) < y0 - 4) { k.arch(x - w * 0.36, y0 + 8, w * 0.72, y1 - y0 - 8, M.DARK, 30 + ti * 6); k.arch(x - w * 0.24, y0 + 12, w * 0.48, y1 - y0 - 12, M.DARK, 18); } });
          if (top(x) < 150) k.rect(x - w * 0.18, 124, w * 0.36, 12, M.DARK, 50);
          k.line(x - w * 0.5, 162, x - w * 0.5, 330, 1, M.ST, 245);
        }
        for (const y of [162, 212, 262]) for (let x = -340; x < 340; x += 6) if (top(x) < y - 2) k.line(x, y, x + 6, y, 2.6, M.ST, 248);
        for (const [x, s] of [[-430, 1.2], [-380, 0.9], [400, 1.3], [450, 1]]) k.pine(x, 336, s * 1.6, 140);
      },
    },
    {
      name: "great wall", place: "china",
      colors: { st: 8, stHi: 0, stShade: 10, dark: 1, rock: 9, rockFar: 14, ground: 9, leaf: 9, leafHi: 2, water: 5, sun: 3, gold: 3, skyTop: 13, skyMid: 14, skyLow: 3 },
      horizon: 260, clouds: { y0: 150, y1: 280, d: 0.5, speed: 0.45 }, stars: 0, birds: 4,
      draw(k, W) {
        const S = span(W);
        k.sky(30, 160, 260); k.glow(240, 90, 120, 150, 80);
        k.circle(240, 90, 16, M.SUN, 245);
        k.ridge([-S, 175, -380, 150, -260, 190, -120, 140, 40, 185, 170, 130, 320, 170, S, 150], 440, M.ROCK, 55);
        for (let x = -S; x < S; x += 4) { const y = 150 + 22 * Math.sin((x + 300) / 110) + 6; k.rect(x, y, 4, 3, M.ST, 150); }
        k.ridge([-S, 240, -340, 215, -210, 262, -60, 205, 90, 250, 230, 195, 380, 238, S, 222], 440, M.ROCK, 70);
        k.ridge([-S, 320, -300, 292, -150, 330, 0, 282, 160, 322, 300, 278, S, 312], 440, M.LEAF, 90);
        for (let i = 0; i < 40; i++) k.line(-S + i * 30, 300 + hash(i, 1) * 20, -S + i * 30 + 40, 440, 1.2, M.LEAF, 70);
        const path = []; for (let x = -S; x <= S; x += 6) path.push([x, 296 - 26 * Math.sin((x + 120) / 150) - 14 * Math.sin(x / 47) + Math.abs(x) * 0.04]);
        for (let i = 0; i < path.length - 1; i++) {
          const [x1, y1] = path[i], [x2, y2] = path[i + 1];
          k.poly([x1, y1, x2, y2, x2, y2 + 24, x1, y1 + 24], M.ST, 236);
          k.poly([x1, y1 + 24, x2, y2 + 24, x2, y2 + 34, x1, y1 + 34], M.ST, 150);
          k.line(x1, y1, x2, y2, 2.5, M.ST, 252);
          if (i % 2 === 0) k.rect(x1, y1 - 8, 4, 8, M.ST, 235);
        }
        for (const i of [10, 42, 76, 110, 140]) {
          if (!path[i]) continue; const [x, y] = path[i];
          k.rect(x - 24, y - 52, 48, 70, M.ST, 240); k.rect(x + 12, y - 52, 12, 70, M.ST, 165);
          k.rect(x - 26, y - 52, 52, 4, M.ST, 252);
          k.arch(x - 15, y - 40, 9, 18, M.DARK, 30); k.arch(x + 2, y - 40, 9, 18, M.DARK, 30);
          for (let j = 0; j < 6; j++) k.rect(x - 26 + j * 9.5, y - 62, 6, 10, M.ST, 230);
        }
        for (let i = 0; i < 90; i++) { const x = -S + hash(i, 9) * 2 * S, y = 350 + hash(i, 10) * 90; k.cypress(x, y, 12 + hash(i, 12) * 10, 5, 130 + hash(i, 11) * 60); }
      },
    },
    {
      name: "petra", place: "ma'an, jordan",
      colors: { st: 7, stHi: 3, stShade: 11, dark: 1, rock: 11, rockFar: 7, ground: 3, leaf: 9, leafHi: 2, water: 5, sun: 3, gold: 3, skyTop: 6, skyMid: 5, skyLow: 3 },
      horizon: 410, clouds: { y0: 0, y1: 34, d: 0.55, speed: 0.5 }, stars: 0, birds: 2,
      draw(k, W) {
        const S = span(W);
        k.rect(-S, 0, 2 * S, 440, M.ROCK, 75);
        for (let i = 0; i < 40; i++) {
          const y = i * 11, wob = Math.sin(i * 1.7) * 8;
          k.line(-S, y + wob, -165, y + 6, 1.6, M.ROCK, 50 + (i % 4) * 30);
          k.line(165, y + 4, S, y + wob, 1.6, M.ROCK, 50 + (i % 4) * 30);
        }
        k.poly([-S, 0, -230, 0, -175, 440, -S, 440], M.ROCK, 60);
        k.poly([S, 0, 230, 0, 175, 440, S, 440], M.ROCK, 95);
        k.poly([-150, 0, 150, 0, 96, 34, -96, 34], M.SKY, 140);
        k.rect(-S, 410, 2 * S, 30, M.GROUND, 160);
        for (let i = 0; i < 8; i++) k.line(-S, 414 + i * 3.5, S, 414 + i * 3.5, 0.8, M.GROUND, 120);
        k.rect(-160, 40, 320, 372, M.ST, 100);
        k.rect(-152, 236, 304, 14, M.ST, 235); k.rect(-152, 400, 304, 12, M.ST, 210);
        for (const x of [-140, -100, -40, 40, 100, 140]) {
          k.rect(x - 8, 250, 16, 150, M.ST, 240); k.rect(x + 3, 250, 5, 150, M.ST, 165);
          k.rect(x - 11, 250, 22, 8, M.GOLD, 235); k.rect(x - 10, 392, 20, 8, M.ST, 225);
        }
        k.poly([-112, 236, 0, 190, 112, 236], M.ST, 245); k.poly([-96, 232, 0, 198, 96, 232], M.ST, 110);
        k.rect(-26, 300, 52, 100, M.DARK, 25); k.rect(-34, 294, 68, 6, M.ST, 230);
        for (const s of [-1, 1]) k.rect(s * 120 - 12, 320, 24, 70, M.DARK, 40);
        k.rect(-152, 176, 304, 12, M.ST, 235);
        k.rect(-46, 82, 92, 94, M.ST, 110);
        for (const x of [-38, -14, 14, 38]) { k.rect(x - 5, 86, 10, 90, M.ST, 245); k.rect(x + 2, 86, 3, 90, M.ST, 170); }
        k.poly([-50, 84, 0, 54, 50, 84], M.ST, 230); k.rect(-6, 34, 12, 22, M.GOLD, 235); k.circle(0, 34, 8, M.GOLD, 245);
        for (const s of [-1, 1]) {
          k.rect(s * 105 - 40, 100, 80, 76, M.ST, 105);
          for (const x of [80, 130]) k.rect(s * x - 5, 104, 10, 72, M.ST, 245);
          k.poly([s * 65, 100, s * 145, 100, s * 145, 82], M.ST, 220);
        }
        const cx = 250, cy = 410;   // a resting camel in the foreground
        k.ellipse(cx, cy - 22, 34, 16, M.ROCK, 150); k.ellipse(cx - 6, cy - 36, 12, 9, M.ROCK, 165);
        k.poly([cx + 26, cy - 26, cx + 46, cy - 52, cx + 54, cy - 52, cx + 36, cy - 20], M.ROCK, 150);
        k.ellipse(cx + 56, cy - 54, 9, 5, M.ROCK, 160);
        for (const dx of [-22, -8, 12, 24]) k.line(cx + dx, cy - 10, cx + dx + 2, cy, 3, M.ROCK, 140);
      },
    },
    {
      name: "christ the redeemer", place: "rio de janeiro, brazil",
      colors: { st: 0, stHi: 0, stShade: 14, dark: 1, rock: 9, rockFar: 13, ground: 9, leaf: 9, leafHi: 2, water: 6, sun: 3, gold: 3, skyTop: 13, skyMid: 15, skyLow: 4 },
      horizon: 360, clouds: { y0: 250, y1: 340, d: 0.48, speed: 0.55 }, stars: 0.012, birds: 3,
      draw(k, W) {
        const S = span(W);
        k.sky(16, 170, 360); k.glow(-260, 300, 220, 175, 90);
        k.circle(-260, 300, 26, M.SUN, 252);
        k.rect(-S, 360, 2 * S, 80, M.WATER, 70);
        k.ridge([-S, 340, -380, 318, -300, 330, -200, 352], 360, M.ROCK, 70);
        k.ridge([170, 360, 230, 296, 280, 280, 320, 294, 370, 360], 380, M.ROCK, 95);
        k.ridge([380, 360, 420, 330, 470, 340, S, 350], 380, M.ROCK, 75);
        k.ridge([-S, 390, -210, 330, -70, 250, -20, 222, 20, 222, 80, 270, 200, 350, S, 398], 440, M.ROCK, 130);
        k.poly([20, 222, 80, 270, 200, 350, S, 398, S, 440, 0, 440], M.ROCK, 90);
        for (let i = 0; i < 30; i++) k.line(-80 + i * 5, 250 + i * 3, -120 + i * 2, 380, 1, M.ROCK, 160);
        for (let i = 0; i < 90; i++) { const x = -S + hash(i, 3) * 2 * S, y = 366 + hash(i, 4) * 60; if (Math.abs(x) > 160 || y > 400) k.circle(x, y, 1.7, M.GOLD, 230); }
        k.rect(-16, 206, 32, 18, M.ST, 160);
        k.poly([-13, 206, 13, 206, 10, 110, -10, 110], M.ST, 220);
        k.poly([2, 206, 13, 206, 10, 110, 2, 110], M.ST, 150);
        k.poly([-118, 108, 118, 108, 118, 118, -118, 118], M.ST, 235);
        k.poly([-118, 114, 118, 114, 118, 118, -118, 118], M.ST, 160);
        k.rect(-12, 104, 24, 16, M.ST, 220);
        k.ellipse(0, 92, 7, 10, M.ST, 240);
      },
    },
    {
      name: "machu picchu", place: "cusco, peru",
      colors: { st: 8, stHi: 0, stShade: 10, dark: 1, rock: 9, rockFar: 14, ground: 2, leaf: 9, leafHi: 2, water: 5, sun: 3, gold: 3, skyTop: 13, skyMid: 6, skyLow: 5 },
      horizon: 300, clouds: { y0: 110, y1: 260, d: 0.5, speed: 0.5 }, stars: 0, birds: 4,
      draw(k, W) {
        const S = span(W);
        k.sky(30, 150, 300); k.glow(-250, 70, 110, 130, 70);
        k.ridge([-S, 190, -320, 140, -200, 180, 260, 165, 380, 125, S, 160], 440, M.ROCK, 55);
        k.ridge([-S, 240, -360, 200, -240, 230, -120, 215, 260, 230, 400, 200, S, 228], 440, M.ROCK, 75);
        k.ridge([20, 300, 80, 150, 115, 60, 150, 34, 182, 52, 210, 130, 255, 230, 320, 300], 440, M.ROCK, 155);
        k.poly([150, 34, 182, 52, 210, 130, 255, 230, 320, 300, 160, 300], M.ROCK, 100);
        for (let i = 0; i < 26; i++) k.line(100 + i * 2.2, 70 + i * 9, 80 + i * 3.2, 300, 1.2, M.ROCK, 200);
        k.ridge([-S, 300, -250, 270, -120, 285, 60, 290, 250, 320, S, 340], 440, M.GROUND, 120);
        for (let i = 0; i < 11; i++) { const y = 296 + i * 12; k.line(-S, y + 6, -40 - i * 16, y, 2, M.ST, 230); k.line(-S, y + 9, -40 - i * 16, y + 3, 1, M.GROUND, 70); }
        k.rect(-60, 296, 150, 30, M.LEAF, 170);
        for (let i = 0; i < 18; i++) {
          const x = -270 + (i % 9) * 64 + (i > 8 ? 26 : 0), y = i > 8 ? 352 : 316, w = 40 + hash(i, 1) * 16, h = 22;
          if (x > -70 && x < 95 && y < 330) continue;
          k.rect(x, y, w, h, M.ST, 200); k.rect(x + w - 6, y, 6, h, M.ST, 130);
          k.poly([x, y, x + w / 2, y - 18, x + w, y], M.ST, 228);
          k.rect(x + w * 0.35, y + 4, 5, 9, M.DARK, 35); k.line(x, y + h, x + w, y + h, 1, M.ST, 245);
        }
        for (let i = 0; i < 30; i++) k.cypress(-S + hash(i, 5) * 2 * S, 380 + hash(i, 6) * 60, 14, 5, 140 + hash(i, 7) * 50);
      },
    },
    {
      name: "chichen itza", place: "yucatan, mexico",
      colors: { st: 8, stHi: 0, stShade: 10, dark: 1, rock: 9, rockFar: 14, ground: 9, leaf: 9, leafHi: 2, water: 5, sun: 3, gold: 3, skyTop: 6, skyMid: 5, skyLow: 3 },
      horizon: 330, clouds: { y0: 20, y1: 150, d: 0.52, speed: 0.7 }, stars: 0, birds: 4,
      draw(k, W) {
        const S = span(W);
        k.sky(40, 160, 330); k.glow(280, 80, 130, 150, 70);
        k.circle(280, 80, 22, M.SUN, 250);
        k.ridge([-S, 316, -300, 300, -200, 312, 200, 306, 330, 298, S, 314], 340, M.LEAF, 120);
        for (let i = 0; i < 60; i++) { const x = -S + i * (2 * S / 60); k.ellipse(x, 312 - hash(i, 4) * 10, 14, 9, M.LEAF, 140 + hash(i, 5) * 50); }
        k.rect(-S, 330, 2 * S, 110, M.GROUND, 120);
        for (let i = 0; i < 200; i++) { const x = -S + hash(i, 8) * 2 * S, y = 336 + hash(i, 9) * 100; k.line(x, y, x + 2, y - 5, 1, M.LEAF, 170); }
        k.poly([-230, 330, 256, 330, 360, 352, -160, 352], M.GROUND, 55);
        for (let i = 0; i < 9; i++) {
          const y = 330 - (i + 1) * 20, hw = 230 - i * 18;
          k.rect(-hw, y, 2 * hw, 20, M.ST, 185 + (i % 2) * 15);
          k.poly([hw, y, hw + 26, y + 6, hw + 26, y + 26, hw, y + 20], M.ST, 115);
          k.line(-hw, y, hw, y, 1.6, M.ST, 250);
          for (let x = -hw + 8; x < hw - 8; x += 14) k.line(x, y + 4, x, y + 16, 0.8, M.ST, 140);
        }
        k.rect(-34, 150, 68, 180, M.ST, 225);
        for (let y = 154; y < 330; y += 6) k.line(-34, y, 34, y, 1, M.ST, 150);
        for (const s of [-1, 1]) { k.rect(s * 34 - (s > 0 ? 0 : 6), 150, 6, 180, M.ST, 248); k.ellipse(s * 40, 326, 10, 6, M.ST, 235); }
        k.rect(-52, 104, 104, 46, M.ST, 205); k.rect(26, 104, 26, 46, M.ST, 140); k.rect(-56, 100, 112, 6, M.ST, 248);
        for (const x of [-34, 0, 34]) k.rect(x - 7, 120, 14, 30, M.DARK, 30);
        k.rect(-46, 108, 92, 4, M.ST, 150);
      },
    },
  ];

  /* ---------- rasterise to cells ---------- */
  function make(index, C, R, cw, ch) {
    const wonder = LIST[((index % LIST.length) + LIST.length) % LIST.length];
    const pxW = Math.max(1, Math.round(C * cw * 0.5)), pxH = Math.max(1, Math.round(R * ch * 0.5));
    const cv = document.createElement("canvas"); cv.width = pxW; cv.height = pxH;
    const g = cv.getContext("2d", { willReadFrequently: true }), s = pxH / 440, W = pxW / s;
    g.fillStyle = `rgb(0,${M.SKY * 20 + 10},0)`; g.fillRect(0, 0, pxW, pxH);
    g.setTransform(s, 0, 0, s, pxW / 2, 0);
    wonder.draw(kit(g), W);
    const img = g.getImageData(0, 0, pxW, pxH).data, cellW = pxW / C, cellH = pxH / R;
    const n = C * R, lum = new Uint8Array(n), mat = new Uint8Array(n), base = new Uint16Array(n), bcol = new Uint8Array(n);
    const px = (x, y) => { x = Math.min(pxW - 1, Math.max(0, x | 0)); y = Math.min(pxH - 1, Math.max(0, y | 0)); return (y * pxW + x) * 4; };
    const K = wonder.colors, hRow = Math.round(wonder.horizon / 440 * R), votes = new Uint8Array(9);
    for (let r = 0; r < R; r++) for (let c = 0; c < C; c++) {
      let sum = 0, mn = 255, mx = 0, gl = 0, gr = 0, gt = 0, gb = 0; votes.fill(0);
      for (let j = 0; j < 3; j++) for (let i = 0; i < 3; i++) {
        const o = px((c + (i + 0.5) / 3) * cellW, (r + (j + 0.5) / 3) * cellH), l = img[o], m = Math.max(0, Math.min(8, Math.round((img[o + 1] - 10) / 20)));
        sum += l; if (l < mn) mn = l; if (l > mx) mx = l; votes[m]++;
        if (i === 0) gl += l; if (i === 2) gr += l; if (j === 0) gt += l; if (j === 2) gb += l;
      }
      let m = 0; for (let q = 1; q < 9; q++) if (votes[q] > votes[m]) m = q;
      const L = sum / 9, i = r * C + c, hh = hash(c, r); lum[i] = L; mat[i] = m;
      let k = 32, col = 1;
      if ((m === M.ST || m === M.GOLD) && mx - mn > 70) {
        const gx = gr - gl, gy = gb - gt, ax = Math.abs(gx), ay = Math.abs(gy);
        k = ay > ax * 1.8 ? (gy > 0 ? 95 : 45) : ax > ay * 1.8 ? 124 : (gx > 0) === (gy > 0) ? 92 : 47;
      } else if (m === M.DARK) { k = L > 45 ? 58 : hh > 0.6 ? 46 : 32; }
      else if (m === M.SKY) {
        const d = L / 255;   // the sky dither thickens toward the glow and the horizon
        if (hh < d * 0.85) k = d > 0.62 ? (hh < d * 0.3 ? 61 : 45) : d > 0.4 ? 58 : 46;
      } else {
        const tex = m === M.ROCK || m === M.GROUND || m === M.LEAF ? (fbm(c * 0.22, r * 0.45) - 0.5) * 70 : 0;
        const LL = m === M.ST || m === M.GOLD || m === M.SUN ? L : Math.max(0, L * 0.62 + 12 + tex);
        k = RAMP.charCodeAt(Math.min(9, Math.max(1, Math.floor(LL / 256 * 10))));
        if (LL < 46 && hh > 0.45) k = 32;
      }
      if (m === M.ST) col = L > 228 ? K.stHi : L > 150 ? K.st : K.stShade;
      else if (m === M.GOLD) col = K.gold;
      else if (m === M.DARK) col = K.dark;
      else if (m === M.ROCK) col = L > 120 ? K.rock : L > 70 ? K.rockFar : 1;
      else if (m === M.GROUND) col = L > 100 ? K.ground : 1;
      else if (m === M.LEAF) col = L > 160 ? K.leafHi : K.leaf;
      else if (m === M.SUN) { col = K.sun; k = L > 220 ? 64 : 42; }
      else if (m === M.WATER) col = K.water;
      else if (m === M.SKY) { const d = r / Math.max(1, hRow); col = d < 0.33 ? K.skyTop : d < 0.72 ? K.skyMid : K.skyLow; if (L > 170) col = K.sun; }
      base[i] = k; bcol[i] = col;
    }
    // outline every landmass against the sky, with slope glyphs, so ridges read as forms
    const land = (q) => q === M.ROCK || q === M.LEAF || q === M.GROUND;
    const top = (r, c) => r > 0 && r < R && c >= 0 && c < C && land(mat[r * C + c]) && mat[(r - 1) * C + c] === M.SKY;
    for (let r = 1; r < R; r++) for (let c = 0; c < C; c++) {
      if (!top(r, c)) continue;
      const i = r * C + c, up = top(r - 1, c + 1) || top(r - 1, c - 1);
      base[i] = top(r - 1, c + 1) ? 47 : top(r - 1, c - 1) ? 92 : (top(r + 1, c + 1) ? 92 : top(r + 1, c - 1) ? 47 : 95);
      if (!up && base[i] === 95 && hash(c, r) > 0.7) base[i] = 45;
      bcol[i] = mat[i] === M.LEAF ? K.leafHi : mat[i] === M.ROCK ? (lum[i] > 100 ? K.rock : K.rockFar) : K.ground;
    }
    const grid = { wonder, C, R, lum, mat, base, bcol, code: new Uint16Array(base), col: new Uint8Array(bcol), hRow };
    grid.sky = []; grid.water = []; grid.leaf = []; grid.stone = []; grid.stars = []; grid.lights = [];
    for (let i = 0; i < n; i++) {
      const m = mat[i];
      if (m === M.SKY) { grid.sky.push(i); if (lum[i] < 90 && hash(i, 77) < wonder.stars) grid.stars.push(i); }
      else if (m === M.WATER) grid.water.push(i);
      else if (m === M.LEAF) grid.leaf.push(i);
      else if (m === M.GOLD && lum[i] < 240) grid.lights.push(i);
      else if (m === M.ST || m === M.ROCK) grid.stone.push(i);
    }
    return grid;
  }

  const WAVES = "~-~_~=-~", LEAVES = ",;'`\":;,", TWINKLE = ".+*+.";
  function step(G, t) {
    const { C, R, wonder, code, col, base, bcol, lum } = G, K = wonder.colors, cl = wonder.clouds;
    const r0 = Math.floor(cl.y0 / 440 * R), r1 = Math.ceil(cl.y1 / 440 * R);
    for (const i of G.sky) {                                   // clouds drift across their band
      const r = (i / C) | 0, c = i - r * C;
      code[i] = base[i]; col[i] = bcol[i];
      if (r >= r0 && r <= r1) {
        const edge = Math.min(r - r0, r1 - r) / Math.max(1, (r1 - r0) / 2);
        const d = fbm(c * 0.04 - t * cl.speed * 0.6, r * 0.17 + 3.1) * Math.min(1, edge * 1.6 + 0.2);
        if (d > cl.d) { const q = (d - cl.d) / (1 - cl.d); code[i] = q > 0.55 ? 64 : q > 0.38 ? 37 : q > 0.22 ? 61 : 126; col[i] = q > 0.45 ? 0 : q > 0.25 ? 14 : bcol[i]; }
        else if (d > cl.d - 0.06) { code[i] = 46; col[i] = 14; }
      }
    }
    for (const i of G.stars) { code[i] = TWINKLE.charCodeAt(Math.floor(t * 3 + hash(i, 3) * 5) % TWINKLE.length); col[i] = 3; }
    for (const i of G.lights) { code[i] = hash(i, Math.floor(t * 2.5)) > 0.35 ? 42 : 46; col[i] = 3; }
    for (let b = 0; b < (wonder.birds || 0); b++) {            // a few birds cross the upper sky
      const sp = 3 + hash(b, 1) * 4, row = 3 + Math.floor(hash(b, 2) * Math.max(1, G.hRow * 0.45));
      const c = Math.floor(((t * sp + hash(b, 3) * C) % (C + 20)) - 10), flap = Math.floor(t * 4 + b) % 2;
      const yy = row + Math.round(Math.sin(t * 0.8 + b) * 1.2);
      for (const dc of [0, 1]) {
        const cc = c + dc; if (cc < 0 || cc >= C || yy < 0 || yy >= R) continue;
        const i = yy * C + cc; if (G.mat[i] === M.SKY) { code[i] = flap ? 118 : 45; col[i] = 0; }
      }
    }
    for (const i of G.water) {                                 // ripples, and a broken reflection
      const r = (i / C) | 0, c = i - r * C, rr = 2 * G.hRow - r - 1, off = Math.round(Math.sin(r * 1.7 + t * 3.2) * 1.5);
      const sc = Math.min(C - 1, Math.max(0, c + off)), j = rr >= 0 && rr < R ? rr * C + sc : -1;
      const wave = WAVES.charCodeAt(((c + Math.floor(t * 6) + r * 3) % WAVES.length + WAVES.length) % WAVES.length);
      if (j >= 0 && G.mat[j] !== M.SKY && G.mat[j] !== M.WATER && lum[j] > 120 && hash(c, r + Math.floor(t * 4)) > 0.25) { code[i] = base[j] === 32 ? 61 : base[j]; col[i] = bcol[j] === 0 ? 5 : bcol[j]; }
      else { const shimmer = hash(c * 7 + r, Math.floor(t * 5)); code[i] = shimmer > 0.45 ? wave : 32; col[i] = shimmer > 0.9 ? 0 : (c + r) % 3 ? K.water : 13; }
    }
    for (const i of G.leaf) {                                  // foliage sways with a travelling gust
      const r = (i / C) | 0, c = i - r * C, gust = Math.sin(c * 0.25 - t * 2.4 + r * 0.4);
      code[i] = gust > 0.55 ? LEAVES.charCodeAt((c + r + Math.floor(t * 5)) % LEAVES.length) : base[i];
      col[i] = gust > 0.82 ? K.leafHi : bcol[i];
    }
    const tick = Math.floor(t * 8);                            // stone: a faint shimmer
    for (const i of G.stone) code[i] = base[i];
    for (let q = tick % 29; q < G.stone.length; q += 29) {
      const i = G.stone[q], p = RAMP.indexOf(String.fromCharCode(base[i]));
      if (p > 1 && hash(i, tick) > 0.5) code[i] = RAMP.charCodeAt(p - 1);
    }
  }

  window.Wonders = { list: LIST.map((w) => ({ name: w.name, place: w.place })), palette: PALETTE, make, step };
})();
