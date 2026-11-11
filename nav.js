/* nav.js: the top bar, shared by every page (home, results, paper, pilot, 404).
 *
 * markup it expects, inside header.bar:
 *   span.crumb[data-page]  under 1000px: "/ results / <> watch", the page then the section in view
 *   nav#nav > ul.nav-sec   in-page anchors, <a href="#id" data-g="<>" aria-label="Watch a case">Watch</a>:
 *                          a two-character glyph, a short name, the full title as the label;
 *                          the one in view gets aria-current="location"
 *   nav#nav > ul.nav-pg    pages, the page you are on carries aria-current="page" in the html
 *   button#nav-menu        [ MENU ], shown under 1000px; opens nav#nav as a small terminal list
 *   span.bar-rule > i, b   the bottom edge: i is the ====> fill, b the keys hint and percent read
 *   a.skip                 first in the body, href="#top" pointing at main; shown only on focus
 *
 * keys 1-9 jump to the sections, unless you are typing or holding a modifier.
 * to add it to a page: link site.css (tokens and bar css), copy the bar markup, then
 *   <script src="nav.js" defer></script>   (or ../nav.js one level down)
 *
 * it only reads layout on scroll and flips attributes or text, so the state changes instantly, no fades.
 */
(() => {
  "use strict";
  const bar = document.querySelector(".bar"), nav = document.getElementById("nav");
  if (!bar || !nav) return;

  const links = [...nav.querySelectorAll('.nav-sec a[href^="#"]')];
  const targets = links.map((a) => document.getElementById(decodeURIComponent(a.getAttribute("href").slice(1))));
  const crumb = bar.querySelector(".crumb"), rule = bar.querySelector(".bar-rule");
  const fill = rule && rule.querySelector("i"), info = rule && rule.querySelector("b");
  const page = crumb ? crumb.dataset.page || "" : "";
  const fine = matchMedia("(hover: hover) and (pointer: fine)"), wide = matchMedia("(min-width: 1001px)");
  links.forEach((a, i) => { if (i < 9) a.setAttribute("aria-keyshortcuts", String(i + 1)); });

  /* ---------- the section in view, the crumb and the reading ruler ---------- */
  let cur = null, queued = false, cw = 0, lastFill = -1, lastInfo = "";
  function measure() {
    if (!rule) return;
    const p = document.createElement("span");
    p.textContent = "0".repeat(40); p.style.cssText = "position:absolute;visibility:hidden";
    rule.appendChild(p); cw = p.getBoundingClientRect().width / 40; p.remove();
    lastFill = -1; lastInfo = "";
  }
  function setCrumb() {
    if (!crumb) return;
    // two parts, so a phone can drop the page name and keep the section: <i>/ results</i><b> / <> watch</b>
    const sec = cur ? `${cur.dataset.g ? cur.dataset.g + " " : ""}${cur.textContent.trim().toLowerCase()}` : "";
    const a = document.createElement("i"), b = document.createElement("b");
    a.textContent = page ? "/ " + page : sec ? "" : "/ home"; b.textContent = sec ? (page ? " / " : "/ ") + sec : "";
    crumb.replaceChildren(a, b); crumb.classList.toggle("in-sec", !!sec);
  }
  function mark() {
    queued = false;
    // the reading line sits a third of the way down what the bar leaves visible
    const top = bar.getBoundingClientRect().bottom, line = top + (innerHeight - top) * 0.34;
    let hit = null;
    targets.forEach((t, i) => { if (t) { const r = t.getBoundingClientRect(); if (r.top <= line && r.bottom > line) hit = links[i]; } });
    if (hit !== cur) {
      if (cur) cur.removeAttribute("aria-current");
      if (hit) hit.setAttribute("aria-current", "location");
      cur = hit; setCrumb();
    }
    if (!fill || !cw) return;
    const max = document.documentElement.scrollHeight - innerHeight, p = max > 0 ? Math.min(1, Math.max(0, scrollY / max)) : 0;
    const keys = links.length && fine.matches && wide.matches ? `keys 1-${Math.min(9, links.length)}   ` : "";
    const txt = max > 0 ? `${keys}${String(Math.round(p * 100)).padStart(3, "0")}%` : "";
    if (txt !== lastInfo && info) { info.textContent = txt; lastInfo = txt; }
    // whole characters only, so the ====> lands on the ruler's own grid and stops short of the label
    const room = Math.floor(rule.clientWidth / cw) - (txt ? txt.length + 2 : 0);
    const n = Math.max(0, Math.round(p * room));
    if (n !== lastFill) { fill.style.width = (n * cw).toFixed(2) + "px"; lastFill = n; }
  }
  const queue = () => { if (!queued) { queued = true; requestAnimationFrame(mark); } };
  addEventListener("scroll", queue, { passive: true });
  addEventListener("resize", () => { measure(); queue(); });
  addEventListener("hashchange", queue);
  measure(); setCrumb(); mark();
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(() => { measure(); mark(); });

  /* ---------- skip link: move focus into main, not just the scroll ---------- */
  const skip = document.querySelector("a.skip");
  if (skip) skip.addEventListener("click", () => {
    const m = document.getElementById(decodeURIComponent(skip.hash.slice(1)));
    if (m) { m.tabIndex = -1; m.focus({ preventScroll: true }); }
  });

  /* ---------- keys: 1-9 jump to a section (the page's own click handler lands it) ---------- */
  addEventListener("keydown", (e) => {
    if (e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey || e.shiftKey || e.isComposing) return;
    const n = e.key >= "1" && e.key <= "9" ? +e.key : 0, el = e.target;
    if (!n || n > links.length) return;
    if (el && el.closest && el.closest('input, textarea, select, [contenteditable=""], [contenteditable="true"]')) return;
    e.preventDefault(); links[n - 1].click();
  });

  /* ---------- the narrow-screen menu ---------- */
  const btn = document.getElementById("nav-menu");
  if (!btn) return;
  const isOpen = () => btn.getAttribute("aria-expanded") === "true";
  function set(open) { btn.setAttribute("aria-expanded", String(open)); bar.classList.toggle("open", open); }
  btn.addEventListener("click", () => { set(!isOpen()); if (isOpen()) { const f = nav.querySelector("a"); if (f) f.focus({ preventScroll: true }); } });
  nav.addEventListener("click", (e) => { if (e.target.closest("a")) set(false); });
  addEventListener("keydown", (e) => { if (e.key === "Escape" && isOpen()) { set(false); btn.focus(); } });
  addEventListener("pointerdown", (e) => { if (isOpen() && !bar.contains(e.target)) set(false); });
  matchMedia("(min-width: 1001px)").addEventListener("change", (m) => { if (m.matches) set(false); });
})();
