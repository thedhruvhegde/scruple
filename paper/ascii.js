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

  let lastW = -1, queued = false;
  function draw() { queued = false; const w = document.documentElement.clientWidth; if (w === lastW) return; lastW = w; frames(); }
  addEventListener("resize", () => { if (!queued) { queued = true; requestAnimationFrame(draw); } });
  draw();
  if (document.fonts) document.fonts.ready.then(() => { lastW = -1; draw(); });
})();
