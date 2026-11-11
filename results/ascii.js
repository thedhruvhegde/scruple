/* Results page, the replay drawn in characters. results.js fires a "replay" event after
   every step; this file only draws from it. Nothing here runs on a loop: each drawing
   happens once per step, or once per resize when the column count changes. */
(() => {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const NAME = { haiku: "Haiku", sonnet: "Sonnet", opus: "Opus" };

  // how many characters fit across a pre, measured in its own font
  function cols(pre) {
    const s = document.createElement("span");
    s.textContent = "0".repeat(20);
    s.style.whiteSpace = "pre";
    pre.append(s);
    const w = s.getBoundingClientRect().width / 20;
    s.remove();
    const cs = getComputedStyle(pre);
    return Math.max(24, Math.floor((pre.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight)) / w));
  }
  // a line is a list of [text, ink]; ink is a c-* class, "B" + ink sets it bold
  function draw(pre, lines) {
    pre.textContent = "";
    for (const l of lines) {
      for (const [t, ink] of l) {
        const n = document.createElement(ink && ink[0] === "B" ? "b" : "span");
        n.className = "c-" + (ink ? ink.replace(/^B/, "") : "d");
        n.textContent = t;
        pre.append(n);
      }
      pre.append("\n");
    }
  }
  const len = (segs) => segs.reduce((a, [t]) => a + t.length, 0);

  /* ---------- case file: a folder tab, the case, two stamps ---------- */
  function caseFile(ep) {
    const pre = $("#casefile"), W = cols(pre), c = ep.case;
    const tab = `CASE ${ep.seed}`;
    const SYN = [["[ SYNTHETIC CASE ]", "Bb"]], REAL = [["[ REAL CALLS ]", "Bg"]];
    const rows = [
      [[[NAME[ep.model], "f"], [`  ${c.network} ${c.reason_code}  `, "d"], [c.amount, "f"]], SYN],
      [[[c.reason_title, "d"]], REAL],
      [[[`opened ${c.opened_on}, ${c.opening_stage}, ${ep.steps.length} calls`, "d"]], []],
    ];
    const inner = W - 4, out = [];
    out.push([[" " + "_".repeat(tab.length + 2), "d"]]);
    const t = "/ " + tab + " \\";
    out.push([["/ ", "d"], [tab, "Bf"], [" \\" + "_".repeat(Math.max(0, W - t.length)), "d"]]);
    const row = (segs, right) => {
      const gap = inner - len(segs) - len(right);
      if (gap < 2 && right.length) { row(segs, []); row([], right); return; }
      // text wider than the box is cut, never wrapped
      let room = inner - len(right) - (right.length ? 2 : 0);
      const cut = [];
      for (const [s, ink] of segs) { const x = s.slice(0, Math.max(0, room)); room -= x.length; if (x) cut.push([x, ink]); }
      out.push([["| ", "d"], ...cut, [" ".repeat(Math.max(0, inner - len(cut) - len(right))), "d"], ...right, [" |", "d"]]);
    };
    for (const [l, r] of rows) row(l, r);
    out.push([["|" + "_".repeat(W - 2) + "|", "d"]]);
    draw(pre, out);
  }

  /* ---------- ledger paper: ruled columns behind the trace ---------- */
  function rules() {
    const pre = $("#rules"), tr = $("#trace-pre"), W = cols(tr);
    const lh = parseFloat(getComputedStyle(tr).lineHeight) || 20, n = Math.ceil(tr.offsetHeight / lh);
    // call number | day | the call ............ | and a closing rule at the right edge
    const line = (i) => {
      const a = Array(W).fill(" ");
      a[2] = a[13] = ":"; a[W - 1] = "|";
      if (i % 5 === 4) for (let x = 14; x < W - 1; x += 4) a[x] = ".";
      return a.join("");
    };
    pre.style.height = tr.offsetHeight + "px";
    pre.textContent = Array.from({ length: n }, (_, i) => line(i)).join("\n");
  }

  /* ---------- timeline: the stages a dispute can pass through ---------- */
  const STAGES = ["inquiry", "chargeback", "representment", "pre_arbitration", "arbitration", "resolved"];
  const LONG = ["inquiry", "chargeback", "representment", "pre-arb", "arbitration", "resolved"];
  const SHORT = ["inq", "chgbk", "repr", "pre-arb", "arb", "closed"];
  function timeline(ep, pos) {
    const pre = $("#timeline"), W = cols(pre), N = ep.steps.length;
    const now = pos < N ? ep.steps[pos].stage : ep.result.path[ep.result.path.length - 1];
    const seen = new Set([ep.case.opening_stage, now, ...ep.steps.slice(0, pos + 1).map((s) => s.stage)]);
    let names = LONG, sep = " -- ";
    const width = (n, s) => n.reduce((a, x) => a + x.length + 2, 0) + s.length * (n.length - 1);
    if (width(names, sep) > W) sep = "-";
    if (width(names, sep) > W) { names = SHORT; sep = " - "; }
    if (width(names, sep) > W) sep = "-";
    const top = [], at = [0];
    STAGES.forEach((st, i) => {
      if (i) top.push([sep, seen.has(st) && seen.has(STAGES[i - 1]) ? "g" : "d"]);
      const x = len(top);
      if (st === now) { top.push([" " + names[i] + " ", "Bnow"]); at[0] = x; }
      else top.push([" " + names[i] + " ", seen.has(st) ? "g" : "d"]);
    });
    const note = pos >= N ? "closed, scored" : pos ? `after call ${pos} of ${N}, ${ep.steps[pos - 1].day}` : `opened ${ep.case.opened_on}`;
    const cx = Math.max(0, Math.min(at[0] + 1, W - note.length - 2));
    draw(pre, [top, [[" ".repeat(cx) + "^ ", "f"], [note, "d"]]]);
  }

  /* ---------- stamp: BLOCKED, set in a box of characters ---------- */
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  let stampKey = "", landT = 0;
  function stamp(ep, pos, notes) {
    const box = $("#stamp"), N = ep.steps.length, ids = [];
    for (const [i, list] of notes) if (i < pos || pos >= N) for (const [c] of list) if (c.hard && !ids.includes(c.id)) ids.push(c.id);
    const key = ids.length ? ep.episode_id + ids.join() : "";
    if (!ids.length) { box.hidden = true; stampKey = ""; return; }
    const lines = ["BLOCKED", ids.join(" ")];
    const w = Math.max(...lines.map((l) => l.length)) + 4;
    const pad = (t) => { const l = Math.floor((w - t.length) / 2); return " ".repeat(l) + t + " ".repeat(w - t.length - l); };
    box.textContent = ["#" + "=".repeat(w) + "#", ...lines.map((l) => "#" + pad(l) + "#"), "#" + "=".repeat(w) + "#"].join("\n");
    box.style.top = $("#trace").offsetTop + 14 + "px";
    box.hidden = false;
    if (key !== stampKey && !reduce) {
      box.classList.add("land");
      clearTimeout(landT);
      landT = setTimeout(() => box.classList.remove("land"), 90);
    }
    stampKey = key;
  }

  let last = null, lastW = 0;
  function render(d) {
    last = d;
    caseFile(d.ep);
    rules();
    timeline(d.ep, d.pos);
    stamp(d.ep, d.pos, d.notes);
    lastW = cols($("#casefile"));
  }
  document.addEventListener("replay", (e) => render(e.detail));
  let queued = false;
  addEventListener("resize", () => {
    if (queued || !last) return;
    queued = true;
    requestAnimationFrame(() => { queued = false; if (cols($("#casefile")) !== lastW) render(last); });
  });
  if (document.fonts) document.fonts.ready.then(() => { if (last) render(last); });
})();
