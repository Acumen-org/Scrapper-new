/* Bellwether charts: responsive, interactive, no library.
 *
 * The server renders every chart twice: a static SVG for the first paint (and
 * for anyone without scripts), and the data as JSON on the wrapper:
 *
 *   <div class="ichart" data-chart="line" data-series='{"points": [["2024-03-01", 1.2e9], ...],
 *        "format": "money"}' data-height="220"> static svg </div>
 *   <div class="ichart" data-chart="bars" data-series='{"labels": [2019, ...],
 *        "up": [...], "down": [...], "upName": "joined", "downName": "left"}'> ... </div>
 *
 * This file redraws each one at the width it actually has, keeps it sized as
 * the window or a tab changes, and adds hover: a crosshair and readout on line
 * charts, a highlighted column and readout on bar charts. Anything else that
 * carries data-tip="..." gets the same styled tooltip.
 */
(function () {
  "use strict";
  var NS = "http://www.w3.org/2000/svg";

  function esc(s) {
    var d = document.createElement("span");
    d.textContent = String(s == null ? "" : s);
    return d.innerHTML;
  }

  function money(v) {
    var a = Math.abs(v);
    if (a >= 1e12) return "$" + (v / 1e12).toFixed(2) + "T";
    if (a >= 1e9) return "$" + (v / 1e9).toFixed(2) + "B";
    if (a >= 1e6) return "$" + (v / 1e6).toFixed(0) + "M";
    if (a >= 1e3) return "$" + (v / 1e3).toFixed(0) + "K";
    return "$" + Math.round(v).toLocaleString();
  }
  function fmt(v, kind) {
    return kind === "money" ? money(v) : Math.round(v).toLocaleString();
  }
  // Axis labels drop trailing zeros: $80B, $1.5B, not $80.00B.
  function tickFmt(v, kind) {
    return fmt(v, kind).replace(/\.0+([KMBT])$/, "$1").replace(/(\.\d*?)0+([KMBT])$/, "$1$2");
  }

  function node(tag, attrs, parent) {
    var n = document.createElementNS(NS, tag);
    for (var k in attrs) n.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(n);
    return n;
  }

  /* ---------------------------------------------------------- tooltip */
  var tip = document.createElement("div");
  tip.className = "bw-tip";
  tip.setAttribute("role", "tooltip");
  tip.hidden = true;
  document.body.appendChild(tip);

  function showTip(html, x, y) {
    tip.innerHTML = html;
    tip.hidden = false;
    var r = tip.getBoundingClientRect();
    var left = Math.min(window.innerWidth - r.width - 8, Math.max(8, x - r.width / 2));
    var top = y - r.height - 12;
    if (top < 8) top = y + 18;
    tip.style.left = left + "px";
    tip.style.top = top + "px";
  }
  function hideTip() { tip.hidden = true; }
  window.bwTip = { show: showTip, hide: hideTip };

  document.addEventListener("pointerover", function (e) {
    var t = e.target.closest && e.target.closest("[data-tip]");
    if (!t) return;
    var r = t.getBoundingClientRect();
    showTip(t.getAttribute("data-tip"), r.left + r.width / 2, r.top);
  });
  document.addEventListener("pointerout", function (e) {
    var t = e.target.closest && e.target.closest("[data-tip]");
    if (t && !t.contains(e.relatedTarget)) hideTip();
  });
  window.addEventListener("scroll", hideTip, { passive: true });

  /* ---------------------------------------------------------- scales */
  function niceStep(span, count) {
    var raw = span / Math.max(1, count);
    var mag = Math.pow(10, Math.floor(Math.log10(raw)));
    var f = raw / mag;
    return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * mag;
  }
  function ticks(lo, hi, count) {
    if (hi === lo) hi = lo + 1;
    var step = niceStep(hi - lo, count);
    var start = Math.floor(lo / step) * step, out = [];
    for (var v = start; v <= hi + step * 0.5; v += step) out.push(v);
    return out;
  }
  function dateNum(s) {
    var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(s));
    return m ? Date.UTC(+m[1], +m[2] - 1, +m[3]) : NaN;
  }

  /* ---------------------------------------------------------- line chart */
  function lineChart(box, d) {
    var pts = d.points || [];
    var W = box.clientWidth, H = +(box.dataset.height || 220);
    if (!W || pts.length < 2) return;
    var kind = d.format || "number";
    var PL = 6, PR = 70, PT = 14, PB = 28;
    var xs = pts.map(function (p) { return dateNum(p[0]); });
    var vs = pts.map(function (p) { return +p[1]; });
    var lo = Math.min.apply(null, vs), hi = Math.max.apply(null, vs);
    if (lo >= 0 && lo < hi * 0.35) lo = 0;
    var yt = ticks(lo, hi, 4);
    lo = yt[0]; hi = yt[yt.length - 1];
    var x0 = xs[0], x1 = xs[xs.length - 1] || x0 + 1;
    function X(x) { return PL + (x - x0) / ((x1 - x0) || 1) * (W - PL - PR); }
    function Y(v) { return PT + (1 - (v - lo) / ((hi - lo) || 1)) * (H - PT - PB); }

    var svg = node("svg", { class: "chart live", width: W, height: H, viewBox: "0 0 " + W + " " + H,
                            role: "img", "aria-label": box.dataset.label || "Chart" });
    var gid = "g" + Math.random().toString(36).slice(2, 8);
    var defs = node("defs", {}, svg);
    var grad = node("linearGradient", { id: gid, x1: 0, y1: 0, x2: 0, y2: 1 }, defs);
    node("stop", { offset: "0", "stop-color": "var(--ok)", "stop-opacity": ".22" }, grad);
    node("stop", { offset: "1", "stop-color": "var(--ok)", "stop-opacity": "0" }, grad);

    yt.forEach(function (v) {
      node("line", { class: "grid", x1: PL, x2: W - PR, y1: Y(v), y2: Y(v) }, svg);
      node("text", { x: W - PR + 8, y: Y(v) + 4 }, svg).textContent = tickFmt(v, kind);
    });
    var y0 = new Date(x0).getUTCFullYear(), y1 = new Date(x1).getUTCFullYear();
    var span = Math.max(1, y1 - y0), every = Math.max(1, Math.ceil(span / Math.max(2, Math.floor((W - PR) / 90))));
    for (var yr = y0 + 1; yr <= y1; yr += every) {
      var tx = X(Date.UTC(yr, 0, 1));
      if (tx < PL + 10 || tx > W - PR - 10) continue;
      node("text", { x: tx, y: H - 8, "text-anchor": "middle" }, svg).textContent = yr;
    }
    var line = pts.map(function (p, i) { return X(xs[i]).toFixed(1) + "," + Y(vs[i]).toFixed(1); }).join(" ");
    node("polygon", { points: X(xs[0]) + "," + Y(lo) + " " + line + " " + X(xs[xs.length - 1]) + "," + Y(lo),
                      fill: "url(#" + gid + ")" }, svg);
    node("polyline", { points: line, fill: "none", stroke: "var(--ok)", "stroke-width": 2,
                       "stroke-linejoin": "round", "stroke-linecap": "round" }, svg);
    pts.forEach(function (p, i) {
      node("circle", { cx: X(xs[i]), cy: Y(vs[i]), r: 2.4, fill: "var(--ok)", opacity: ".7" }, svg);
    });
    var guide = node("line", { class: "guide", y1: PT, y2: H - PB, x1: -10, x2: -10 }, svg);
    var dot = node("circle", { class: "focus", r: 5, cx: -10, cy: -10 }, svg);
    var hit = node("rect", { x: PL, y: 0, width: W - PL - PR, height: H, fill: "transparent" }, svg);

    function at(clientX) {
      var r = svg.getBoundingClientRect(), mx = clientX - r.left, best = 0, bd = Infinity;
      for (var i = 0; i < xs.length; i++) {
        var dist = Math.abs(X(xs[i]) - mx);
        if (dist < bd) { bd = dist; best = i; }
      }
      return best;
    }
    function move(e) {
      var i = at(e.clientX), x = X(xs[i]), y = Y(vs[i]);
      guide.setAttribute("x1", x); guide.setAttribute("x2", x);
      dot.setAttribute("cx", x); dot.setAttribute("cy", y);
      var change = "";
      if (i > 0 && vs[i - 1]) {
        var pct = (vs[i] - vs[i - 1]) / vs[i - 1] * 100;
        change = Math.abs(pct) < 0.5 ? "No change from the filing before"
          : '<span class="' + (pct > 0 ? "ok" : "bad") + '">' + (pct > 0 ? "+" : "") + pct.toFixed(0) +
            "%</span> from the filing before";
      }
      var r = svg.getBoundingClientRect();
      showTip("<b>" + esc(fmt(vs[i], kind)) + "</b><span>" + esc(d.what || "Filed") + " " + esc(pts[i][0]) +
              "</span>" + (change ? "<span>" + change + "</span>" : ""), r.left + x, r.top + y);
    }
    hit.addEventListener("pointerenter", function () { svg.classList.add("hover"); });
    hit.addEventListener("pointermove", move);
    hit.addEventListener("pointerdown", function (e) { svg.classList.add("hover"); move(e); });
    hit.addEventListener("pointerleave", function () {
      hideTip(); svg.classList.remove("hover");
    });
    box.replaceChildren(svg);
  }

  /* ---------------------------------------------------------- bar chart */
  function barChart(box, d) {
    var labels = d.labels || [], up = d.up || [], down = d.down || [];
    var W = box.clientWidth, H = +(box.dataset.height || 160);
    if (!W || !labels.length) return;
    var PL = 44, PR = 8, PT = 10, PB = 24;
    var top = Math.max(1, Math.max.apply(null, up.concat([0])), Math.max.apply(null, down.concat([0])));
    var mid = PT + (H - PT - PB) * (down.some(function (v) { return v; }) ? 0.58 : 1);
    var band = (W - PL - PR) / labels.length, bw = Math.min(34, band * 0.56);
    var svg = node("svg", { class: "chart live", width: W, height: H, viewBox: "0 0 " + W + " " + H,
                            role: "img", "aria-label": box.dataset.label || "Chart" });
    // The highlight sits behind the bars; one transparent surface on top of
    // everything takes the pointer, so moving over a bar, a gap or a label
    // never drops the readout (the old per-column targets sat under the bars).
    var shade = node("rect", { class: "colshade", x: -100, y: PT, width: band - 2, height: H - PT - PB, rx: 4 }, svg);
    node("line", { class: "grid", x1: PL, x2: W - PR, y1: mid, y2: mid }, svg);
    node("text", { x: 0, y: PT + 10 }, svg).textContent = d.upName || "";
    if (mid < H - PB) node("text", { x: 0, y: H - PB - 2 }, svg).textContent = d.downName || "";
    labels.forEach(function (lab, i) {
      var cx = PL + band * i + band / 2;
      var hu = (up[i] || 0) / top * (mid - PT - 4), hd = (down[i] || 0) / top * (H - PB - mid - 4);
      if (hu) node("rect", { x: cx - bw / 2, y: mid - hu, width: bw, height: hu, rx: 3, fill: "var(--ok)",
                             opacity: ".85" }, svg);
      if (hd) node("rect", { x: cx - bw / 2, y: mid + 1, width: bw, height: hd, rx: 3, fill: "var(--red-hi)",
                             opacity: ".75" }, svg);
      var short = labels.length > 9 && String(lab).length === 4 ? "'" + String(lab).slice(2) : lab;
      node("text", { x: cx, y: H - 7, "text-anchor": "middle" }, svg).textContent = short;
    });
    var hit = node("rect", { x: PL, y: 0, width: W - PL - PR, height: H, fill: "transparent" }, svg);
    var shown = -1;
    function at(e) {
      var r = svg.getBoundingClientRect();
      var i = Math.floor((e.clientX - r.left - PL) / band);
      return Math.max(0, Math.min(labels.length - 1, i));
    }
    function show(e) {
      var i = at(e);
      if (i === shown) return;
      shown = i;
      shade.setAttribute("x", PL + band * i + 1);
      var r = svg.getBoundingClientRect(), net = (up[i] || 0) - (down[i] || 0);
      showTip("<b>" + esc(labels[i]) + "</b><span>" + (up[i] || 0) + " " + esc(d.upName || "") + ", " +
              (down[i] || 0) + " " + esc(d.downName || "") + "</span><span>" +
              (net > 0 ? "Team grew by " + net : net < 0 ? "Team shrank by " + (-net) : "No change") +
              "</span>", r.left + PL + band * i + band / 2, r.top + PT);
    }
    hit.addEventListener("pointermove", show);
    hit.addEventListener("pointerdown", show);
    hit.addEventListener("pointerleave", function () {
      shown = -1; shade.setAttribute("x", -100); hideTip();
    });
    box.replaceChildren(svg);
  }

  /* ---------------------------------------------------------- wiring */
  var KINDS = { line: lineChart, bars: barChart };
  function draw(box) {
    var fn = KINDS[box.dataset.chart];
    if (!fn) return;
    try { fn(box, JSON.parse(box.dataset.series || "{}")); } catch (err) { /* keep the static chart */ }
  }
  var ro = window.ResizeObserver ? new ResizeObserver(function (entries) {
    entries.forEach(function (en) {
      var box = en.target, w = Math.round(en.contentRect.width);
      if (w && w !== +box.dataset.drawnAt) { box.dataset.drawnAt = w; draw(box); }
    });
  }) : null;
  document.querySelectorAll(".ichart[data-chart]").forEach(function (box) {
    if (ro) ro.observe(box); else draw(box);
  });
})();
