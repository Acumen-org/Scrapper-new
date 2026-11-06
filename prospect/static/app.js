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
    el.setAttribute("role", kind === "bad" ? "alert" : "status");
    el.textContent = msg;
    document.body.appendChild(el);
    setTimeout(function () { el.remove(); }, 3800);
  }
  window.bwFlash = flash;

  var navToggle = document.querySelector("[data-nav-toggle]");
  if (navToggle) navToggle.addEventListener("click", function () {
    var open = document.getElementById("main-nav").classList.toggle("open");
    navToggle.setAttribute("aria-expanded", String(open));
    navToggle.textContent = open ? "Close menu" : "Menu";
  });
  document.querySelectorAll("main table").forEach(function (table) {
    if (table.closest(".table-scroll")) return;
    var wrap = document.createElement("div");
    wrap.className = "table-scroll";
    wrap.tabIndex = 0;
    wrap.setAttribute("role", "region");
    wrap.setAttribute("aria-label", "Scrollable data table");
    table.parentNode.insertBefore(wrap, table);
    wrap.appendChild(table);
  });

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
  function revealTab(nav, tab) {
    if (!tab) return;
    var frame = nav.getBoundingClientRect(), item = tab.getBoundingClientRect();
    if (item.left < frame.left) nav.scrollLeft += item.left - frame.left;
    else if (item.right > frame.right) nav.scrollLeft += item.right - frame.right;
  }
  var secnav = document.querySelector(".secnav");
  if (secnav && document.querySelector(".dossier")) {
    var tabs = Array.from(secnav.querySelectorAll("a[href^='#']"));
    var sections = Array.from(document.querySelectorAll(".dossier .anchor"));
    document.querySelector(".dossier").classList.add("dossier-tabs");
    secnav.setAttribute("role", "tablist");
    secnav.setAttribute("aria-label", "Firm information");
    tabs.forEach(function (tab) {
      var id = tab.hash.slice(1), section = document.getElementById(id);
      tab.id = "tab-" + id;
      tab.setAttribute("role", "tab");
      tab.setAttribute("aria-controls", id);
      section.setAttribute("role", "tabpanel");
      section.setAttribute("aria-labelledby", tab.id);
      section.tabIndex = 0;
    });
    function selectSection(hash) {
      var target = document.getElementById((hash || "#overview").slice(1));
      var section = target && target.closest(".anchor");
      if (!section || sections.indexOf(section) < 0) section = sections[0];
      sections.forEach(function (s) { s.hidden = s !== section; });
      tabs.forEach(function (t) {
        var active = t.hash === "#" + section.id;
        t.classList.toggle("on", active);
        t.setAttribute("aria-selected", String(active));
        t.tabIndex = active ? 0 : -1;
      });
      revealTab(secnav, tabs.find(function (t) { return t.getAttribute("aria-selected") === "true"; }));
      if (target && target.matches("details")) target.open = true;
    }
    document.addEventListener("click", function (e) {
      var link = e.target.closest("a[href^='#']");
      if (!link || !link.hash || !document.getElementById(link.hash.slice(1))) return;
      var target = document.getElementById(link.hash.slice(1));
      if (!target.closest(".anchor")) return;
      e.preventDefault();
      history.pushState(null, "", link.hash);
      selectSection(link.hash);
      if (!secnav.contains(link)) target.scrollIntoView({ block: "nearest" });
    });
    secnav.addEventListener("keydown", function (e) {
      var i = tabs.indexOf(document.activeElement);
      if (i < 0 || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
      e.preventDefault();
      var next = e.key === "Home" ? 0 : e.key === "End" ? tabs.length - 1 :
        (i + (e.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
      tabs[next].click();
      tabs[next].focus();
    });
    window.addEventListener("hashchange", function () { selectSection(location.hash); });
    window.addEventListener("popstate", function () { selectSection(location.hash); });
    selectSection(location.hash);
  }

  /* ------------------------------------------------------------ background actions */
  function postAction(b, url, body) {
    if (b.disabled) return;
    if (b.dataset.confirm && !confirm(b.dataset.confirm)) return;
    var label = b.innerHTML;
    b.disabled = true;
    b.innerHTML = b.dataset.busy || "Working";
    // data-timeout gives up after that many milliseconds and says so.
    var ctl = new AbortController(), ms = parseInt(b.dataset.timeout || "0", 10);
    var timer = ms ? setTimeout(function () { ctl.abort(); }, ms) : 0;
    fetch(url, { method: "POST", body: body, headers: { "Accept": "application/json" }, signal: ctl.signal })
      .then(async function (r) {
        if (r.redirected && new URL(r.url).pathname === "/login")
          return { ok: false, message: "Your session expired. Sign in again." };
        var d = await r.json().catch(function () { return {}; });
        if (!r.ok) { d.ok = false; d.message = d.message || "Request failed. Please try again."; }
        return d;
      })
      .then(function (d) {
        flash(d.message || (d.ok ? "Done" : "That did not work"), d.ok ? "good" : "bad");
        if (d.replace && b.dataset.target) {
          document.querySelectorAll(b.dataset.target).forEach(function (t) { t.innerHTML = d.replace; });
        }
        if (d.reload) setTimeout(function () { location.reload(); }, 600);
      })
      .catch(function (err) {
        flash(err && err.name === "AbortError" ? "No answer within " + Math.round(ms / 1000) + " seconds."
              : "Could not reach Bellwether", "bad");
      })
      .finally(function () { clearTimeout(timer); b.disabled = false; b.innerHTML = label; });
  }
  document.addEventListener("click", function (e) {
    var b = e.target.closest("button[data-post]");
    if (!b) return;
    e.preventDefault();
    postAction(b, b.dataset.post, new URLSearchParams(b.dataset.body || ""));
  });
  document.addEventListener("submit", function (e) {
    var form = e.target.closest("form[data-api-form]");
    if (!form) return;
    e.preventDefault();
    postAction(form.querySelector("button[type=submit]"), form.action, new URLSearchParams(new FormData(form)));
  });

  /* ------------------------------------------------------------ Bellwether AI */
  function aiPanel(panel) {
    var form = panel.querySelector("form.aiform");
    var box = panel.querySelector(".aimsgs");
    var orb = panel.querySelector("canvas[data-orb]");
    var history = [];
    var pending = false;
    var inp = form && form.querySelector("input,textarea");
    box.setAttribute("role", "log");
    box.setAttribute("aria-live", "polite");
    function setOrb(s) { if (orb && window.BWOrb) window.BWOrb.set(orb, s); }
    function add(kind, html) {
      var d = document.createElement("div");
      d.className = "aimsg " + kind;
      box.appendChild(d);
      // Bellwether's answers sit beside its mark, so their content goes in
      // a body cell; the user's own words are a plain bubble.
      var body = d;
      if (kind === "bot") { body = document.createElement("div"); body.className = "body"; d.appendChild(body); }
      body.innerHTML = html;
      scrollEnd();
      return body;
    }
    // The full-page conversation scrolls the window; side panels scroll their box.
    function scrollEnd() {
      if (panel.classList.contains("ai-stage")) window.scrollTo(0, document.body.scrollHeight);
      else box.scrollTop = box.scrollHeight;
    }
    function ask(q) {
      if (!q || pending) return;
      panel.classList.add("is-chatting");
      pending = true;
      panel.setAttribute("aria-busy", "true");
      panel.querySelectorAll("button").forEach(function (b) { b.disabled = true; });
      if (inp) { inp.value = ""; inp.readOnly = true; }
      var welcome = panel.querySelector(".ai-welcome");
      if (welcome) welcome.hidden = true;
      add("me", esc(q));
      var wait = add("bot", '<div class="ai-live"><p class="ai-step muted">Looking through the data</p>' +
                            '<div class="ai-draft"></div><div class="ai-rows"></div></div>');
      var step = wait.querySelector(".ai-step"), draft = wait.querySelector(".ai-draft"),
          rows = wait.querySelector(".ai-rows");
      setOrb("searching");
      var controller = new AbortController();
      // The clock restarts with every event, so a long answer that keeps
      // arriving is never cut off; only silence is.
      var timeout = 0;
      function arm() { clearTimeout(timeout); timeout = setTimeout(function () { controller.abort(); }, 60000); }
      arm();
      var written = "", finished = null;
      function onEvent(ev) {
        arm();
        if (ev.t === "status") { step.textContent = ev.text; }
        else if (ev.t === "table") { rows.innerHTML = ev.html; scrollEnd(); }
        else if (ev.t === "delta") {
          if (!written) { setOrb("composing"); step.hidden = true; }
          written += ev.text;
          draft.textContent = written;
          scrollEnd();
        }
        else if (ev.t === "done") { finished = ev; }
        else if (ev.t === "error") { throw new Error(ev.error || "No answer was returned. Try again."); }
      }
      fetch("/api/ai/ask", {
        method: "POST",
        signal: controller.signal,
        headers: { "Content-Type": "application/json", "Accept": "application/x-ndjson" },
        body: JSON.stringify({ q: q, scope: panel.dataset.scope || "global", history: history.slice(-6) })
      }).then(function (r) {
        if (r.status === 401 || (r.redirected && new URL(r.url).pathname === "/login"))
          throw new Error("Your session expired. Sign in again to continue.");
        if (!r.ok) throw new Error("The request failed. Try again.");
        if (!r.body || !r.body.getReader || (r.headers.get("content-type") || "").indexOf("ndjson") < 0) {
          return r.json().then(function (d) {
            if (!d.ok) throw new Error(d.error || "No answer was returned. Try again.");
            finished = d;
          });
        }
        var reader = r.body.getReader(), dec = new TextDecoder(), buf = "";
        function pump() {
          return reader.read().then(function (res) {
            buf += dec.decode(res.value || new Uint8Array(), { stream: !res.done });
            var lines = buf.split("\n");
            buf = res.done ? "" : lines.pop();
            lines.forEach(function (line) { if (line.trim()) onEvent(JSON.parse(line)); });
            if (!res.done) return pump();
          });
        }
        return pump();
      }).then(function () {
        if (!finished) throw new Error("No answer was returned. Try again.");
        setOrb("composing");
        wait.innerHTML = finished.html || esc(finished.error || "No answer");
        history.push({ role: "user", content: q });
        history.push({ role: "assistant", content: finished.text || "" });
        setTimeout(function () { setOrb("breathing"); }, 900);
        scrollEnd();
      }).catch(function (error) {
        var message = error.name === "AbortError" ? "The answer took too long. Try again." :
          error.message === "Failed to fetch" ? "Could not reach Bellwether. Try again." : error.message;
        wait.innerHTML = '<span class="bad">' + esc(message) + '</span>';
        var retry = document.createElement("button");
        retry.type = "button";
        retry.className = "sm ai-retry";
        retry.textContent = "Retry";
        retry.addEventListener("click", function () { if (!pending) { wait.closest(".aimsg").remove(); ask(q); } });
        wait.appendChild(document.createElement("br"));
        wait.appendChild(retry);
        if (inp) inp.value = q;
        setOrb("breathing");
      }).finally(function () {
        clearTimeout(timeout);
        pending = false;
        panel.setAttribute("aria-busy", "false");
        panel.querySelectorAll("button").forEach(function (b) { b.disabled = false; });
        if (inp) inp.readOnly = false;
        scrollEnd();
      });
    }
    if (form) {
      form.addEventListener("submit", function (e) {
        e.preventDefault();
        var q = inp.value.trim();
        ask(q);
      });
      if (inp.tagName === "TEXTAREA") inp.addEventListener("keydown", function (e) {
        if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
          e.preventDefault();
          form.requestSubmit();
        }
      });
    }
    panel.querySelectorAll(".aisugs button").forEach(function (b) {
      b.addEventListener("click", function () { ask(b.dataset.question || b.textContent.trim()); });
    });
    var pre = panel.dataset.ask;
    if (pre) ask(pre);
  }
  document.querySelectorAll(".aipanel").forEach(aiPanel);
  document.querySelectorAll("[data-focus-ai]").forEach(function (b) {
    b.addEventListener("click", function () {
      var chat = document.getElementById("firm-ai");
      if (!chat) return;
      var drawer = document.getElementById("firm-ai-drawer");
      if (drawer && drawer.contains(chat) && !drawer.open) drawer.showModal();
      else chat.scrollIntoView({ block: "center", behavior: "smooth" });
      chat.querySelector("textarea").focus({ preventScroll: true });
    });
  });
  document.querySelectorAll("[data-close-ai]").forEach(function (b) {
    b.addEventListener("click", function () { b.closest("dialog").close(); });
  });

  /* ------------------------------------------------------------ filter bar
     Quick filters apply the moment they change; the More filters panel waits
     for its Apply button so several can be set at once. */
  document.querySelectorAll("form.fbar").forEach(function (form) {
    form.addEventListener("change", function (e) {
      if (e.target.closest(".fpanel")) return;
      if (e.target.matches("select,input[type=checkbox]")) form.requestSubmit();
    });
  });
  document.addEventListener("click", function (e) {
    document.querySelectorAll("details.fmore[open],details.save-view[open]").forEach(function (d) {
      if (!d.contains(e.target)) d.open = false;
    });
  });
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    document.querySelectorAll("details.fmore[open],details.save-view[open]").forEach(function (d) { d.open = false; });
  });
  document.querySelectorAll(".composer textarea").forEach(function (t) {
    function fit() { t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight, 240) + "px"; }
    t.addEventListener("input", fit);
  });
  document.querySelectorAll('form[action="/views/save"]').forEach(function (form) {
    var details = document.createElement("details");
    details.className = "save-view";
    var summary = document.createElement("summary");
    summary.textContent = "Save view";
    form.before(details);
    details.append(summary, form);
  });

  document.querySelectorAll("[data-workspace-tabs]").forEach(function (nav) {
    var tabs = Array.from(nav.querySelectorAll("button"));
    nav.setAttribute("role", "tablist");
    function select(tab, navigate) {
      tabs.forEach(function (button) {
        var selected = button === tab;
        button.setAttribute("role", "tab");
        button.setAttribute("aria-selected", String(selected));
        button.tabIndex = selected ? 0 : -1;
        var section = document.getElementById(button.dataset.panel);
        section.hidden = !selected;
        section.setAttribute("role", "tabpanel");
        section.setAttribute("aria-labelledby", button.id);
        section.tabIndex = 0;
        button.setAttribute("aria-controls", section.id);
      });
      revealTab(nav, tab);
      if (navigate) history.pushState(null, "", "#" + tab.dataset.panel);
    }
    tabs.forEach(function (tab, i) {
      tab.id = "workspace-tab-" + tab.dataset.panel;
      tab.addEventListener("click", function () { select(tab, true); });
      tab.addEventListener("keydown", function (e) {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
        e.preventDefault();
        var next = e.key === "Home" ? 0 : e.key === "End" ? tabs.length-1 : (i + (e.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
        select(tabs[next], true); tabs[next].focus();
      });
    });
    function fromHash() {
      select(tabs.find(function (t) { return "#" + t.dataset.panel === location.hash; }) || tabs[0]);
    }
    document.addEventListener("click", function (e) {
      var link = e.target.closest("a[href^='#']");
      var tab = link && tabs.find(function (t) { return "#" + t.dataset.panel === link.hash; });
      if (!tab) return;
      e.preventDefault(); select(tab, true); tab.focus();
    });
    window.addEventListener("hashchange", fromHash);
    window.addEventListener("popstate", fromHash);
    fromHash();
  });

  /* ------------------------------------------------------------ Cold outreach
     Send people to Cold outreach in Cadence. Tick people on People or on a
     firm page (or press Send to Cold outreach on one card), see in the dialog
     who would go and who would not (Cadence's own check says who is already
     in the CRM), send, and watch each contact's result. The page's status
     lines update when the dialog closes. Everything is decided on the server
     (prospect/cadence.py); this only shows it. */
  var cadCtx = document.getElementById("cad-ctx");
  if (cadCtx) cadencePush(cadCtx);

  function cadencePush(ctx) {
    var picked = new Map(), allMatching = false, firstOnly = false, bar = null, dlg = null, opener = null;
    var poll = 0, st = {};
    var PHONE_TYPE = { direct: "Direct", mobile: "Mobile", office: "Office", main: "Main line",
                       toll_free: "Toll free", firm: "Firm's main line" };
    // Why someone will not go, as group headings (B083, B177).
    var GROUP = { already_in_crm: "Already in CRM", in_cadence: "Already sent", opted_out: "Do not contact",
                  do_not_contact: "Do not contact", erased: "Erased in Cadence", customer: "Customer firm",
                  no_name: "No first and last name", no_contact: "No way to reach them",
                  other_firm: "Already sent from another firm", same_email: "Same email as someone sent",
                  same_in_push: "Same email twice", same_person: "Same person twice",
                  cadence_refuses: "Cadence would refuse", not_found: "Not in the people list yet" };
    var total = parseInt(ctx.dataset.total || "0", 10) || 0;
    var cap = parseInt(ctx.dataset.cap || "200", 10) || 200;

    function boxes() { return Array.from(document.querySelectorAll("input[data-cad-id]")); }
    function plural(n, one, many) { return n.toLocaleString() + " " + (n === 1 ? one : many); }
    function sync() {
      var bs = boxes();
      bs.forEach(function (b) { b.checked = allMatching || picked.has(b.dataset.cadId); });
      var head = document.querySelector("input[data-cad-all]");
      if (head) {
        var on = bs.filter(function (b) { return b.checked; }).length;
        head.checked = bs.length > 0 && on === bs.length;
        head.indeterminate = on > 0 && on < bs.length;
      }
      renderBar();
    }
    function renderBar() {
      var n = allMatching ? Math.min(total, cap) : picked.size;
      if (!bar) {
        bar = document.createElement("div");
        bar.className = "selbar";
        bar.hidden = true;
        bar.setAttribute("role", "region");
        bar.setAttribute("aria-label", "Selected people");
        bar.innerHTML = '<span class="cnt" aria-live="polite"></span><button type="button" class="sm ghost" data-cad-matching hidden></button>' +
          '<button type="button" class="primary sm" data-cad-bulk>Send to Cold outreach</button>' +
          '<button type="button" class="sm ghost" data-cad-clear>Clear</button>';
        document.body.appendChild(bar);
      }
      bar.querySelector(".cnt").textContent = !n ? "" : allMatching ?
        (firstOnly ? "The first " + plural(n, "person", "people") + " of " + total.toLocaleString() + " matching" :
         "All " + plural(total, "person", "people") + " matching") : n.toLocaleString() + " selected";
      bar.hidden = !n;
      if (!n) return;
      var bs = boxes(), every = bs.length > 0 && bs.every(function (b) { return picked.has(b.dataset.cadId); });
      var more = bar.querySelector("[data-cad-matching]");
      // Only People can select beyond the page, and never past one send's cap (B077).
      more.hidden = !(ctx.dataset.origin === "people" && every && total > bs.length && !allMatching);
      more.textContent = total > cap ? "Select the first " + cap.toLocaleString() + " of " + total.toLocaleString() :
        "Select all " + total.toLocaleString() + " matching";
    }
    document.addEventListener("change", function (e) {
      var b = e.target.closest("input[data-cad-id]");
      if (b) {
        allMatching = false;
        if (b.checked) picked.set(b.dataset.cadId, b.dataset.cadName || ""); else picked.delete(b.dataset.cadId);
        sync();
        return;
      }
      var head = e.target.closest("input[data-cad-all]");
      if (head) {
        allMatching = false;
        boxes().forEach(function (x) {
          if (head.checked) picked.set(x.dataset.cadId, x.dataset.cadName || ""); else picked.delete(x.dataset.cadId);
        });
        sync();
      }
    });
    document.addEventListener("click", function (e) {
      var t = e.target.closest("[data-cad-matching],[data-cad-bulk],[data-cad-clear],button[data-cad-push],button[data-cad-refresh],button[data-cad-dnc],button[data-cad-batch],button[data-cad-retry]");
      if (!t) return;
      e.preventDefault();
      if (t.matches("[data-cad-matching]")) { allMatching = true; firstOnly = total > cap; sync(); }
      else if (t.matches("[data-cad-clear]")) { allMatching = false; picked.clear(); sync(); }
      else if (t.matches("[data-cad-bulk]")) {
        open(allMatching ? { filter: ctx.dataset.qs || "", first: firstOnly } : { ids: Array.from(picked.keys()) }, t);
      }
      else if (t.matches("button[data-cad-push]")) open({ ids: [t.dataset.cadPush] }, t);
      else if (t.matches("button[data-cad-retry]")) open({ ids: t.dataset.cadRetry.split(" ").filter(Boolean) }, t);
      else if (t.matches("button[data-cad-batch]")) results(t.dataset.cadBatch, t);
      else if (t.matches("button[data-cad-refresh]")) refresh([t.dataset.cadRefresh], t);
      else if (t.matches("button[data-cad-dnc]")) dnc(t);
    });

    function post(url, payload) {
      return fetch(url, { method: "POST", body: JSON.stringify(payload),
                          headers: { "Content-Type": "application/json", "Accept": "application/json" } })
        .then(read);
    }
    function read(r) {
      if (r.status === 401 || (r.redirected && new URL(r.url).pathname === "/login"))
        throw new Error("Your session expired. Sign in again.");
      return r.json().catch(function () { return { gateway: true }; }).then(function (d) {
        if (!r.ok) { d.ok = false; d.gateway = d.gateway || r.status >= 500; d.message = d.message || d.error || "That did not work. Try again."; }
        return d;
      });
    }
    function update(lines) {
      Object.keys(lines || {}).forEach(function (id) {
        var sel = '[data-cad-status="' + CSS.escape(id) + '"]';
        document.querySelectorAll(sel).forEach(function (el) {
          var ctl = el.closest(".cad-ctl");
          // A firm card gets its whole controls back, so a person who can be
          // sent again has Select and Send without a reload (N094).
          if (ctl && lines[id].card) ctl.outerHTML = lines[id].card;
          else el.innerHTML = lines[id].html;
        });
        if (!lines[id].can_push) {
          document.querySelectorAll('[data-cad-id="' + CSS.escape(id) + '"],button[data-cad-push="' + CSS.escape(id) + '"]')
            .forEach(function (el) {
              var lab = el.closest("label");
              (lab || el).hidden = true;
              if (el.matches("input")) el.checked = false;
            });
          picked.delete(id);
        }
      });
      sync();
    }
    function refresh(ids, btn) {
      if (btn) btn.disabled = true;
      return post("/api/cadence/refresh", { ids: ids, cards: ctx.dataset.origin === "firm" }).then(function (d) {
        update(d.lines);
        if (btn) flash(d.message || "Done", d.ok ? "good" : "bad");
      }).catch(function (err) { if (btn) flash(err.message, "bad"); })
        .finally(function () { if (btn && document.contains(btn)) btn.disabled = false; });
    }
    /* A firm page reads Cadence's status for its sent people once, when
       the People & contacts tab is first shown. */
    if (ctx.dataset.origin === "firm") {
      var readOnce = false;
      var maybe = function () {
        var sec = document.getElementById("people");
        if (readOnce || !sec || sec.hidden) return;
        readOnce = true;
        var ids = Array.from(document.querySelectorAll("[data-cad-pushed]")).map(function (x) { return x.dataset.cadPushed; });
        if (ids.length) refresh(ids.slice(0, 500));
      };
      setTimeout(maybe, 0);
      window.addEventListener("hashchange", maybe);
      document.addEventListener("click", function () { setTimeout(maybe, 0); });
    }

    function dialog() {
      if (dlg) return dlg;
      dlg = document.createElement("dialog");
      dlg.className = "cad-dialog";
      dlg.setAttribute("aria-labelledby", "cad-title");
      dlg.innerHTML = '<div class="cad-head"><h2 id="cad-title">Send to Cold outreach</h2>' +
        '<button type="button" class="sm ghost" data-cad-close>Close</button></div>' +
        '<div class="cad-body" aria-live="polite"></div><div class="cad-foot"></div>';
      document.body.appendChild(dlg);
      dlg.addEventListener("click", function (e) {
        if (e.target === dlg || e.target.closest("[data-cad-close]")) dlg.close();
      });
      dlg.addEventListener("close", function () {
        clearTimeout(poll);
        if (st.lines) update(st.lines);
        if (opener && document.contains(opener)) opener.focus();
      });
      return dlg;
    }
    function bodyEl() { return dlg.querySelector(".cad-body"); }
    function foot(html) { dlg.querySelector(".cad-foot").innerHTML = html; }
    function title(text) { dlg.querySelector("#cad-title").textContent = text; }
    function key() {
      return window.crypto && crypto.randomUUID ? crypto.randomUUID() :
        Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 12);
    }
    function open(sel, from) {
      opener = from || document.activeElement;
      st = { sel: sel, product: ctx.dataset.product || "", owner: "", pod: "", note: "", confirm: false, key: key() };
      dialog();
      if (!dlg.open) dlg.showModal();
      preview();
    }
    function payload() {
      return { ids: st.sel.ids, filter: st.sel.filter, first: st.sel.first, product: st.product, owner: st.owner,
               pod: st.pod, note: st.note, origin: ctx.dataset.origin, confirm: st.confirm, send_key: st.key };
    }
    function preview(reuse) {
      title("Send to Cold outreach");
      bodyEl().innerHTML = '<p class="muted">Checking who would go, and asking Cadence who is already in the CRM.</p>';
      foot('<button type="button" class="ghost" data-cad-close>Cancel</button>');
      var p = payload();
      if (reuse) p.reuse = reuse;
      post("/api/cadence/preview", p).then(showPreview).catch(function (err) {
        bodyEl().innerHTML = '<div class="note bad">' + esc(err.message) + '</div>';
      });
    }
    function person(x) {
      return '<div class="who"><b>' + esc(x.name) + '</b><span class="meta">' + esc(x.firm || "") + '</span></div>';
    }
    function showPreview(d) {
      st.preview = d;
      if (!st.owner) st.owner = d.owner || "";
      // The owner's pod goes with the send (S3): asked only when Cadence lists more than one for them.
      st.pod = d.pod || "";
      if ("product" in d) st.product = d.product || "";
      var k = (d.send || []).length, n = d.count || 0, h = "";
      // The dialog's order (B076): the verdict, the choices, then who goes and who does not.
      title(k ? "Send " + k.toLocaleString() + " of " + plural(n, "selected", "selected") : "Nothing to send");
      if (d.over) {
        h += '<div class="note plain">' + esc(d.problem) + '</div>';
        bodyEl().innerHTML = h;
        foot('<button type="button" class="ghost" data-cad-close>Cancel</button>' +
          '<button type="button" class="primary" data-cad-first>Send the first ' + (d.max_per_push || cap).toLocaleString() + '</button>');
        dlg.querySelector("[data-cad-first]").addEventListener("click", function () {
          st.sel = { filter: st.sel.filter, first: true };
          preview();
        });
        return;
      }
      if (d.problem) {
        // Nothing can go at all (switched off, held, no email, too many): say
        // why and what to do, not a list that cannot be sent (B084).
        title("Nothing to send");
        bodyEl().innerHTML = '<div class="note bad"><b>Nothing will be sent:</b> ' + esc(d.problem) +
          (d.check && d.check.state === "off" ? " Cadence's CRM check was not run." : "") + '</div>';
        foot('<button type="button" class="ghost" data-cad-close>Close</button>');
        return;
      }
      if (d.verdict) h += '<p class="cad-verdict"><b>' + esc(d.verdict) + '</b></p>';
      else h += '<div class="note bad"><b>Nothing will be sent:</b> ' + esc(d.nothing || "nobody here can go") + '</div>';
      if (d.check && d.check.state === "unavailable") h += '<div class="note warn">' + esc(d.check.message) + '</div>';
      if (k) {
        var ov = d.owner_verdict || {};
        var owners = (d.owners || []).map(function (o) {
          return '<option value="' + esc(o.email) + '"' + (o.email === st.owner ? " selected" : "") + '>' + esc(o.name) + '</option>';
        }).join("");
        var prods = '<option value="">None</option>' + (d.products || []).map(function (p) {
          return '<option value="' + esc(p.key) + '"' + (p.key === st.product ? " selected" : "") + '>' + esc(p.name) + '</option>';
        }).join("");
        h += '<div class="cad-fields top"><label>Owner<select data-cad-owner>' + owners + '</select>' +
          (ov.unclaimed ? '<span class="meta warnc">These ' + plural(k, "person", "people") + ' will arrive unclaimed: ' +
            esc(ov.name || "this person") + ' cannot own contacts in Cadence.</span>' :
           '<span class="meta">Only people who can own contacts in Cadence are listed.</span>') + '</label>' +
          ((d.pods || []).length > 1 ? '<label>Pod<select data-cad-pod>' + '<option value="">Choose a pod</option>' +
            d.pods.map(function (p) {
              return '<option value="' + esc(p) + '"' + (p === st.pod ? " selected" : "") + '>' + esc(p) + '</option>';
            }).join("") + '</select><span class="meta">' + esc(ov.name || "This owner") +
            ' works in ' + d.pods.length + ' pods. Without a choice the contacts go to the first by name.</span></label>' : "") +
          '<label>Product<select data-cad-product>' + prods + '</select></label>' +
          '<label class="wide">' + (k > 1 ? "Note for all " + k.toLocaleString() + " people (copied to each)" : "Note") +
          '<textarea data-cad-note maxlength="' + (d.note_max || 500) + '" rows="2" placeholder="Optional, the first note in Cold outreach">' +
          esc(st.note) + '</textarea></label></div>';
        h += '<h3>Will go <span class="h-cnt">' + k.toLocaleString() + '</span></h3><ul class="cad-list">';
        d.send.forEach(function (x) {
          var bits = [];
          if (x.email) bits.push(esc(x.email) + ' <span class="chip ' + (x.email_status === "Verified" ? "good" : "line") + '">' + esc(x.email_status) + '</span>');
          if (x.phone) bits.push(esc(x.phone) + ' <span class="chip line">' + esc(PHONE_TYPE[x.phone_type] || "Phone") + '</span>');
          h += '<li>' + person(x) + '<div class="how">' + (bits.join(" &middot; ") || '<span class="muted">No email or phone</span>') +
            (x.note ? '<div class="meta">' + esc(x.note) + '</div>' : "") +
            (x.warnings || []).map(function (w) { return '<div class="meta warnc">' + esc(w) + '</div>'; }).join("") +
            (x.again ? '<div class="meta warnc">' + esc(x.again) + '</div>' : "") +
            (x.disqualified ? '<div class="meta warnc">Firm marked Disqualified</div>' : "") + '</div></li>';
        });
        h += '</ul>';
      }
      if ((d.skipped || []).length) {
        var groups = {}, order = [];
        d.skipped.forEach(function (x) {
          var g = GROUP[x.rule] || "Will not go";
          if (!groups[g]) { groups[g] = []; order.push(g); }
          groups[g].push(x);
        });
        h += '<h3>Won\'t go <span class="h-cnt">' + d.skipped.length.toLocaleString() + '</span></h3>';
        order.forEach(function (g) {
          h += '<details class="cad-group"' + (order.length === 1 ? " open" : "") + '><summary>' + esc(g) +
            ' <span class="h-cnt">' + groups[g].length.toLocaleString() + '</span></summary><ul class="cad-list">';
          groups[g].forEach(function (x) {
            var link = x.crm_url && /^https:\/\//i.test(x.crm_url) ?
              ' <a href="' + esc(x.crm_url) + '" target="_blank" rel="noopener">Open in CRM</a>' : "";
            h += '<li>' + person(x) + '<div class="how">' + esc(x.why) + link + '</div></li>';
          });
          h += '</ul></details>';
        });
      }
      bodyEl().innerHTML = h;
      var asks = (d.disqualified || []).length || (d.again || []).length;
      var why = k && d.over_cap ? d.over_cap : "";
      var tickText = [];
      if ((d.disqualified || []).length) tickText.push(d.disqualified.join(", ") + (d.disqualified.length === 1 ? " is" : " are") + " marked Disqualified");
      if ((d.again || []).length) tickText.push(plural(d.again.length, "person was", "people were") + " removed or called in Cold outreach (see above)");
      // The footer stays in view: why Send is off, and what is left of today's allowance (B076, B179).
      foot('<div class="cad-why" role="status">' +
        (asks && k ? '<label class="inline"><input type="checkbox" data-cad-confirm' + (st.confirm ? " checked" : "") + '> Send anyway: ' + esc(tickText.join("; ")) + '</label>' : "") +
        '<span class="meta" data-cad-reason>' + esc(why) + '</span>' +
        (k && "left_today" in d ? '<span class="meta">You can send ' + d.left_today.toLocaleString() + ' more today</span>' : "") +
        '<div class="cad-msg" role="alert"></div></div>' +
        '<button type="button" class="ghost" data-cad-close>Cancel</button>' +
        (k ? '<button type="button" class="primary" data-cad-go>Send ' + k.toLocaleString() + ' to Cold outreach</button>' : ""));
      var go = dlg.querySelector("[data-cad-go]");
      var tick = dlg.querySelector("[data-cad-confirm]");
      var reason = dlg.querySelector("[data-cad-reason]");
      function gate() {
        if (!go) return;
        go.disabled = !d.can_send || (tick && !tick.checked);
        if (d.can_send && tick && !tick.checked) reason.textContent = "Tick Send anyway to include them.";
        else if (d.can_send) reason.textContent = "";
      }
      gate();
      if (tick) tick.addEventListener("change", function () { st.confirm = tick.checked; gate(); });
      var own = dlg.querySelector("[data-cad-owner]"), prod = dlg.querySelector("[data-cad-product]"),
          note = dlg.querySelector("[data-cad-note]");
      // A new owner or product keeps the check Cadence already answered (B065).
      if (own) own.addEventListener("change", function () { st.owner = own.value; st.pod = ""; preview(d.preview_id); });
      var podSel = dlg.querySelector("[data-cad-pod]");
      if (podSel) podSel.addEventListener("change", function () { st.pod = podSel.value; });
      if (prod) prod.addEventListener("change", function () { st.product = prod.value; preview(d.preview_id); });
      if (note) note.addEventListener("input", function () { st.note = note.value; });
      if (go) go.addEventListener("click", function () { push(go); });
    }
    function say(html) { var m = dlg.querySelector(".cad-msg"); if (m) m.innerHTML = html; }
    function push(go) {
      var label = go.textContent;
      go.disabled = true;
      go.textContent = "Sending";
      var p = payload();
      p.preview_id = st.preview && st.preview.preview_id;
      post("/api/cadence/push", p).then(function (d) {
        if (d.gateway) return checking(go, label);
        if (!d.ok) {
          say('<div class="note bad">' + esc(d.message || "That did not work.") + '</div>');
          go.disabled = false;
          go.textContent = label;
          return;
        }
        sent(d);
      }).catch(function () { checking(go, label); });
    }
    function sent(d) {
      picked.clear();
      allMatching = false;
      renderBar();
      showResults(d);
    }
    /* The answer never came (a gateway error or a dropped connection), but the
       send may be going: look for it by the dialog's own key (B078). */
    function checking(go, label) {
      say('<div class="note plain">Your send may still be running; checking.</div>');
      var tries = 0;
      (function look() {
        fetch("/api/cadence/batch?key=" + encodeURIComponent(st.key), { headers: { "Accept": "application/json" } })
          .then(read).then(function (d) {
            if (d.ok) return sent(d);
            if (++tries < 15) { poll = setTimeout(look, 2000); return; }
            say('<div class="note bad">It did not go: Bellwether has no record of this send. Send again.</div>');
            go.disabled = false;
            go.textContent = label;
          }).catch(function () {
            if (++tries < 15) { poll = setTimeout(look, 2000); return; }
            say('<div class="note bad">Bellwether did not answer. Check My sends on People before sending again.</div>');
          });
      })();
    }
    function results(bid, from) {
      opener = from || document.activeElement;
      st = {};
      dialog();
      if (!dlg.open) dlg.showModal();
      title("Send");
      bodyEl().innerHTML = '<p class="muted">Reading the send.</p>';
      foot('<button type="button" class="primary" data-cad-close>Close</button>');
      fetch("/api/cadence/batch/" + encodeURIComponent(bid), { headers: { "Accept": "application/json" } })
        .then(read).then(function (d) {
          if (!d.ok) bodyEl().innerHTML = '<div class="note bad">' + esc(d.message || "No such send.") + '</div>';
          else showResults(d);
        }).catch(function (err) { bodyEl().innerHTML = '<div class="note bad">' + esc(err.message) + '</div>'; });
    }
    function showResults(d) {
      st.lines = Object.assign(st.lines || {}, d.lines || {});
      title(d.title || "Sent");
      // The summary first, then each person (B175).
      var h = '<p class="cad-sum ' + esc(d.tone || "") + '"><b>' + esc(d.summary || "") + '</b>' +
        (d.pending ? ' <span class="muted">Still going: Bellwether keeps trying in the background, and you can close this.</span>' : "") +
        '</p><ul class="cad-list results">';
      (d.results || []).forEach(function (x) {
        var links = (x.cadence_url ? ' <a href="' + esc(x.cadence_url) + '" target="_blank" rel="noopener">Open in Cadence</a>' : "") +
          (x.crm_url && x.state === "in_crm" ? ' <a href="' + esc(x.crm_url) + '" target="_blank" rel="noopener">Open in CRM</a>' : "");
        h += '<li>' + person(x) + '<div class="how"><span class="ln ' + esc(x.tone || "") + '">' + esc(x.line) + '</span>' + links +
          ((x.warnings || []).length ? '<div class="meta warnc">' + esc(x.warnings.join(". ")) + '</div>' : "") + '</div></li>';
      });
      h += '</ul><div class="cad-msg" role="alert"></div>';
      bodyEl().innerHTML = h;
      var failed = (d.results || []).filter(function (x) { return x.retry; }).map(function (x) { return x.id; });
      foot((failed.length ? '<button type="button" data-cad-retry="' + esc(failed.join(" ")) + '">Retry ' + failed.length.toLocaleString() + '</button>' : "") +
        '<button type="button" class="primary" data-cad-close>Close</button>');
      clearTimeout(poll);
      if (d.pending && d.batch_id) {
        poll = setTimeout(function () {
          fetch("/api/cadence/batch/" + encodeURIComponent(d.batch_id), { headers: { "Accept": "application/json" } })
            .then(read).then(function (n) { if (n.ok && dlg.open) showResults(n); })
            .catch(function () {
              // Say so, rather than leave the dialog looking current (B080).
              say('<div class="note plain">Could not read the latest results. They are in My sends on People.</div>');
            });
        }, 2000);
      }
    }

    var dncDlg = null;
    function dnc(btn) {
      if (!dncDlg) {
        dncDlg = document.createElement("dialog");
        dncDlg.className = "cad-dialog small";
        dncDlg.setAttribute("aria-labelledby", "dnc-title");
        dncDlg.innerHTML = '<div class="cad-head"><h2 id="dnc-title">Do not contact</h2>' +
          '<button type="button" class="sm ghost" data-dnc-close>Close</button></div>' +
          '<form class="cad-body cad-fields" data-api-form action="/api/cadence/suppress" method="post">' +
          '<input type="hidden" name="id"><p class="wide muted" data-dnc-who></p>' +
          '<label class="wide">Covers<select name="scope"><option value="person">This person, at every firm Bellwether knows them at</option>' +
          '<option value="firm" data-dnc-firm></option></select></label>' +
          '<label class="inline wide" data-dnc-confirm hidden><input type="checkbox" name="confirm" value="1"> <span></span></label>' +
          '<label>Reason<select name="reason"><option value="opted_out">Asked not to be contacted</option><option value="do_not_call">Do not call</option><option value="do_not_email">Do not email</option><option value="other">Other</option></select></label>' +
          '<label class="wide">Note<input type="text" name="note" maxlength="300"></label>' +
          '<p class="wide meta">It covers email and calls. Nobody can send them to Cold outreach while it stands, and anyone already there is opted out in Cadence too. Only an admin can lift it here.</p>' +
          '<div class="wide row"><button type="submit" class="primary" data-busy="Saving">Save</button></div></form>';
        document.body.appendChild(dncDlg);
        dncDlg.addEventListener("click", function (e) {
          if (e.target === dncDlg || e.target.closest("[data-dnc-close]")) dncDlg.close();
        });
        var scope = dncDlg.querySelector("select[name=scope]"), box = dncDlg.querySelector("[data-dnc-confirm]");
        // Everyone at the firm says how many people it covers, and needs a tick (B075).
        scope.addEventListener("change", function () {
          box.hidden = scope.value !== "firm";
          box.querySelector("input").required = scope.value === "firm";
        });
        // The form posts like any data-api-form; the answer reloads the page when it took.
        dncDlg.addEventListener("submit", function () { setTimeout(function () { dncDlg.close(); }, 300); });
        dncDlg.addEventListener("close", function () { if (opener && document.contains(opener)) opener.focus(); });
      }
      opener = btn;
      var firm = ctx.dataset.firm || "this firm", people = parseInt(ctx.dataset.people || "0", 10) || 0;
      dncDlg.querySelector("input[name=id]").value = btn.dataset.cadDnc;
      dncDlg.querySelector("[data-dnc-who]").textContent = btn.dataset.cadName || "";
      dncDlg.querySelector("[data-dnc-firm]").textContent = "Everyone Bellwether knows at " + firm +
        (people ? " (" + plural(people, "person", "people") + ")" : "");
      dncDlg.querySelector("[data-dnc-confirm] span").textContent = "Yes: nobody at " + firm +
        " can be sent to Cold outreach, and everyone already sent from it is opted out in Cadence.";
      dncDlg.querySelector("select[name=scope]").value = "person";
      dncDlg.querySelector("[data-dnc-confirm]").hidden = true;
      dncDlg.showModal();
    }
  }
})();
