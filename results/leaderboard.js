/* Results page, the leaderboard. Every number is read from data/leaderboard.json, which
   results/build_leaderboard.py builds from the engine's real result files. Rows are grouped
   by harness, never ranked: the samples are small and the harnesses differ. No animation. */
(() => {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const body = $("#lb-body"), notes = $("#lb-notes");
  if (!body) return;

  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
  const p2 = (x) => x.toFixed(2), p3 = (x) => x.toFixed(3);
  const sgn = (x, f) => (x > 0 ? "+" : x < 0 ? "-" : "") + f(Math.abs(x));
  const usd = (x) => (x < 0 ? "-$" : x > 0 ? "+$" : "$") + Math.abs(x).toFixed(2);
  const GROUPS = [
    ["subagent", "Claude, as Claude Code subagents through the scruple episode CLI"],
    ["api", "Open models, through the raw Groq API (scruple eval run, zero-shot)"],
    ["reference", "Reference policies, in process"],
    ["baseline", "CFPB baselines, fitted on train, chosen on dev"],
  ];
  const HARNESS = { subagent: "subagent", api: "API", reference: "reference", baseline: "baseline" };

  // a cell: the value on top, the interval under it in mute
  function cell(main, sub, cls) {
    const td = el("td", "lb-c" + (cls ? " " + cls : ""));
    td.append(el("b", null, main));
    for (const x of [].concat(sub || [])) td.append(el("span", null, x));
    return td;
  }
  const none = (why) => { const td = el("td", "lb-c lb-none"); td.append(el("span", null, why)); return td; };
  const kn = (c, bad) => cell(`${c.k}/${c.n}`, `[${p2(c.ci95[0])}, ${p2(c.ci95[1])}]`, bad ? (c.k > 0 ? "no" : "") : "");
  function cfpb(c, r) {
    if (!c) return none(r.harness === "reference" ? "" : "not run");
    const sub = c.vs_tfidf ? [sgn(c.vs_tfidf.diff, p3), `[${sgn(c.vs_tfidf.ci95[0], p3)}, ${sgn(c.vs_tfidf.ci95[1], p3)}]`] : null;
    const valid = c.valid != null && c.valid < c.n ? [`${c.valid}/${c.n} valid replies`] : [];
    return cell(p3(c.value), [...valid, ...(sub || [])]);
  }

  function render(d) {
    const marks = [];
    for (const [g, label] of GROUPS) {
      const rows = d.rows.filter((r) => r.harness === g);
      if (!rows.length) continue;
      const head = el("tr", "lb-group"), th = el("th", null, label);
      th.colSpan = 11; th.scope = "colgroup"; head.append(th); body.append(head);
      for (const r of rows) {
        const tr = el("tr");
        const name = el("th"); name.scope = "row";
        name.append(el("b", null, r.name));
        if (r.harness === "subagent" || r.harness === "api") name.append(el("span", null, r.model));
        if (r.partial.length || r.notes.length) {
          marks.push(r);
          const a = el("a", "lb-mark", `[${marks.length}]`); a.href = `#lb-n${marks.length}`;
          a.setAttribute("aria-label", `Note ${marks.length}${r.partial.length ? ", partial run" : ""}`);
          name.querySelector("b").append(" ", a);
          if (r.partial.length) name.append(el("i", "warn", "partial run"));
        }
        name.prepend(el("i", "lb-h", HARNESS[r.harness]));
        tr.append(name);
        const e = r.episodes;
        if (e) {
          tr.append(
            cell(String(e.n), e.seeds),
            kn(e.gate_pass), kn(e.fair_outcome), kn(e.sr01_fail, true),
            e.cost_vs_evidence ? cell(usd(e.cost_vs_evidence.diff), `[${usd(e.cost_vs_evidence.ci95[0])}, ${usd(e.cost_vs_evidence.ci95[1])}]`) : cell("ref", `mean $${e.mean_cost_usd.toFixed(2)}`),
          );
        } else for (let i = 0; i < 5; i++) tr.append(none(i ? "" : "no episodes"));
        tr.append(...["regime-shared", "regime-2015", "outcome-2015", "routing-2025"].map((t) => cfpb(r.cfpb[t], r)));
        if (r.e4) {
          const h = r.e4.honest.gate_pass, i = r.e4.incentive.gate_pass;
          tr.append(cell(`${h.k}/${h.n} to ${i.k}/${i.n}`, `SR-01 ${r.e4.honest.sr01_fail.k} to ${r.e4.incentive.sr01_fail.k}`));
        } else tr.append(none(r.harness === "api" ? "not run" : ""));
        body.append(tr);
      }
    }
    marks.forEach((r, i) => {
      const li = el("li"); li.id = `lb-n${i + 1}`;
      const fin = (x) => (/[.)]$/.test(x) ? x : x + ".");
      const text = [r.partial.length ? "Partial. " + r.partial.map((x) => fin(x[0].toUpperCase() + x.slice(1))).join(" ") : "", ...r.notes.map(fin)].filter(Boolean).join(" ");
      li.append(el("b", null, `[${i + 1}] ${r.name}`), el("span", null, text));
      notes.append(li);
    });
    const h1 = d.h1_sr01_open_minus_claude.complete_open_models, e4 = d.e4;
    const li = el("li");
    li.append(el("b", null, "Pooled"), el("span", null,
      `Self-report failure rate, the two complete open models minus Claude, paired over ${h1.seeds.length} seeds: ${sgn(h1.diff, p3)} [${sgn(h1.ci95[0], p3)}, ${sgn(h1.ci95[1], p3)}]. Under incentive (E4, seeds ${e4.seeds}), ${e4.h3.surfaced} of ${e4.h3.bad_episodes} bad episodes surfaced on the scorecard; the sign test on contesting valid claims found ${e4.h1.positive} of ${e4.h1.nonzero} non-zero pairs positive, p = ${e4.h1.p_one_sided.toFixed(2)}.`));
    notes.append(li);
  }

  fetch(new URL("data/leaderboard.json", document.baseURI))
    .then((r) => { if (!r.ok) throw new Error(`leaderboard.json: ${r.status}`); return r.json(); })
    .then(render)
    .catch((err) => { const tr = el("tr"), td = el("td", null, "Could not load the leaderboard data. Serve the site over http (python3 -m http.server)."); td.colSpan = 11; tr.append(td); body.append(tr); console.error(err); });
})();
