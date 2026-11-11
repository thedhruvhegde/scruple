/* nav.js: the top bar, shared by every page (home, results, paper).
 *
 * markup it expects, inside header.bar:
 *   nav#nav > ul.nav-sec  in-page anchors, <a href="#id">; the one in view gets aria-current="location"
 *   nav#nav > ul.nav-pg   pages, the page you are on carries aria-current="page" in the html
 *   button#nav-menu       [ MENU ], shown under 1100px; opens nav#nav as a panel over the page
 *
 * to add it to a page: copy the bar markup and the bar css block, then
 *   <script src="nav.js" defer></script>   (or ../nav.js one level down)
 *
 * it only reads layout on scroll and flips attributes, so the state changes instantly, no fades.
 */
(() => {
  "use strict";
  const bar = document.querySelector(".bar"), nav = document.getElementById("nav");
  if (!bar || !nav) return;

  /* ---------- the section in view ---------- */
  const links = [...nav.querySelectorAll('.nav-sec a[href^="#"]')];
  const targets = links.map((a) => document.getElementById(decodeURIComponent(a.getAttribute("href").slice(1))));
  let cur = null, queued = false;
  function mark() {
    queued = false;
    // the reading line sits a third of the way down what the bar leaves visible
    const top = bar.getBoundingClientRect().bottom, line = top + (innerHeight - top) * 0.34;
    let hit = null;
    targets.forEach((t, i) => { if (t) { const r = t.getBoundingClientRect(); if (r.top <= line && r.bottom > line) hit = links[i]; } });
    if (hit === cur) return;
    if (cur) cur.removeAttribute("aria-current");
    if (hit) hit.setAttribute("aria-current", "location");
    cur = hit;
  }
  const queue = () => { if (!queued) { queued = true; requestAnimationFrame(mark); } };
  addEventListener("scroll", queue, { passive: true });
  addEventListener("resize", queue);
  addEventListener("hashchange", queue);
  mark();

  /* ---------- the narrow-screen menu ---------- */
  const btn = document.getElementById("nav-menu");
  if (!btn) return;
  const isOpen = () => btn.getAttribute("aria-expanded") === "true";
  function set(open) { btn.setAttribute("aria-expanded", String(open)); bar.classList.toggle("open", open); }
  btn.addEventListener("click", () => { set(!isOpen()); if (isOpen()) { const f = nav.querySelector("a"); if (f) f.focus({ preventScroll: true }); } });
  nav.addEventListener("click", (e) => { if (e.target.closest("a")) set(false); });
  addEventListener("keydown", (e) => { if (e.key === "Escape" && isOpen()) { set(false); btn.focus(); } });
  addEventListener("pointerdown", (e) => { if (isOpen() && !bar.contains(e.target)) set(false); });
  matchMedia("(min-width: 1101px)").addEventListener("change", (m) => { if (m.matches) set(false); });
})();
