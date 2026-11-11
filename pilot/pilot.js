/* Pilot page: the contact form. GitHub Pages is static, so nothing is posted: the form
   checks its fields, then writes a mailto: with a structured subject and body and hands
   it to the visitor's mail app. The composed text is also shown, with a link and a copy
   button, for when no mail app opens. No third-party scripts, nothing stored. */
(() => {
  "use strict";
  const TO = "dhegde@umich.edu";
  const $ = (s) => document.querySelector(s);
  const form = $("#pilot-form");
  if (!form) return;

  const FIELDS = [
    { id: "f-name", label: "Name", need: true },
    { id: "f-company", label: "Company", need: true },
    { id: "f-role", label: "Role", need: true },
    { id: "f-volume", label: "Disputes a month", need: true, check: (v) => (/^\d{1,9}$/.test(v.replace(/[,_\s]/g, "")) ? "" : "Enter a whole number, like 1200.") },
    { id: "f-tool", label: "How disputes are decided today", need: true, pick: true },
    { id: "f-stack", label: "Current tooling", need: false },
    { id: "f-change", label: "The change you would test", need: false },
  ];
  const val = (f) => $("#" + f.id).value.trim();

  function validate() {
    const bad = [];
    for (const f of FIELDS) {
      const el = $("#" + f.id), v = val(f), err = $("#" + f.id + "-err");
      let msg = "";
      if (f.need && !v) msg = f.pick ? "Choose one." : "Required.";
      else if (v && f.check) msg = f.check(v);
      else if (v.length > (+el.getAttribute("maxlength") || 1e4)) msg = "Too long.";
      err.textContent = msg;
      if (msg) { el.setAttribute("aria-invalid", "true"); bad.push([f, msg]); } else el.removeAttribute("aria-invalid");
    }
    return bad;
  }

  function compose() {
    const v = Object.fromEntries(FIELDS.map((f) => [f.id, val(f)]));
    // a multi-line answer starts on its own line, indented, so each label stays at the margin
    const block = (t) => (!t ? "-" : t.includes("\n") ? "\n  " + t.split(/\r?\n/).join("\n  ") : t);
    const vol = Number(v["f-volume"].replace(/[,_\s]/g, ""));
    const subject = `Scruple pilot: ${v["f-company"]}`;
    const lines = [
      "Scruple pilot enquiry",
      "",
      `Name: ${v["f-name"]}`,
      `Company: ${v["f-company"]}`,
      `Role: ${v["f-role"]}`,
      `Monthly dispute volume: ${vol.toLocaleString("en-US")}`,
      `Decided today by: ${v["f-tool"]}`,
      `Current tooling: ${block(v["f-stack"])}`,
      `Change to test: ${block(v["f-change"])}`,
      "",
      "Sent from the form at thedhruvhegde.github.io/scruple/pilot/",
    ];
    const body = lines.join("\n");
    // RFC 6068: percent-encode everything, line breaks as CRLF
    const href = `mailto:${TO}?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(body.replace(/\n/g, "\r\n"))}`;
    return { subject, body, href };
  }

  // clear a field's error as soon as it is fixed, never add one while typing
  function recheck(e) {
    const f = FIELDS.find((x) => x.id === e.target.id);
    if (!f || e.target.getAttribute("aria-invalid") !== "true") return;
    const v = val(f);
    if ((!f.need || v) && (!v || !f.check || !f.check(v))) { e.target.removeAttribute("aria-invalid"); $("#" + f.id + "-err").textContent = ""; }
  }
  form.addEventListener("input", recheck);
  form.addEventListener("change", recheck);

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const bad = validate(), box = $("#problems");
    if (bad.length) {
      const ul = document.createElement("ul");
      for (const [f, msg] of bad) {
        const a = document.createElement("a");
        a.href = "#" + f.id; a.textContent = `${f.label}: ${msg}`;
        a.addEventListener("click", (ev) => { ev.preventDefault(); $("#" + f.id).focus(); });
        const li = document.createElement("li"); li.append(a); ul.append(li);
      }
      const b = document.createElement("b");
      b.textContent = bad.length === 1 ? "1 field to fix" : `${bad.length} fields to fix`;
      box.replaceChildren(b, ul); box.hidden = false; box.focus();
      return;
    }
    box.hidden = true; box.replaceChildren();
    const m = compose();
    $("#mail-pre").textContent = `To: ${TO}\nSubject: ${m.subject}\n\n${m.body}`;
    $("#mail-again").href = m.href;
    $("#copy-note").textContent = "";
    $("#sent").hidden = false;
    location.href = m.href;
  });

  $("#mail-copy").addEventListener("click", async () => {
    const note = $("#copy-note");
    try { await navigator.clipboard.writeText($("#mail-pre").textContent); note.textContent = "Copied."; }
    catch { note.textContent = "Copy did not work here. Select the text above."; }
  });
})();
