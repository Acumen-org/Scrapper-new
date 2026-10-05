/* Bellwether: behaviour shared by every page. No framework, no build step.
 *
 *   - Ctrl K or / opens the finder: firms by name or CRD, people, and pages.
 *   - Clicking anywhere on a row marked tr.go opens what the row is about.
 *   - A sticky section bar highlights the section on screen.
 *   - Buttons with data-post send a POST in the background and show the
 *     answer as a short notice, so a quick action never reloads the page.
 *   - Any .aipanel is a Bellwether AI conversation.
 */
(function () {
  "use strict";

  function esc(s) {
    var d = document.createElement("span");
    d.textContent = String(s == null ? "" : s);
    return d.innerHTML;
  }
  window.bwEsc = esc;

  function flash(msg, kind) {
    var el = document.createElement("div");
    el.className = "flash " + (kind || "good");
    el.textContent = msg;
    document.body.appendChild(el);
    setTimeout(function () { el.remove(); }, 3800);
  }
  window.bwFlash = flash;

  /* ------------------------------------------------------------ finder */
  var PAGES = window.BW_PAGES || [];
  var palT = null, palSel = 0, palItems = [];
  function palShow() {
    var p = document.getElementById("pal");
    if (!p) return;
    p.style.display = "block";
    var q = document.getElementById("palq");
    q.value = "";
    palItems = PAGES.slice(0, 8);
    palSel = 0;
    palRender();
    setTimeout(function () { q.focus(); }, 0);
  }
  window.palShow = palShow;
  function palHide() { var p = document.getElementById("pal"); if (p) p.style.display = "none"; }
  function palRender() {
    var r = document.getElementById("palr");
    if (!r) return;
    r.innerHTML = palItems.map(function (x, i) {
      return '<a href="' + esc(x.href) + '" class="' + (i === palSel ? "sel" : "") + '">'
        + "<span>" + esc(x.name) + '</span><span class="m">' + esc(x.meta || "Page") + "</span></a>";
    }).join("") || '<div style="padding:12px 18px;color:var(--faint);font-size:13px">Nothing matches</div>';
  }
  function palMove(d) { palSel = Math.max(0, Math.min(palItems.length - 1, palSel + d)); palRender(); }
  document.addEventListener("keydown", function (e) {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); palShow(); return; }
    var pal = document.getElementById("pal");
    if (pal && pal.style.display === "block") {
      if (e.key === "Escape") palHide();
      if (e.key === "ArrowDown") { e.preventDefault(); palMove(1); }
      if (e.key === "ArrowUp") { e.preventDefault(); palMove(-1); }
      if (e.key === "Enter" && palItems.length) location.href = palItems[palSel].href;
      return;
    }
    var t = e.target.tagName;
    if (t === "INPUT" || t === "TEXTAREA" || t === "SELECT") return;
    if (e.key === "/") { e.preventDefault(); palShow(); return; }
    if (typeof window.rowKey === "function") window.rowKey(e);
  });
  document.addEventListener("input", function (e) {
    if (e.target.id !== "palq") return;
    clearTimeout(palT);
    var v = e.target.value, lv = v.toLowerCase();
    var pages = PAGES.filter(function (p) { return p.name.toLowerCase().indexOf(lv) >= 0; });
    if (v.length < 2) { palItems = pages.slice(0, 8); palSel = 0; palRender(); return; }
    palT = setTimeout(function () {
      fetch("/api/search?q=" + encodeURIComponent(v)).then(function (r) { return r.json(); })
        .then(function (d) {
          palItems = pages.slice(0, 3).concat(d.map(function (x) {
            return { name: x.name, href: x.href, meta: x.meta };
          }));
          palSel = 0;
          palRender();
        });
    }, 110);
  });
  document.addEventListener("click", function (e) {
    var pal = document.getElementById("pal");
    if (pal && e.target === pal) { palHide(); return; }
    var tr = e.target.closest("tr.go");
    if (!tr) return;
    if (e.target.closest("a,button,select,input,form,label,summary,textarea")) return;
    if (e.ctrlKey || e.metaKey) window.open(tr.dataset.href, "_blank");
    else location.href = tr.dataset.href;
  });

  /* ------------------------------------------------------------ section bar */
  var secnav = document.querySelector(".secnav");
  if (secnav && "IntersectionObserver" in window) {
    var links = {};
    secnav.querySelectorAll("a[href^='#']").forEach(function (a) { links[a.getAttribute("href").slice(1)] = a; });
    var visible = {};
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) { visible[en.target.id] = en.isIntersecting ? en.boundingClientRect.top : null; });
      var best = null, bestTop = Infinity;
      Object.keys(visible).forEach(function (id) {
        var t = visible[id];
        if (t !== null && t < bestTop) { best = id; bestTop = t; }
      });
      if (!best) return;
      Object.keys(links).forEach(function (id) { links[id].classList.toggle("on", id === best); });
    }, { rootMargin: "-70px 0px -55% 0px" });
    Object.keys(links).forEach(function (id) { var s = document.getElementById(id); if (s) io.observe(s); });
  }

  /* ------------------------------------------------------------ background actions */
  document.addEventListener("click", function (e) {
    var b = e.target.closest("[data-post]");
    if (!b) return;
    e.preventDefault();
    if (b.dataset.confirm && !confirm(b.dataset.confirm)) return;
    var label = b.innerHTML;
    b.disabled = true;
    b.innerHTML = b.dataset.busy || "Working";
    var body = new URLSearchParams(b.dataset.body || "");
    fetch(b.dataset.post, { method: "POST", body: body, headers: { "Accept": "application/json" } })
      .then(function (r) { return r.json().catch(function () { return { ok: r.ok }; }); })
      .then(function (d) {
        flash(d.message || (d.ok ? "Done" : "That did not work"), d.ok === false ? "bad" : "good");
        if (d.replace && b.dataset.target) {
          var t = document.querySelector(b.dataset.target);
          if (t) t.innerHTML = d.replace;
        }
        if (d.reload) setTimeout(function () { location.reload(); }, 600);
      })
      .catch(function () { flash("Could not reach Bellwether", "bad"); })
      .finally(function () { b.disabled = false; b.innerHTML = label; });
  });

  /* ------------------------------------------------------------ Bellwether AI */
  function aiPanel(panel) {
    var form = panel.querySelector("form.aiform");
    var box = panel.querySelector(".aimsgs");
    var orb = panel.querySelector("canvas[data-orb]");
    var history = [];
    function setOrb(s) { if (orb && window.BWOrb) window.BWOrb.set(orb, s); }
    function add(kind, html) {
      var d = document.createElement("div");
      d.className = "aimsg " + kind;
      d.innerHTML = html;
      box.appendChild(d);
      box.scrollTop = box.scrollHeight;
      return d;
    }
    function ask(q) {
      if (!q) return;
      add("me", esc(q));
      var wait = add("bot", '<span class="muted">Looking through the data</span>');
      setOrb("searching");
      fetch("/api/ai/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ q: q, scope: panel.dataset.scope || "global", history: history.slice(-6) })
      }).then(function (r) { return r.json(); }).then(function (d) {
        setOrb("composing");
        wait.innerHTML = d.html || esc(d.error || "No answer");
        history.push({ role: "user", content: q });
        history.push({ role: "assistant", content: d.text || "" });
        setTimeout(function () { setOrb("breathing"); }, 900);
        box.scrollTop = box.scrollHeight;
      }).catch(function () {
        wait.innerHTML = '<span class="bad">Bellwether AI could not be reached.</span>';
        setOrb("breathing");
      });
    }
    if (form) {
      form.addEventListener("submit", function (e) {
        e.preventDefault();
        var inp = form.querySelector("input");
        var q = inp.value.trim();
        inp.value = "";
        ask(q);
      });
    }
    panel.querySelectorAll(".aisugs button").forEach(function (b) {
      b.addEventListener("click", function () { ask(b.textContent.trim()); });
    });
    var pre = panel.dataset.ask;
    if (pre) ask(pre);
  }
  document.querySelectorAll(".aipanel").forEach(aiPanel);
})();
