// Theme: follows the OS setting until the visitor picks one. The choice is kept
// in localStorage (not a cookie) and never leaves the browser.
(() => {
  const KEY = "theme";
  const html = document.documentElement;
  const media = window.matchMedia("(prefers-color-scheme: dark)");

  const stored = () => { try { return localStorage.getItem(KEY); } catch { return null; } };
  const isDark = () => (stored() ? stored() === "dark" : media.matches);

  function apply() {
    html.classList.toggle("dark", isDark());
    const btn = document.getElementById("theme-toggle");
    if (btn) btn.setAttribute("aria-label", isDark() ? "Switch to light theme" : "Switch to dark theme");
  }

  apply();
  media.addEventListener("change", () => { if (!stored()) apply(); });

  document.addEventListener("DOMContentLoaded", () => {
    apply();
    document.getElementById("theme-toggle")?.addEventListener("click", () => {
      try { localStorage.setItem(KEY, isDark() ? "light" : "dark"); } catch { html.classList.toggle("dark"); return; }
      apply();
    });
  });
})();
