/* Paper page, drawn in characters. Static: everything is drawn once, and again only
   when a resize changes how many characters fit. */
(() => {
  "use strict";
  const chw = (host) => {
    const s = document.createElement("span");
    s.textContent = "0".repeat(20);
    s.style.cssText = "position:absolute;visibility:hidden;white-space:pre";
    host.append(s);
    const w = s.getBoundingClientRect().width / 20;
    s.remove();
    return w;
  };

  /* ---------- theorem boxes ---------- */
  const list = document.querySelector(".thm");
  const boxes = [];
  if (list) {
    list.classList.add("boxed");
    for (const li of list.children) {
      const f = document.createElement("pre"), g = document.createElement("pre");
      f.className = g.className = "frame";
      f.setAttribute("aria-hidden", "true"); g.setAttribute("aria-hidden", "true");
      li.prepend(f, g);
      boxes.push([li, f, g]);
    }
  }
  function frames() {
    for (const [li, f, g] of boxes) {
      const w = chw(f), W = Math.floor(li.clientWidth / w), R = Math.ceil(li.clientHeight / 16);
      const side = "|" + " ".repeat(W - 2) + "|", edge = "+" + "-".repeat(W - 2) + "+";
      f.textContent = [edge, ...Array(Math.max(0, R - 1)).fill(side)].join("\n");
      g.textContent = edge;
    }
  }

  /* ---------- Theorem 10.3 in characters, from the figure's own svg ---------- */
  const out = document.getElementById("bound-chars");
  const svg = document.querySelector('.screen[data-head="hacking gap"] svg.wide');
  function bound() {
    if (!out || !svg) return;
    const num = (n, a) => parseFloat(n.getAttribute(a));
    const grid = [...svg.querySelectorAll("line.l-grid")];
    const hz = grid.filter((l) => num(l, "y1") === num(l, "y2")), vt = grid.filter((l) => num(l, "x1") === num(l, "x2"));
    const x0 = Math.min(...vt.map((l) => num(l, "x1"))), x1 = Math.max(...vt.map((l) => num(l, "x1")));
    const y0 = num(vt[0], "y1"), y1 = num(vt[0], "y2");
    const texts = [...svg.querySelectorAll("text.t-d")];
    const near = (x, y) => texts.find((t) => Math.abs(num(t, "x") - x) < 12 && Math.abs(num(t, "y") - y) < 12);
    const LW = 6, W = Math.floor((out.clientWidth - 36) / chw(out)), P = Math.max(30, Math.min(96, W - LW - 2)), H = 15;
    const cx = (x) => Math.round((x - x0) / (x1 - x0) * (P - 1)), ry = (y) => (y - y0) / (y1 - y0) * (H - 1);
    const cells = Array.from({ length: H }, () => Array.from({ length: P }, () => [" ", "r"]));
    const tick = new Map();
    for (const l of hz) { const r = Math.round(ry(num(l, "y1"))), t = near(x0 - 8, num(l, "y1") + 4); if (r >= 0 && r < H) { tick.set(r, t ? t.textContent : ""); for (let c = 0; c < P; c += 2) cells[r][c] = [".", "r"]; } }
    for (const [cls, ink] of [["l-k-pine", "g"], ["l-k-sky", "s"], ["l-k-verm", "v"]]) {
      const pl = svg.querySelector("polyline." + cls);
      if (!pl) continue;
      const pts = pl.getAttribute("points").trim().split(/\s+/).map((p) => p.split(",").map(Number));
      for (let c = 0; c < P; c++) {
        const x = x0 + c / (P - 1) * (x1 - x0);
        let i = pts.findIndex((p) => p[0] >= x);
        if (i < 0) break;
        if (i === 0) i = 1;
        const [ax, ay] = pts[i - 1], [bx, by] = pts[i], y = ay + (by - ay) * ((x - ax) / (bx - ax || 1));
        const f = ry(y), r = Math.floor(f + 0.5), sub = f + 0.5 - r;
        if (r < 0 || r >= H) continue;
        cells[r][c] = [sub < 0.34 ? "'" : sub < 0.67 ? "-" : ".", ink];
      }
    }
    for (const [cls, ink] of [["k-pine", "g"], ["k-sky", "s"], ["k-verm", "v"]]) {
      for (const rc of svg.querySelectorAll("rect." + cls)) {
        const r = Math.round(ry(num(rc, "y") + 4)), c = cx(num(rc, "x") + 4);
        if (r >= 0 && r < H && c >= 0 && c < P) cells[r][c] = ["#", ink];
      }
    }
    for (const t of svg.querySelectorAll("text.t-f")) {
      const r = Math.round(ry(num(t, "y") - 4)), c = cx(num(t, "x")) + 1, s = t.textContent;
      if (r >= 0 && r < H && c + s.length <= P) for (let k = 0; k < s.length; k++) cells[r][c + k] = [s[k], "f"];
    }
    out.textContent = "";
    const put = (t, ink) => { const n = document.createElement("span"); n.className = "c-" + ink; n.textContent = t; out.append(n); };
    const head = svg.querySelector("text.t-h");
    if (head) { put(head.textContent.slice(0, LW + 2 + P), "f"); out.append("\n\n"); }
    cells.forEach((row, r) => {
      put((tick.get(r) || "").padStart(LW) + (tick.has(r) ? " +" : " |"), "d");
      let run = "", ink = null;
      for (const [ch, k] of row) { if (k !== ink && run) { put(run, ink); run = ""; } ink = k; run += ch; }
      put(run, ink);
      out.append("\n");
    });
    put(" ".repeat(LW) + " +" + "-".repeat(P), "d");
    out.append("\n");
    const lab = Array(P + LW + 2).fill(" ");
    for (const l of vt) {
      const t = near(num(l, "x1"), y1 + 18);
      if (!t) continue;
      const s = t.textContent, at = Math.min(LW + 2 + P - s.length, Math.max(0, LW + 2 + cx(num(l, "x1")) - Math.floor(s.length / 2)));
      if (lab.slice(at - 1, at + s.length + 1).every((ch) => ch === " ")) for (let k = 0; k < s.length; k++) lab[at + k] = s[k];
    }
    put(lab.join("").trimEnd(), "d");
  }

  let lastW = -1, queued = false;
  function draw() { queued = false; const w = document.documentElement.clientWidth; if (w === lastW) return; lastW = w; frames(); bound(); }
  addEventListener("resize", () => { if (!queued) { queued = true; requestAnimationFrame(draw); } });
  draw();
  if (document.fonts) document.fonts.ready.then(() => { lastW = -1; draw(); });
})();
