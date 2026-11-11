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

  let last = null, lastW = 0;
  function render(d) {
    last = d;
    caseFile(d.ep);
    rules();
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
