/* nav.js: the top bar, shared by every page (home, results, paper).
 *
 * markup it expects, inside header.bar:
 *   nav#nav > ul.nav-sec  in-page anchors, <a href="#id">; the one in view gets aria-current="location"
 *   nav#nav > ul.nav-pg   pages, the page you are on carries aria-current="page" in the html
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
})();
