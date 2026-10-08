/* The sign-in backdrop: the United States drawn as a field of dots, with the
 * cities where advisory firms cluster pulsing softly and, now and then, a
 * faint signal travelling between two of them. It says what Bellwether
 * watches without a word, sits behind the card, and stays still for anyone
 * who prefers reduced motion. Drawn once to an offscreen canvas; each frame
 * only adds the few moving pieces, and nothing runs while the tab is hidden.
 */
(function () {
  "use strict";
  var canvas = document.getElementById("signin-map");
  if (!canvas || !canvas.getContext) return;
  var reduced = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;

  // Contiguous United States outline, longitude and latitude, clockwise from
  // Cape Flattery. Coarse on purpose: it only has to read as the country.
  var OUTLINE = [
    [-124.7, 48.4], [-123.3, 49.0], [-95.2, 49.0], [-95.2, 49.4], [-94.6, 48.7], [-91.4, 48.1],
    [-89.6, 48.0], [-88.4, 47.6], [-86.5, 46.7], [-84.8, 46.5], [-84.1, 45.9], [-83.5, 45.4],
    [-82.5, 43.6], [-82.4, 42.9], [-83.1, 42.1], [-82.7, 41.7], [-80.5, 42.1], [-79.0, 42.6],
    [-79.0, 43.3], [-76.2, 43.6], [-75.4, 44.6], [-74.8, 45.0], [-71.5, 45.0], [-70.8, 45.4],
    [-70.0, 46.7], [-69.2, 47.4], [-67.8, 47.1], [-67.8, 45.7], [-67.0, 44.8], [-68.8, 44.3],
    [-70.2, 43.6], [-70.6, 42.6], [-70.0, 41.7], [-71.4, 41.4], [-72.9, 41.2], [-73.9, 40.6],
    [-74.0, 39.6], [-75.0, 38.9], [-75.6, 37.9], [-76.0, 36.9], [-75.5, 35.2], [-76.7, 34.7],
    [-77.9, 33.9], [-79.2, 33.2], [-80.9, 32.0], [-81.4, 30.4], [-80.6, 28.4], [-80.1, 26.7],
    [-80.4, 25.2], [-81.1, 25.1], [-81.8, 26.1], [-82.6, 27.9], [-82.8, 29.2], [-84.3, 30.0],
    [-85.4, 29.7], [-87.4, 30.4], [-88.1, 30.4], [-89.6, 30.2], [-89.2, 29.2], [-90.2, 29.1],
    [-91.5, 29.5], [-93.8, 29.7], [-94.8, 29.3], [-96.5, 28.3], [-97.4, 27.3], [-97.2, 25.9],
    [-99.1, 26.4], [-99.5, 27.5], [-101.4, 29.8], [-103.1, 29.0], [-104.5, 29.6], [-106.5, 31.8],
    [-108.2, 31.8], [-111.1, 31.3], [-114.8, 32.5], [-117.1, 32.5], [-118.4, 33.8], [-120.6, 34.6],
    [-122.0, 36.9], [-122.5, 37.8], [-123.8, 39.8], [-124.4, 40.4], [-124.2, 42.0], [-124.0, 44.6],
    [-123.9, 46.2], [-124.1, 47.0]
  ];
  // Where advisory firms cluster; the larger the weight, the more often it pulses.
  var HUBS = [
    [-74.0, 40.7, 5], [-71.1, 42.4, 3], [-87.6, 41.9, 4], [-122.4, 37.8, 3], [-118.2, 34.1, 3],
    [-96.8, 32.8, 3], [-95.4, 29.8, 2], [-84.4, 33.7, 2], [-80.2, 25.8, 2], [-104.9, 39.7, 2],
    [-122.3, 47.6, 2], [-112.1, 33.4, 2], [-93.3, 44.98, 2], [-80.8, 35.2, 2], [-77.0, 38.9, 2],
    [-75.2, 39.95, 2], [-90.2, 38.6, 1], [-94.6, 39.1, 1], [-111.9, 40.8, 1], [-86.8, 36.2, 1],
    [-122.7, 45.5, 1], [-117.2, 32.7, 1], [-83.0, 42.3, 1], [-80.0, 40.4, 1], [-81.7, 41.5, 1],
    [-82.5, 27.9, 1], [-78.6, 35.8, 1], [-86.2, 39.8, 1], [-83.0, 39.96, 1], [-87.9, 43.0, 1],
    [-97.7, 30.3, 1], [-98.5, 29.4, 1], [-115.1, 36.2, 1], [-90.1, 29.95, 1], [-77.4, 37.5, 1],
    [-72.7, 41.8, 1], [-76.6, 39.3, 1], [-85.8, 38.3, 1], [-96.0, 41.3, 1], [-116.2, 43.6, 1]
  ];

  // Albers equal-area conic, the projection US maps use, so the shape is familiar.
  var R = Math.PI / 180, p1 = 29.5 * R, p2 = 45.5 * R, phi0 = 37.5 * R, lam0 = -96 * R;
  var n = (Math.sin(p1) + Math.sin(p2)) / 2, C = Math.cos(p1) * Math.cos(p1) + 2 * n * Math.sin(p1);
  var rho0 = Math.sqrt(C - 2 * n * Math.sin(phi0)) / n;
  function albers(lon, lat) {
    var rho = Math.sqrt(C - 2 * n * Math.sin(lat * R)) / n, th = n * (lon * R - lam0);
    return [rho * Math.sin(th), rho0 - rho * Math.cos(th)];
  }
  var raw = OUTLINE.map(function (p) { return albers(p[0], p[1]); });
  var minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  raw.forEach(function (p) {
    minX = Math.min(minX, p[0]); maxX = Math.max(maxX, p[0]);
    minY = Math.min(minY, p[1]); maxY = Math.max(maxY, p[1]);
  });

  function inside(x, y, poly) {
    var hit = false;
    for (var i = 0, j = poly.length - 1; i < poly.length; j = i++) {
      var xi = poly[i][0], yi = poly[i][1], xj = poly[j][0], yj = poly[j][1];
      if ((yi > y) !== (yj > y) && x < (xj - xi) * (y - yi) / (yj - yi) + xi) hit = !hit;
    }
    return hit;
  }

  var ctx = canvas.getContext("2d"), base = document.createElement("canvas"), bctx = base.getContext("2d");
  var W = 0, H = 0, dpr = 1, hubs = [], pulses = [], arcs = [], raf = 0, last = 0;

  function layout() {
    dpr = Math.min(2, window.devicePixelRatio || 1);
    W = window.innerWidth; H = window.innerHeight;
    canvas.width = base.width = Math.round(W * dpr);
    canvas.height = base.height = Math.round(H * dpr);
    canvas.style.width = W + "px"; canvas.style.height = H + "px";
    // The map fills most of the width, centred a little below the middle so
    // the coasts frame the card rather than hide behind it.
    var mapW = Math.min(W * 0.96, 1280), scale = mapW / (maxX - minX);
    var mapH = (maxY - minY) * scale;
    if (mapH > H * 0.86) { scale = H * 0.86 / (maxY - minY); mapW = (maxX - minX) * scale; mapH = H * 0.86; }
    var ox = (W - mapW) / 2, oy = (H - mapH) / 2 + H * 0.03;
    function px(p) { return [ox + (p[0] - minX) * scale, oy + (maxY - p[1]) * scale]; }
    var poly = raw.map(px);
    var step = Math.max(8, Math.min(12, mapW / 110));
    bctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    bctx.clearRect(0, 0, W, H);
    for (var y = oy; y < oy + mapH; y += step) {
      for (var x = ox; x < ox + mapW; x += step) {
        if (!inside(x, y, poly)) continue;
        // Dots fade towards the edges of the screen so the page stays calm.
        var dx = (x - W / 2) / (W / 2), dy = (y - H / 2) / (H / 2);
        var a = 0.16 - Math.min(0.09, (dx * dx + dy * dy) * 0.05);
        bctx.fillStyle = "rgba(255,255,255," + a.toFixed(3) + ")";
        bctx.beginPath(); bctx.arc(x, y, 1.15, 0, 6.2832); bctx.fill();
      }
    }
    hubs = HUBS.map(function (h) { var q = px(albers(h[0], h[1])); return { x: q[0], y: q[1], w: h[2] }; });
    hubs.forEach(function (h) {
      bctx.fillStyle = "rgba(255,255,255,.42)";
      bctx.beginPath(); bctx.arc(h.x, h.y, h.w > 2 ? 2.2 : 1.7, 0, 6.2832); bctx.fill();
    });
    paint(performance.now());
  }

  function pick() {
    var total = hubs.reduce(function (s, h) { return s + h.w; }, 0), r = Math.random() * total;
    for (var i = 0; i < hubs.length; i++) { r -= hubs[i].w; if (r <= 0) return hubs[i]; }
    return hubs[0];
  }

  function paint(t) {
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(base, 0, 0);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    pulses = pulses.filter(function (p) { return t - p.t0 < 2600; });
    pulses.forEach(function (p) {
      if (t < p.t0) return;
      var k = (t - p.t0) / 2600, r = 2 + k * 22;
      ctx.strokeStyle = "rgba(240,107,114," + (0.55 * (1 - k)).toFixed(3) + ")";
      ctx.lineWidth = 1.2;
      ctx.beginPath(); ctx.arc(p.h.x, p.h.y, r, 0, 6.2832); ctx.stroke();
      ctx.fillStyle = "rgba(240,107,114," + (0.9 * (1 - k)).toFixed(3) + ")";
      ctx.beginPath(); ctx.arc(p.h.x, p.h.y, 2.4, 0, 6.2832); ctx.fill();
    });
    arcs = arcs.filter(function (a) { return t - a.t0 < 3200; });
    arcs.forEach(function (a) {
      var k = Math.min(1, (t - a.t0) / 1800), fade = t - a.t0 > 1800 ? 1 - (t - a.t0 - 1800) / 1400 : 1;
      var mx = (a.a.x + a.b.x) / 2, my = Math.min(a.a.y, a.b.y) - Math.abs(a.a.x - a.b.x) * 0.22;
      ctx.strokeStyle = "rgba(255,255,255," + (0.28 * fade).toFixed(3) + ")";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(a.a.x, a.a.y);
      for (var s = 1; s <= 24 * k; s++) {
        var u = s / 24, x = (1 - u) * (1 - u) * a.a.x + 2 * (1 - u) * u * mx + u * u * a.b.x;
        var y = (1 - u) * (1 - u) * a.a.y + 2 * (1 - u) * u * my + u * u * a.b.y;
        ctx.lineTo(x, y);
      }
      ctx.stroke();
    });
  }

  function frame(t) {
    raf = requestAnimationFrame(frame);
    if (t - last > 520 && hubs.length) {
      last = t;
      if (Math.random() < 0.8) pulses.push({ h: pick(), t0: t });
      if (Math.random() < 0.12 && arcs.length < 1) {
        var a = pick(), b = pick();
        if (a !== b && Math.abs(a.x - b.x) > 160) { arcs.push({ a: a, b: b, t0: t }); pulses.push({ h: b, t0: t + 1700 }); }
      }
    }
    paint(t);
  }

  layout();
  var resizeT = 0;
  window.addEventListener("resize", function () { clearTimeout(resizeT); resizeT = setTimeout(layout, 120); });
  if (reduced) return;
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) cancelAnimationFrame(raf); else raf = requestAnimationFrame(frame);
  });
  raf = requestAnimationFrame(frame);
})();
