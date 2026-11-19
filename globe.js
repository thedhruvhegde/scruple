/* globe.js: the earth from space, drawn in characters.
 *
 * a sphere is ray-cast once per character cell when the screen is laid out: each cell keeps its
 * latitude, its longitude relative to the sub-viewer point, and how the sun and the eye see it.
 * after that, a frame only turns the planet: it adds the spin to every longitude and looks the cell
 * up in textures built once at load: land from natural earth (50m), biomes, city lights, and a
 * cloud deck that drifts slower than the ground. light is lambert plus a sun glint on the sea,
 * a sunset band along the terminator, city lights and aurora on the night side, and a blue
 * atmosphere that glows past the limb. stars twinkle and drift, meteors fall, a satellite orbits.
 *
 * same api as ocean.js and wonders.js, so the page can swap one for another:
 *   Globe.list, Globe.palette, Globe.make(index, cols, rows, cellW, cellH), Globe.step(grid, seconds)
 * a grid exposes code (Uint16Array) and col (Uint8Array), one entry per cell, row-major.
 * the palette keeps the 16 site colours at 0-15 and adds globe colours from 16 on.
 * land outline: natural earth via world-atlas (public domain), rasterised to 1024x512.
 */
(function () {
  "use strict";
  const BASE = ["#E6E3D6", "#55617D", "#6CC39B", "#E8C46A", "#F27B55", "#93B3EA", "#4F6FB4", "#E3967A",
    "#B9B09A", "#3F9A6E", "#857D6A", "#B0634C", "#AD8838", "#34497A", "#7486AD", "#C9779F"];
  const GC = {
    starW: "#F4F7FF", starB: "#9FC4FF", starY: "#FFE3A8",
    atm1: "#8AF3FF", atm2: "#3DB8FF", atm3: "#2470E6", atm4: "#1A3F96",
    oDeep: "#0F3478", oMid: "#1C55B5", oLit: "#3388EC", oShal: "#3FD0E6", glint: "#FFF8E1",
    jungle: "#1F8A44", forest: "#3FA552", grass: "#8CC25C", savanna: "#C9B466", desert: "#E8C285", dune: "#F7E3B4",
    rock: "#A27E5A", tundra: "#9AB095", ice: "#F4FAFF", iceSh: "#B3CDEF",
    cloud: "#FFFFFF", cloudSh: "#BCCBE0",
    city: "#FFB534", cityHot: "#FFEDB8", nLand: "#22355E", nSea: "#132449",
    dusk: "#FF7638", duskHi: "#FFC870",
    aur: "#4DFFA8", aur2: "#C46BFF",
    moon: "#DCD8CC", moonSh: "#807C72",
  };
  const PALETTE = BASE.concat(Object.values(GC));
  const K = {}; Object.keys(GC).forEach((k, i) => (K[k] = 16 + i));

  // glyphs sorted from lightest to heaviest ink
  // no letters: the planet should read as an image, not as text
  const RAMP = " `.,-':;_~^\"!+=<>*?/\\|()[]{}#%&$@";
  const NG = RAMP.length, RC = Array.from(RAMP, (ch) => ch.charCodeAt(0));

  function hash(a, b) { let h = Math.imul(a ^ 0x9e3779b9, 0x85ebca6b) ^ Math.imul(b + 0x632be5ab, 0xc2b2ae35); h ^= h >>> 15; h = Math.imul(h, 0x2c1b3c6d); h ^= h >>> 12; return (h >>> 0) / 4294967296; }
  const h3 = (x, y, z) => hash(Math.imul(x, 73856093) ^ Math.imul(y, 19349663), z * 83492791 | 0);
  const smooth = (a, b, x) => { const t = Math.min(1, Math.max(0, (x - a) / (b - a))); return t * t * (3 - 2 * t); };
  const clamp = (x, a, b) => (x < a ? a : x > b ? b : x);
  const wrap = (x, lo, hi) => { const w = hi - lo; return lo + ((((x - lo) % w) + w) % w); };
  const sq = (x) => x * x, TAU = Math.PI * 2, D2R = Math.PI / 180;

  function vnoise(x, y, z) {
    const xi = Math.floor(x), yi = Math.floor(y), zi = Math.floor(z);
    let xf = x - xi, yf = y - yi, zf = z - zi;
    xf = xf * xf * (3 - 2 * xf); yf = yf * yf * (3 - 2 * yf); zf = zf * zf * (3 - 2 * zf);
    const l = (a, b, t) => a + (b - a) * t;
    return l(l(l(h3(xi, yi, zi), h3(xi + 1, yi, zi), xf), l(h3(xi, yi + 1, zi), h3(xi + 1, yi + 1, zi), xf), yf),
             l(l(h3(xi, yi, zi + 1), h3(xi + 1, yi, zi + 1), xf), l(h3(xi, yi + 1, zi + 1), h3(xi + 1, yi + 1, zi + 1), xf), yf), zf);
  }
  function fbm(x, y, z, o) { let s = 0, a = 0.5, n = 0; for (let i = 0; i < o; i++) { s += a * vnoise(x, y, z); n += a; x *= 2.03; y *= 2.03; z *= 2.03; a *= 0.5; } return s / n; }

  /* ---------- textures, built once ---------- */
  const TW = 1024, TH = 512;
  const MASK = "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";
  let TEX = null;
  function textures() {
    if (TEX) return TEX;
    const N = TW * TH, m = new Uint8Array(N), bin = atob(MASK);
    for (let i = 0, p = 0, cur = 0; i < bin.length; cur ^= 1) {
      let n = 0, sh = 0, b; do { b = bin.charCodeAt(i++); n |= (b & 127) << sh; sh += 7; } while (b & 128);
      m.fill(cur, p, p + n); p += n;
    }
    // near-land: a wide box blur of the mask, for shallow water and coastal city lights
    const blur = (src, rad) => {
      const a = new Float32Array(N), o = new Float32Array(N);
      for (let y = 0; y < TH; y++) { let s = 0; const r0 = y * TW;
        for (let x = -rad; x <= rad; x++) s += src[r0 + ((x + TW) % TW)];
        for (let x = 0; x < TW; x++) { a[r0 + x] = s / (2 * rad + 1); s += src[r0 + ((x + rad + 1) % TW)] - src[r0 + ((x - rad + TW) % TW)]; } }
      for (let x = 0; x < TW; x++) { let s = 0;
        for (let y = -rad; y <= rad; y++) s += a[clamp(y, 0, TH - 1) * TW + x];
        for (let y = 0; y < TH; y++) { o[y * TW + x] = s / (2 * rad + 1); s += a[clamp(y + rad + 1, 0, TH - 1) * TW + x] - a[clamp(y - rad, 0, TH - 1) * TW + x]; } }
      return o;
    };
    const near = blur(m, 4);
    const blob = (lat, lon, b) => { let dl = lon - b[1]; dl = wrap(dl, -180, 180); return b[4] * Math.exp(-sq((lat - b[0]) / b[2]) - sq(dl / b[3])); };
    const maxb = (lat, lon, list) => { let v = 0; for (const b of list) v = Math.max(v, blob(lat, lon, b)); return v; };
    // [lat, lon, lat spread, lon spread, weight]
    const DESERT = [[22, 8, 9, 26, 1], [22, 30, 8, 10, 1], [24, 47, 8, 10, 1], [27, 70, 4, 5, .8], [43, 103, 5, 14, .9], [41, 62, 5, 12, .8], [31, 57, 5, 8, .8],
      [-24, 19, 6, 6, .9], [-25, 132, 8, 15, 1], [33, -112, 5, 6, .8], [40, -117, 4, 4, .6], [-23, -69, 6, 2.5, .9], [-44, -68, 6, 4, .6], [38, 84, 4, 8, .9]];
    const JUNGLE = [[-4, -62, 9, 13, 1], [0, 22, 6, 9, 1], [1, 112, 7, 14, .9], [-5, 140, 4, 8, .9], [12, -86, 5, 6, .8], [15, 102, 6, 6, .8], [-18, 47, 5, 3, .6], [8, -10, 4, 6, .7]];
    const MOUNT = [[31, 84, 4, 12, 1], [-20, -68, 18, 3, .9], [45, -113, 9, 4, .8], [46, 9, 2, 4, .7], [37, 72, 4, 6, .8], [42, 45, 2, 5, .6], [-43, 171, 2, 2, .7]];
    const CITY = [[50, 8, 6, 12, 1], [52, -1, 3, 4, 1], [45, 12, 4, 8, .8], [56, 37, 4, 10, .6], [23, 79, 8, 9, 1], [31, 115, 8, 9, 1], [36, 137, 3, 4, 1], [37, 127, 2, 2, .9],
      [39, -78, 5, 9, 1], [42, -88, 4, 6, .8], [34, -118, 3, 3, .9], [30, -96, 4, 6, .8], [-23, -46, 4, 5, .9], [19, -99, 4, 4, .9], [7, 6, 4, 5, .7], [29, 31, 5, 2, .8],
      [25, 52, 4, 6, .6], [14, 101, 4, 4, .6], [-34, 151, 2, 3, .6], [-34, -59, 2, 3, .7], [6, 107, 4, 6, .7], [24, 121, 3, 3, .7], [-26, 28, 3, 3, .6], [4, -74, 4, 3, .6], [33, 35, 4, 4, .7], [41, 29, 3, 6, .7]];
    // the blobs are smooth, so evaluate them on a quarter-resolution grid and look them up
    const BW = TW >> 2, BH = TH >> 2, BD = new Float32Array(BW * BH * 4);
    for (let y = 0; y < BH; y++) for (let x = 0; x < BW; x++) {
      const la = 90 - (y + 0.5) * 180 / BH, lo = (x + 0.5) * 360 / BW - 180, j = 4 * (y * BW + x);
      BD[j] = maxb(la, lo, DESERT); BD[j + 1] = maxb(la, lo, JUNGLE); BD[j + 2] = maxb(la, lo, MOUNT); BD[j + 3] = maxb(la, lo, CITY);
    }
    const col = new Uint8Array(N), alb = new Float32Array(N), water = new Uint8Array(N), city = new Float32Array(N);
    for (let y = 0; y < TH; y++) {
      const lat = 90 - (y + 0.5) * 180 / TH, cl = Math.cos(lat * D2R), sl = Math.sin(lat * D2R), alat = Math.abs(lat);
      for (let x = 0; x < TW; x++) {
        const i = y * TW + x, lon = (x + 0.5) * 360 / TW - 180;
        const px = cl * Math.sin(lon * D2R), py = sl, pz = cl * Math.cos(lon * D2R);
        if (!m[i]) {
          water[i] = 1; const sh = near[i];
          col[i] = sh > 0.12 ? K.oShal : K.oMid; alb[i] = sh > 0.12 ? 0.7 : 0.5;
          if (alat > 70 && fbm(px * 6 + 11, py * 6, pz * 6, 3) > 0.5 - (alat - 70) / 30) { water[i] = 0; col[i] = K.ice; alb[i] = 0.95; }   // sea ice
          continue;
        }
        const n1 = fbm(px * 6 + 11, py * 6, pz * 6, 4), n2 = vnoise(px * 40, py * 40 + 5, pz * 40);
        const bj = 4 * ((y >> 2) * BW + (x >> 2));
        const des = BD[bj] + (n1 - 0.5) * 0.7, jun = BD[bj + 1] + (n1 - 0.5) * 0.6, mnt = BD[bj + 2] * (0.6 + 0.8 * n2);
        let c, a;
        const green = lat > 59 && lat < 84 && lon > -75 && lon < -10;
        if (lat < -62 || green || alat > 80) { c = n2 > 0.55 ? K.iceSh : K.ice; a = 0.95; }
        else if (alat > 66) { c = n1 > 0.52 ? K.ice : K.tundra; a = 0.8; }
        else if (mnt > 0.55) { c = mnt > 0.85 && alat > 25 ? K.ice : K.rock; a = mnt > 0.85 ? 0.9 : 0.66; }
        else if (des > 0.52) { c = n2 > 0.6 ? K.dune : K.desert; a = 0.88; }
        else if (jun > 0.5) { c = K.jungle; a = 0.48; }
        else if (alat > 52) { c = n1 > 0.55 ? K.tundra : K.forest; a = 0.55; }
        else if (alat < 22 && des > 0.25) { c = K.savanna; a = 0.72; }
        else { c = n1 > 0.5 ? K.grass : K.forest; a = n1 > 0.5 ? 0.66 : 0.55; }
        col[i] = c; alb[i] = a * (0.86 + 0.28 * n2);
        const pop = BD[bj + 3] + 0.12 * (1 - near[i]) * 0 + 0.15 * smooth(0.3, 0.9, 1 - near[i]) * (alat < 60 ? 1 : 0);
        const sp = hash(x, y * 7 + 3), grain = smooth(0.7, 1, vnoise(px * 90, py * 90, pz * 90)) + (sp > 0.985 ? 0.5 : 0);
        city[i] = alat > 65 || lat < -50 ? 0 : clamp(pop * (0.45 + 1.5 * grain) + (sp > 0.993 ? 0.35 : 0), 0, 1);
      }
    }
    // clouds: domain-warped fbm on the sphere, banded by latitude (ITCZ, clear subtropics, storm tracks)
    const CW = 384, CH = 192, cloud = new Float32Array(CW * CH);
    for (let y = 0; y < CH; y++) {
      const lat = 90 - (y + 0.5) * 180 / CH, cl = Math.cos(lat * D2R), sl = Math.sin(lat * D2R), alat = Math.abs(lat);
      const band = 0.2 * Math.exp(-sq((lat - 5) / 7)) + 0.22 * Math.exp(-sq((alat - 55) / 13)) - 0.2 * Math.exp(-sq((alat - 24) / 8)) + (alat > 75 ? 0.05 : 0);
      for (let x = 0; x < CW; x++) {
        const lon = (x + 0.5) * TAU / CW, px = cl * Math.sin(lon), py = sl, pz = cl * Math.cos(lon);
        const wx = fbm(px * 2.2 + 3, py * 2.2, pz * 2.2, 2), wy = fbm(px * 2.2, py * 2.2 + 7, pz * 2.2, 2);
        const v = fbm(px * 3.4 + 2.2 * wx, py * 5 + 2.2 * wy, pz * 3.4 - 1.6 * wx, 5);
        cloud[y * CW + x] = smooth(0.54, 0.7, v + band);
      }
    }
    TEX = { col, alb, water, city, cloud, CW, CH };
    return TEX;
  }

  /* ---------- scenes ---------- */
  // r: radius in U, where U is the screen's short side (capped for wide screens).
  const LIST = [
    { name: "the blue marble", place: "earth from 35,000 km, turning", r: 0.47, cx: 0.57, cy: 0.5, sun: [0.62, 0.38, 0.69], tilt: 0.38, pitch: 0.22, lon0: 25, spin: TAU / 48, halo: 0.05, moon: true, sat: true, meteors: true, aurora: true },
    { name: "the night side", place: "city lights and aurora from low orbit", r: 1.0, cx: 0.63, cy: 1.12, sun: [0.95, 0.35, -0.3], tilt: 0.2, pitch: 0.5, lon0: 70, spin: TAU / 70, halo: 0.06, moon: false, sat: true, meteors: true, aurora: true },
    { name: "orbital sunrise", place: "the terminator, from the space station's height", r: 2.3, cx: 0.46, top: 0.5, sunAt: [0.66, -0.04], tilt: -0.1, pitch: 0.3, lon0: 150, spin: TAU / 110, halo: 0.07, moon: false, sat: false, meteors: true },
  ];

  function make(index, C, R, cw, ch) {
    const T = textures(), P = LIST[index % LIST.length];
    const Wp = C * cw, Hp = R * ch, U = Math.min(Hp, Wp / 1.7), N = C * R;
    const rad = P.r * U, gx = P.cx * Wp, gy = P.top !== undefined ? P.top * Hp + rad : P.cy * Hp;
    let L = P.sun;
    let sunX = 0, sunY = 0;
    if (P.sunAt) {   // a sun just over the limb: light from behind and above
      sunX = P.sunAt[0] * Wp; sunY = gy - rad + P.sunAt[1] * U;
      const vx = (sunX - gx) / rad, vy = -(sunY - gy) / rad;
      L = [vx * 0.32, vy * 0.32, -0.95];
    }
    { const l = Math.hypot(L[0], L[1], L[2]); L = [L[0] / l, L[1] / l, L[2] / l]; }
    const Hh = (() => { const x = L[0], y = L[1], z = L[2] + 1, l = Math.hypot(x, y, z); return [x / l, y / l, z / l]; })();
    const ct = Math.cos(P.tilt), st = Math.sin(P.tilt), cp = Math.cos(P.pitch), sp = Math.sin(P.pitch);
    const kind = new Uint8Array(N), lat = new Float32Array(2 * N), lon = new Float32Array(2 * N);
    const diff = new Float32Array(N), spec = new Float32Array(N), rim = new Float32Array(N), glow = new Float32Array(N), gcol = new Uint8Array(N);
    const haloPx = P.halo * U, fwd = Math.max(0, -L[2]);
    const view = (px, py) => {   // pixel -> unit view vector on the sphere, or null
      const x = (px - gx) / rad, y = -(py - gy) / rad, d2 = x * x + y * y;
      if (d2 >= 1) return null;
      return [x, y, Math.sqrt(1 - d2)];
    };
    const toWorld = (v, k, i) => {
      const x1 = v[0] * ct + v[1] * st, y1 = -v[0] * st + v[1] * ct, z1 = v[2];
      const y2 = y1 * cp + z1 * sp, z2 = -y1 * sp + z1 * cp;
      lat[2 * i + k] = Math.asin(clamp(y2, -1, 1)); lon[2 * i + k] = Math.atan2(x1, z2);
    };
    for (let r = 0; r < R; r++) for (let c = 0; c < C; c++) {
      const i = r * C + c, px = (c + 0.5) * cw, py = (r + 0.5) * ch;
      const v = view(px, py);
      if (v) {
        kind[i] = 1;
        const va = view(px, py - ch * 0.25) || v, vb = view(px, py + ch * 0.25) || v;
        toWorld(va, 0, i); toWorld(vb, 1, i);
        diff[i] = v[0] * L[0] + v[1] * L[1] + v[2] * L[2];
        spec[i] = Math.pow(Math.max(0, v[0] * Hh[0] + v[1] * Hh[1] + v[2] * Hh[2]), 90);
        rim[i] = Math.pow(1 - v[2], 4);
        continue;
      }
      // past the limb: the atmosphere
      const dx = px - gx, dy = -(py - gy), d = Math.hypot(dx, dy), t = (d - rad) / haloPx;
      if (t > 4) continue;
      const nl = (dx * L[0] + dy * L[1]) / d;   // how much this bit of limb faces the sun
      const lit = clamp(nl + 0.35, 0, 1.35) * (1 + 3 * fwd * Math.pow(clamp(nl, 0, 1), 2));
      const g = Math.exp(-t * 1.15) * (0.1 + 0.95 * lit);
      if (g < 0.03) continue;
      kind[i] = 2; glow[i] = g;
      gcol[i] = fwd > 0.3 && nl > 0.75 && t < 0.6 ? (t < 0.25 ? K.duskHi : K.dusk) : t < 0.35 ? K.atm1 : t < 1.1 ? K.atm2 : t < 2.2 ? K.atm3 : K.atm4;
    }
    // stars
    const stars = [], ns = Math.round(Wp * Hp / 2600);
    for (let k = 0; k < ns; k++) {
      const m = Math.pow(hash(k, index * 31 + 1), 3.2);
      stars.push([hash(k, 2) * Wp, hash(k, 3) * Hp, m, hash(k, 4) * TAU, hash(k, 5) < 0.2 ? K.starB : hash(k, 5) < 0.32 ? K.starY : K.starW, 1.5 + hash(k, 6) * 3]);
    }
    // moon: a small lit sphere with craters
    let moon = null;
    if (P.moon) {
      const mx = 0.15 * Wp, my = 0.27 * Hp, mr = 0.075 * U, cells = [];
      for (let r = Math.max(0, Math.floor((my - mr) / ch)); r <= Math.min(R - 1, Math.ceil((my + mr) / ch)); r++)
        for (let c = Math.max(0, Math.floor((mx - mr) / cw)); c <= Math.min(C - 1, Math.ceil((mx + mr) / cw)); c++) {
          const x = ((c + 0.5) * cw - mx) / mr, y = -((r + 0.5) * ch - my) / mr, d2 = x * x + y * y;
          if (d2 >= 1) continue;
          const z = Math.sqrt(1 - d2), lam = Math.max(0, x * L[0] + y * L[1] + z * L[2]);
          const cr = fbm(x * 3 + 9, y * 3, z * 3, 3);
          cells.push([r * C + c, 0.06 + lam * (0.3 + 0.35 * cr), cr < 0.44 || lam < 0.25 ? K.moonSh : K.moon]);
        }
      moon = { x: mx, y: my, r: mr, cells };
    }
    return { C, R, cw, ch, Wp, Hp, U, N, P, L, index, gx, gy, rad, kind, lat, lon, diff, spec, rim, glow, gcol, stars, moon, sunX, sunY,
      code: new Uint16Array(N), col: new Uint8Array(N), dens: new Float32Array(N), dcol: new Uint8Array(N), fix: new Uint16Array(N) };
  }

  function step(G, t) {
    const T = TEX, { C, R, cw, ch, Wp, Hp, U, N, P, kind, lat, lon, diff, spec, rim, glow, gcol, dens, dcol, fix, code, col } = G;
    dens.fill(0); fix.fill(0);
    const spin = (P.lon0 * D2R) - P.spin * t, cspin = (P.lon0 * D2R) - P.spin * 0.86 * t;
    const tick = Math.floor(t * 24), CW = T.CW, CH = T.CH;
    for (let i = 0; i < N; i++) {
      const k = kind[i];
      if (k === 2) {   // atmosphere glow, with a drift of sparkling dust like light caught in haze
        let g = glow[i];
        const s = hash(i, tick >> 1);
        if (s < g * 0.22) g = Math.max(g, 0.5 + s * 2);
        dens[i] = g; dcol[i] = gcol[i]; continue;
      }
      if (k !== 1) continue;
      const df = diff[i], day = smooth(-0.12, 0.2, df), lightL = 0.16 + 0.95 * Math.pow(Math.max(0, df), 0.75);
      let dsum = 0, cBest = 0, cityMax = 0, cloudSum = 0, wsum = 0, aur = 0;
      let ci = 0;
      for (let s = 0; s < 2; s++) {
        const la = lat[2 * i + s], lo = lon[2 * i + s];
        const u = wrap((lo + spin) / TAU + 0.5, 0, 1), v = 0.5 - la / Math.PI;
        const tx = (u * TW) | 0, ty = Math.min(TH - 1, (v * TH) | 0), ti = ty * TW + tx;
        const uc = wrap((lo + cspin) / TAU + 0.5 + 0.004 * Math.sin(la * 6 + t * 0.15), 0, 1);
        const cv = T.cloud[Math.min(CH - 1, (v * CH) | 0) * CW + ((uc * CW) | 0)];
        cloudSum += cv; wsum += T.water[ti];
        dsum += T.alb[ti]; if (s === 0) cBest = T.col[ti];
        cityMax = Math.max(cityMax, T.city[ti]);
        const al = Math.abs(la) / D2R;
        if (P.aurora && al > 58 && al < 78) {   // aurora ovals, rippling curtains
          const lw = lo + spin;
          const c0 = 67 + 3.2 * Math.sin(lw * 3 + t * 0.45) + 1.5 * Math.sin(lw * 7 - t * 0.8);
          const ring = Math.exp(-sq((al - c0) / 1.7));
          const cur = 0.5 + 0.5 * Math.sin(lw * 19 + t * 1.6 + 2.5 * Math.sin(lw * 5 - t * 0.5));
          aur = Math.max(aur, ring * (0.15 + 0.85 * Math.pow(cur, 4)));
        }
      }
      const alb = dsum / 2, cv = cloudSum / 2, isWater = wsum >= 1;
      // day side
      let dd, dc;
      if (isWater) {
        const sp = spec[i] * (0.75 + 0.5 * hash(i, tick));
        dd = 0.08 + 0.2 * lightL + 0.06 * alb + sp * 1.3;
        dc = sp > 0.3 ? K.glint : cBest === K.oShal ? (lightL > 0.45 ? K.oShal : K.oMid) : lightL > 0.8 ? K.oLit : lightL > 0.42 ? K.oMid : K.oDeep;
      } else { dd = 0.16 + 0.5 * alb * lightL; dc = cBest; }
      if (cv > 0.02) {
        dd = dd * (1 - cv) + (0.4 + 0.55 * lightL) * cv;
        if (cv > 0.42) dc = lightL > 0.5 ? K.cloud : K.cloudSh;
      }
      // sunset along the terminator
      const dusk = Math.exp(-sq((df - 0.02) / 0.045));
      if (dusk > 0.35 && (cv > 0.3 || !isWater)) { dc = dusk > 0.75 && cv > 0.3 ? K.duskHi : K.dusk; dd = Math.max(dd, 0.35 + 0.4 * dusk * Math.max(cv, 0.4)); }
      // the limb takes on the blue of the air
      const air = rim[i] * (0.25 + Math.max(0, df));
      dd += air * 0.25; if (air > 0.4) dc = air > 0.7 ? K.atm1 : K.atm2;
      // night side
      let nd = isWater ? 0.035 + 0.03 * hash(i, 9) : 0.06 + 0.04 * hash(i, 9), nc = isWater ? K.nSea : K.nLand;
      const cityV = cityMax * (1 - 0.75 * cv) * (0.8 + 0.2 * Math.sin(t * 3 + i * 1.7));
      if (cityV > 0.1) { nd = 0.3 + 0.7 * cityV; nc = cityV > 0.55 ? K.cityHot : K.city; }
      if (aur > 0.2) { const a = aur * (0.8 + 0.2 * hash(i, tick >> 1)); if (aur > 0.3 && 0.15 + a * 0.6 > nd) { nd = 0.15 + a * 0.6; nc = a > 0.42 ? K.aur : K.aur2; } }
      if (cv > 0.4 && cityV <= 0.12 && aur <= 0.2) nd += 0.04;
      ci = day > 0.5 ? dc : nc;
      if (day < 0.5 && day > 0.05 && dd * day > nd * (1 - day)) ci = dc;
      dens[i] = dd * day + nd * (1 - day);
      if (day < 0.6 && (cityV > 0.1 || aur > 0.3)) { dens[i] = Math.max(dens[i], nd * (1 - day * 0.8)); ci = nc; }
      if (day < 0.5 && aur > 0.3 && (nc === K.aur || nc === K.aur2)) fix[i] = aur > 0.7 ? 124 : aur > 0.5 ? 33 : 58;   // | ! : curtains
      dcol[i] = ci;
    }
    // moon
    if (G.moon) for (const [i, d, c] of G.moon.cells) { dens[i] = d; dcol[i] = c; }
    // stars: twinkle and drift; hidden behind the planet and its air
    const inside = (x, y) => Math.hypot(x - G.gx, y - G.gy) < G.rad + P.halo * U * 1.2;
    for (const s of G.stars) {
      const x = wrap(s[0] - s[5] * t, 0, Wp), y = s[1];
      if (inside(x, y)) continue;
      const c = Math.min(C - 1, (x / cw) | 0), r = Math.min(R - 1, (y / ch) | 0), i = r * C + c;
      if (kind[i] || (G.moon && Math.hypot(x - G.moon.x, y - G.moon.y) < G.moon.r * 1.3)) continue;
      const tw = s[2] * (0.55 + 0.45 * Math.sin(t * (1.5 + s[3]) + s[3] * 7));
      if (tw < 0.04) continue;
      dens[i] = 0.3 + tw; dcol[i] = s[4];
      fix[i] = tw > 0.75 ? 42 : tw > 0.45 ? 43 : tw > 0.2 ? 46 : 96;   // * + . `
      if (tw > 0.85 && c > 0 && c < C - 1) {   // the brightest get a little cross of light
        const a = r * C + c - 1, b = r * C + c + 1;
        if (!kind[a]) { fix[a] = 45; dens[a] = 0.5; dcol[a] = s[4]; }
        if (!kind[b]) { fix[b] = 45; dens[b] = 0.5; dcol[b] = s[4]; }
      }
    }
    // meteors: one every few seconds, a short bright streak that fades
    if (P.meteors) {
      const per = 5.5, n = Math.floor(t / per), ph = (t - n * per) / 1.1;
      if (ph < 1) {
        const x0 = (0.1 + 0.8 * hash(n, 91 + G.index)) * Wp, y0 = (0.05 + 0.35 * hash(n, 92)) * Hp, ang = 0.35 + 0.3 * hash(n, 93), dir = hash(n, 94) < 0.5 ? -1 : 1;
        const len = 0.5 * U, vx = Math.cos(ang) * dir, vy = Math.sin(ang);
        const hx = x0 + vx * len * ph, hy = y0 + vy * len * ph, fade = Math.sin(Math.PI * ph);
        const glyph = dir * vx * vy > 0 ? 92 : 47;   // \ or /
        for (let k = 0; k < 16; k++) {
          const x = hx - vx * k * cw * 1.1, y = hy - vy * k * cw * 1.1, c = (x / cw) | 0, r = (y / ch) | 0;
          if (c < 0 || c >= C || r < 0 || r >= R) continue;
          const i = r * C + c; if (kind[i] || inside(x, y)) continue;
          const d = fade * (1.2 - k / 14);
          if (d > dens[i]) { dens[i] = d; dcol[i] = k < 2 ? K.starW : k < 6 ? K.atm1 : K.atm2; fix[i] = k === 0 ? 42 : glyph; }
        }
      }
    }
    // a satellite in a tilted low orbit, glinting; it passes behind the planet
    if (P.sat) {
      const a = t * 0.32 + G.index, orr = G.rad * 1.13, inc = 0.32;
      const ox = Math.cos(a) * orr, oz = Math.sin(a) * orr, oy = Math.sin(a) * orr * inc;
      const x = G.gx + ox, y = G.gy - oy - 0.35 * Math.cos(a) * orr * 0.25;
      const behind = oz < 0 && Math.hypot(x - G.gx, y - G.gy) < G.rad * 1.02;
      const c = (x / cw) | 0, r = (y / ch) | 0;
      if (!behind && c > 0 && c < C - 1 && r >= 0 && r < R) {
        const i = r * C + c, gl = 0.9 + 0.4 * Math.max(0, Math.sin(t * 2.3));
        dens[i] = gl; fix[i] = 35; dcol[i] = K.starW;                     // #
        dens[i - 1] = dens[i + 1] = 0.8; fix[i - 1] = fix[i + 1] = 61; dcol[i - 1] = dcol[i + 1] = K.atm1;   // = panels
      }
    }
    // a sun just breaking over the limb, with a flare along the horizon
    if (P.sunAt) {
      const sr = 0.07 * U, br = 0.92 + 0.08 * Math.sin(t * 0.9);
      const r0 = Math.max(0, Math.floor((G.sunY - 0.5 * U) / ch)), r1 = Math.min(R - 1, Math.ceil((G.sunY + 0.5 * U) / ch));
      for (let r = r0; r <= r1; r++) for (let c = 0; c < C; c++) {
        const x = (c + 0.5) * cw - G.sunX, y = (r + 0.5) * ch - G.sunY, d = Math.hypot(x, y);
        let v = 2.2 * Math.exp(-d / sr) + 0.9 * Math.exp(-Math.abs(y) / (0.012 * U)) * Math.exp(-Math.abs(x) / (0.55 * U));
        v *= br;
        // eight long rays, slowly turning
        const ang = Math.atan2(y, x), ray = Math.pow(Math.abs(Math.cos(4 * ang + t * 0.12)), 60) * Math.exp(-d / (0.22 * U));
        const i = r * C + c;
        if (kind[i] === 1) v *= 0.55;
        if (ray > 0.25 && d > sr * 1.2 && ray > v * 0.5) {
          const a = ((Math.atan2(-y, x) % Math.PI) + Math.PI) % Math.PI;
          dens[i] = Math.max(dens[i], 0.6 + ray * 0.5); dcol[i] = ray > 0.6 ? K.duskHi : K.dusk;
          fix[i] = a < 0.39 || a > 2.75 ? 45 : a < 1.18 ? 47 : a < 1.96 ? 124 : 92;   // - / | backslash
          continue;
        }
        if (v > 0.12 && v > dens[i] * 0.8) {
          dens[i] = Math.max(dens[i], v); fix[i] = 0;
          dcol[i] = v > 1.1 ? K.glint : v > 0.6 ? K.duskHi : v > 0.3 ? K.dusk : dcol[i] || K.dusk;
        }
      }
    }
    // density to glyph, with a gentle flicker so the surface shimmers like grain
    for (let i = 0; i < N; i++) {
      const d = dens[i];
      if (d < 0.035) { code[i] = 32; continue; }
      if (fix[i]) { code[i] = fix[i]; col[i] = dcol[i]; continue; }
      const h = hash(i, tick - (i % 3)), fl = (h + hash(i + 7, tick) - 1) * (kind[i] === 2 ? 3 : 0.8);
      const g = Math.max(1, Math.min(NG - 1, Math.round(Math.min(1, d) * (NG - 1) + fl)));
      code[i] = RC[g]; col[i] = dcol[i];
    }
  }

  window.Globe = { list: LIST.map((w) => ({ name: w.name, place: w.place })), palette: PALETTE, make, step };
})();
