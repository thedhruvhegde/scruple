// scruple console: the one script. Applies the saved colour theme before first paint
// (loaded synchronously in <head>) and wires the LIGHT / DARK toggle. Nothing else:
// every page works without it.
(function () {
  "use strict";
  var root = document.documentElement;
  var KEY = "scruple-theme";
  function saved() {
    try { return localStorage.getItem(KEY); } catch (e) { return null; }
  }
  function apply(t) {
    if (t === "light" || t === "dark") root.setAttribute("data-theme", t);
    else root.removeAttribute("data-theme");
  }
  function current() {
    var t = root.getAttribute("data-theme");
    if (t === "light" || t === "dark") return t;
    return window.matchMedia && matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  apply(saved());
  function wire() {
    var buttons = document.querySelectorAll("[data-theme-set]");
    function mark() {
      var now = current();
      for (var i = 0; i < buttons.length; i++) {
        buttons[i].setAttribute("aria-pressed", String(buttons[i].getAttribute("data-theme-set") === now));
      }
    }
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].addEventListener("click", function (ev) {
        var t = ev.currentTarget.getAttribute("data-theme-set");
        apply(t);
        try { localStorage.setItem(KEY, t); } catch (e) { /* private mode: not saved */ }
        mark();
      });
    }
    mark();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", wire);
  else wire();
})();
