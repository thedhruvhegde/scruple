/* ocean.js: an underwater scene drawn in characters. whale shark, dolphins and a reef school.
 *
 * every creature is a swimming silhouette: a top (dorsal) and bottom (ventral) outline along the
 * body, bent by a travelling wave that grows toward the tail. each character cell asks every
 * creature how far inside it is, which gives a density and a colour: density picks a glyph from a
 * ramp sorted by ink, with a flicker so the body shimmers; species and markings pick the colour
 * (clownfish bands, the whale shark's spots, a dolphin's pale belly). marine snow drifts down,
 * bubbles rise and light shafts sweep through the water behind them.
 *
 * same api as wonders.js, so the page can swap one for the other:
 *   Ocean.list, Ocean.palette, Ocean.make(index, cols, rows, cellW, cellH), Ocean.step(grid, seconds)
 * a grid exposes code (Uint16Array) and col (Uint8Array), one entry per cell, row-major.
 * the palette keeps the 16 site colours at 0-15 and adds ocean colours from 16 on.
 */
(function () {
  "use strict";
  const BASE = ["#E6E3D6", "#55617D", "#6CC39B", "#E8C46A", "#F27B55", "#93B3EA", "#4F6FB4", "#E3967A",
    "#B9B09A", "#3F9A6E", "#857D6A", "#B0634C", "#AD8838", "#34497A", "#7486AD", "#C9779F"];
  const OCEAN = {
    hi: "#EAF6FF", shark: "#4F86D9", sharkBelly: "#9CC9F0", dolphin: "#7193CC", dolphinBelly: "#E7DDF1",
    clown: "#FF7A1F", tang: "#2F6BFF", yellow: "#FFD62E", anthias: "#FF4FB8", anthiasLow: "#FF9A3C",
    neon: "#4FE8FF", neonRed: "#FF3D6B", parrot: "#2EE6A6", parrotTail: "#8A6BFF", snow: "#7FB6CF",
    bubble: "#B8ECFF", shaft: "#3D6E86", dark: "#1C2A44",
  };
  const PALETTE = BASE.concat(Object.values(OCEAN));
  const K = {}; Object.keys(OCEAN).forEach((k, i) => (K[k] = 16 + i));

  // glyphs sorted from lightest to heaviest ink
  const RAMP = " `.:-',_;!~><=+*?r/\\|JL()lcvxYizufo][t}{nj1ICZah%XkwmqpdbU#O08&QMWB$@";
  const NG = RAMP.length;
  const RC = Array.from(RAMP, (ch) => ch.charCodeAt(0));

  function hash(a, b) { let h = Math.imul(a ^ 0x9e3779b9, 0x85ebca6b) ^ Math.imul(b + 0x632be5ab, 0xc2b2ae35); h ^= h >>> 15; h = Math.imul(h, 0x2c1b3c6d); h ^= h >>> 12; return (h >>> 0) / 4294967296; }
  function rng(seed) { let s = seed >>> 0; return () => ((s = (s + 0x6D2B79F5) >>> 0), hash(s, 7)); }
  const smooth = (a, b, x) => { const t = Math.min(1, Math.max(0, (x - a) / (b - a))); return t * t * (3 - 2 * t); };
  const wrap = (x, lo, hi) => { const w = hi - lo; return lo + ((((x - lo) % w) + w) % w); };

  /* ---------- body outlines: (s, half-thickness / H), s = 0 at the head, 1 at the tail tip ---------- */
  const PROFILES = {
    whaleshark: {
      top: [[0, .50], [.02, .72], [.08, .86], [.25, .98], [.42, 1.0], [.46, 1.55], [.50, 1.85], [.53, 1.1], [.62, .80], [.70, .62], [.72, .80], [.735, .62], [.80, .32], [.84, .24], [.88, .55], [.94, 1.25], [1.0, 1.75]],
      bot: [[0, .45], [.03, .70], [.10, .88], [.20, .95], [.24, 1.75], [.30, 1.55], [.33, .92], [.55, .80], [.68, .55], [.70, .68], [.72, .50], [.82, .25], [.86, .22], [.92, .60], [.97, .95], [1.0, .85]] },
    dolphin: {
      top: [[0, .10], [.07, .14], [.10, .45], [.16, .80], [.30, 1.0], [.42, .95], [.46, 1.30], [.52, 1.95], [.56, 1.4], [.60, .80], [.75, .45], [.86, .22], [.90, .25], [.95, .32], [1.0, .38]],
      bot: [[0, .10], [.07, .14], [.12, .55], [.22, .90], [.25, 1.55], [.31, 1.65], [.34, 1.0], [.45, .95], [.62, .70], [.80, .35], [.86, .22], [.90, .25], [.95, .32], [1.0, .38]] },
    fish: {
      top: [[0, .25], [.06, .65], [.22, .98], [.40, 1.0], [.58, .80], [.74, .35], [.80, .25], [.86, .55], [.94, .95], [1.0, 1.05]],
      bot: [[0, .25], [.06, .60], [.22, .92], [.40, .95], [.58, .78], [.74, .35], [.80, .25], [.86, .55], [.94, .95], [1.0, 1.05]] },
    tang: {
      top: [[0, .30], [.05, .85], [.18, 1.15], [.40, 1.25], [.60, 1.05], [.74, .35], [.80, .24], [.86, .55], [.94, .95], [1.0, 1.0]],
      bot: [[0, .30], [.05, .80], [.18, 1.10], [.40, 1.20], [.60, 1.0], [.74, .35], [.80, .24], [.86, .55], [.94, .95], [1.0, 1.0]] },
  };
  // sample each outline into a lookup table
  const LUTN = 512;
  function lut(pts) {
    const a = new Float32Array(LUTN + 1);
    for (let i = 0; i <= LUTN; i++) {
      const s = i / LUTN; let j = 0;
      while (j < pts.length - 2 && pts[j + 1][0] < s) j++;
      const [s0, h0] = pts[j], [s1, h1] = pts[j + 1], k = s1 > s0 ? Math.min(1, Math.max(0, (s - s0) / (s1 - s0))) : 0;
      a[i] = h0 + (h1 - h0) * k;
    }
    return a;
  }
  const PROF = {}; for (const k in PROFILES) PROF[k] = { top: lut(PROFILES[k].top), bot: lut(PROFILES[k].bot) };
  const at = (a, s) => (s < 0 || s > 1 ? 0 : a[Math.round(s * LUTN)]);

  /* ---------- colour and markings per species ---------- */
  function paint(kind, s, vn, u, vv) {
    // returns [colour index, density multiplier]
    switch (kind) {
      case "whaleshark": {
        const a = u / 26, b = vv / 22, ra = Math.round(a), bb = b + 0.5 * ra;
        if (vn < 0.35 && Math.hypot(a - ra, bb - Math.round(bb)) < 0.24) return [K.hi, 1.25];
        return [vn > 0.25 ? K.sharkBelly : K.shark, 1];
      }
      case "dolphin": return [vn > 0.3 ? K.dolphinBelly : K.dolphin, 1];
      case "clown": {
        const band = Math.abs(s - .20) < .045 || Math.abs(s - .48) < .05 || Math.abs(s - .79) < .03;
        const edge = Math.abs(Math.abs(s - .20) - .045) < .012 || Math.abs(Math.abs(s - .48) - .05) < .012;
        return edge ? [K.dark, .55] : band ? [K.hi, 1.25] : [K.clown, 1];
      }
      case "bluetang": {
        if (s > .80) return [K.yellow, 1];
        if (s < .78 && Math.abs(vn + .15 - .55 * Math.sin(s * 3.3)) < .16) return [K.dark, .6];
        return [K.tang, 1];
      }
      case "yellowtang": return [K.yellow, 1];
      case "anthias": return [vn > .45 ? K.anthiasLow : K.anthias, 1];
      case "neon": return [vn > .15 ? K.neonRed : K.neon, 1];
      case "parrot": return [s > .55 ? K.parrotTail : K.parrot, 1];
    }
    return [0, 1];
  }

  /* ---------- scenes ---------- */
  // positions are fractions of the screen; sizes scale with its height (designed at 1080 px)
  const LIST = [
    { name: "whale shark", place: "ningaloo reef, australia", shark: 1, dolphins: 1, reef: 42, neons: 22, far: 26 },
    { name: "bottlenose dolphins", place: "the gulf stream, atlantic", shark: 0, dolphins: 2, reef: 30, neons: 22, far: 34 },
    { name: "reef school", place: "great barrier reef, australia", shark: 0, dolphins: 0, reef: 64, neons: 30, far: 30 },
  ];

  function make(index, C, R, cw, ch) {
    const sc = LIST[index % LIST.length], Wp = C * cw, Hp = R * ch, S = Math.min(Hp / 760, Wp / 1500);
    const rnd = rng(42 + index * 7), crs = [];
    const add = (o) => crs.push(Object.assign({ depth: 1, phase: 0, prof: PROF.fish }, o));
    // a far, faint school drifting across the upper water
    for (let i = 0; i < sc.far; i++) {
      const ox = (rnd() - .5) * 0.55, oy = (rnd() - .5) * 0.13, ph = rnd() * 6.28, sp = .9 + rnd() * .2, L = (34 + rnd() * 12) * S;
      add({ kind: "neon", L, H: L * .2, wave: [.25, 1.2, 3.2], depth: .38, phase: ph,
        path: (t) => [wrap(1.07 - .078 * t * sp + ox, -0.35, 1.35) * Wp + 18 * S * Math.sin(1.3 * t + ph), (.23 + oy) * Hp + 10 * S * Math.sin(.9 * t + ph)] });
    }
    if (sc.shark) {
      const L = 760 * S, span = [-L / Wp - .05, 1.08];
      add({ kind: "whaleshark", prof: PROF.whaleshark, L, H: 74 * S, wave: [.30, 1.0, .42],
        path: (t) => [wrap(.81 - .0615 * t, span[0], span[1]) * Wp, (.648 + .024 * Math.sin(.45 * t)) * Hp] });
    }
    for (let d = 0; d < sc.dolphins + (sc.dolphins ? 1 : 0); d++) {
      const lead = d === sc.dolphins, L = (lead ? 380 : 330 - d * 20) * S, lag = .15 * (d + 1) * (lead ? 0 : 1);
      const y0 = sc.shark ? .28 : .32 + d * .12, amp = (lead ? 95 : 80) * S;
      add({ kind: "dolphin", prof: PROF.dolphin, L, H: (lead ? 46 : 40) * S, wave: [.22, 1.0, 1.4], depth: lead ? 1 : .75, phase: d,
        path: (t) => [wrap(-.22 - lag + .128 * t, -.4, 1.35) * Wp, y0 * Hp + amp * Math.sin(2 * Math.PI * (t - .55 * d) / 3.6) + (lead ? 0 : 80 * S)] });
    }
    // the reef school: mixed species circling mid-screen
    const kinds = ["clown", "bluetang", "yellowtang", "anthias", "anthias", "parrot", "clown", "bluetang"];
    const SIZE = { clown: 62, bluetang: 78, yellowtang: 70, anthias: 56, parrot: 92 };
    const big = sc.reef > 50 ? 1.25 : 1;
    for (let i = 0; i < sc.reef; i++) {
      const kd = kinds[i % kinds.length], L = SIZE[kd] * (.85 + rnd() * .3) * S * big, tangish = kd === "bluetang" || kd === "yellowtang";
      const ox = (rnd() + rnd() + rnd() - 1.5) * 0.25 * (sc.reef > 50 ? 1.4 : 1), oy = (rnd() + rnd() + rnd() - 1.5) * 0.17, ph = rnd() * 6.28;
      add({ kind: kd, prof: tangish ? PROF.tang : PROF.fish, L, H: L * (tangish ? .30 : .26), wave: [.30, 1.1, 2.6 + (rnd() - .5) * .8], depth: .82 + rnd() * .18, phase: ph,
        path: (t) => {
          const cx = .5625 + .219 * Math.sin(.33 * t + .4), cy = (sc.shark ? .435 : .5) + .111 * Math.sin(.51 * t + 1.1), ang = .25 * Math.sin(.4 * t + ph);
          const dx = ox * Wp, dy = oy * Hp;
          return [cx * Wp + dx * Math.cos(ang) - dy * Math.sin(ang) + 22 * S * Math.sin(1.7 * t + ph), cy * Hp + dx * Math.sin(ang) + dy * Math.cos(ang) + 14 * S * Math.sin(1.3 * t + 2 * ph)];
        } });
    }
    // a quick neon school low on the left
    for (let i = 0; i < sc.neons; i++) {
      const ox = (rnd() + rnd() - 1) * 0.11, oy = (rnd() + rnd() - 1) * 0.07, ph = rnd() * 6.28;
      add({ kind: "neon", L: 40 * S, H: 9 * S, wave: [.3, 1.2, 3.6], depth: .95, phase: ph,
        path: (t) => [(.1875 + .156 * Math.sin(.6 * t + 2) + ox) * Wp + 10 * S * Math.sin(2.1 * t + ph), (.861 + .056 * Math.sin(.9 * t) + oy) * Hp + 8 * S * Math.sin(1.7 * t + ph)] });
    }
    // drifting particles, scaled to the screen
    const area = (C * R) / (240 * 77);
    const snow = Array.from({ length: Math.round(380 * area) }, () => [rnd() * Wp, rnd() * Hp, (4 + rnd() * 18) * S, rnd() * 6.28, .15 + rnd() * .4]);
    const bub = Array.from({ length: Math.round(70 * area) }, () => [rnd() * Wp, rnd() * Hp, (40 + rnd() * 80) * S, rnd() * 6.28, .4 + rnd() * .5]);
    return { C, R, cw, ch, Wp, Hp, S, crs, snow, bub, code: new Uint16Array(C * R).fill(32), col: new Uint8Array(C * R),
      dens: new Float32Array(C * R), dcol: new Uint8Array(C * R) };
  }

  function drawCreature(G, cr, t) {
    const { C, R, cw, ch, dens, dcol } = G;
    const p = cr.path(t), q = cr.path(t + 0.01);
    let dx = q[0] - p[0], dy = q[1] - p[1]; const n0 = Math.hypot(dx, dy) || 1; dx /= n0; dy /= n0;
    let nx = -dy, ny = dx; if (dx < 0) { nx = -nx; ny = -ny; }   // keep the dorsal side up
    const Rr = cr.L * 1.05 + cr.H * 2.2;
    const c0 = Math.max(0, Math.floor((p[0] - Rr) / cw)), c1 = Math.min(C, Math.floor((p[0] + Rr) / cw) + 1);
    const r0 = Math.max(0, Math.floor((p[1] - Rr) / ch)), r1 = Math.min(R, Math.floor((p[1] + Rr) / ch) + 1);
    if (c0 >= c1 || r0 >= r1) return;
    const [amp, k, f] = cr.wave, top = cr.prof.top, bot = cr.prof.bot, L = cr.L, H = cr.H;
    const isShark = cr.kind === "whaleshark", isDolphin = cr.kind === "dolphin";
    for (let r = r0; r < r1; r++) {
      const Y = (r + 0.5) * ch - p[1];
      for (let c = c0; c < c1; c++) {
        const Xx = (c + 0.5) * cw - p[0];
        const u = -(Xx * dx + Y * dy), s = u / L;
        if (s < -0.02 || s > 1.02) continue;
        const v = Xx * nx + Y * ny, sc = Math.min(1.2, Math.max(0, s));
        const vv = v - amp * H * sc * sc * Math.sin(2 * Math.PI * (k * s - f * t) + cr.phase);
        const tp = at(top, s) * H, bt = at(bot, s) * H;
        const inside = vv < 0 ? tp + vv : bt - vv;
        const cov = smooth(-3, 5, inside);
        if (cov <= 0.02) continue;
        const half = Math.max(1, vv < 0 ? tp : bt), vn = Math.max(-1, Math.min(1, vv / half));
        const core = 1 - vn * vn, rim = smooth(0, 6, inside) * (1 - smooth(6, 14, inside));
        let d = 0.30 + 0.62 * core + 0.25 * rim;
        let [ci, dm] = paint(cr.kind, s, vn, u, vv);
        d *= dm;
        if (isShark) {
          if (Math.abs(Math.abs(vn) - 0.45) < 0.05 && s > 0.15 && s < 0.8) d *= 1.15;                  // lateral ridges
          if ([.15, .17, .19, .21].some((g) => Math.abs(s - g) < .004) && Math.abs(vn) < .6) d = .25;  // gill slits
          if (Math.hypot(u - 0.05 * L, vv - 0.15 * H) < 6 * G.S + 2) { d = 1.3; ci = K.hi; }
        } else if (isDolphin) {
          if (Math.hypot(u - 0.13 * L, vv + 0.05 * H) < 5 * G.S + 2) d = 0.15;
        } else if (Math.hypot(u - 0.11 * L, vv + 0.2 * H) < Math.max(3.2 * G.S + 1.5, H * 0.22)) { d = 1.3; ci = K.hi; }
        const a = cov * cr.depth, i = r * C + c;
        dens[i] = dens[i] * (1 - a) + Math.min(1.3, d) * cr.depth * a;
        if (a > 0.35) dcol[i] = ci;
      }
    }
  }

  function step(G, t) {
    const { C, R, cw, ch, Wp, Hp, S, dens, dcol, code, col } = G;
    // light shafts sweep through the upper water
    for (let r = 0; r < R; r++) {
      const y = (r + 0.5) * ch, fall = Math.pow(Math.max(0, 1 - y / (Hp * 0.85)), 1.6);
      for (let c = 0; c < C; c++) {
        const i = r * C + c;
        if (fall <= 0) { dens[i] = 0; dcol[i] = K.shaft; continue; }
        const a = ((c + 0.5) * cw + 0.42 * y) / Wp;
        const rays = (Math.sin(a * 23 + .35 * t) * Math.sin(a * 9 - .2 * t + 1) + 1) / 2;
        dens[i] = Math.min(1, Math.pow(rays, 6) * fall * 0.32); dcol[i] = K.shaft;
      }
    }
    // marine snow and bubbles
    for (const q of G.snow) {
      const x = wrap(q[0] + 14 * S * Math.sin(.6 * t + q[3]) - 6 * S * t, 0, Wp), y = wrap(q[1] + q[2] * t, 0, Hp);
      const tw = q[4] * (.6 + .4 * Math.sin(3 * t + q[3] * 5)), i = Math.min(R - 1, (y / ch) | 0) * C + Math.min(C - 1, (x / cw) | 0);
      if (tw > dens[i]) { dens[i] = tw; dcol[i] = K.snow; }
    }
    for (const q of G.bub) {
      const x = wrap(q[0] + 18 * S * Math.sin(2.2 * t + q[3]), 0, Wp), y = wrap(q[1] - q[2] * t, 0, Hp);
      const i = Math.min(R - 1, (y / ch) | 0) * C + Math.min(C - 1, (x / cw) | 0);
      if (q[4] > dens[i]) { dens[i] = q[4]; dcol[i] = K.bubble; }
    }
    for (const cr of G.crs) drawCreature(G, cr, t);
    // density to glyph, with a flicker that reshuffles a third of the cells about 20 times a second
    const tick = Math.floor(t * 20);
    for (let i = 0; i < C * R; i++) {
      const d = dens[i];
      if (d < 0.035) { code[i] = 32; continue; }
      const h = hash(i, tick - (i % 3)), fl = (h + hash(i + 7, tick) - 1) * 3.2;
      const g = Math.max(1, Math.min(NG - 1, Math.round(Math.min(1, d) * (NG - 1) + fl)));
      code[i] = RC[g];
      col[i] = d > 1.05 ? K.hi : dcol[i];
    }
  }

  window.Ocean = { list: LIST.map((w) => ({ name: w.name, place: w.place })), palette: PALETTE, make, step };
})();
