"""Visual and copy audit of a running Bellwether instance.

Signs in with Playwright, walks every page (and every in-page tab: the firm
page section bar and the dashboard workspace tabs) at several viewport widths
and runs layout checks inside the browser:

  overlap    two text-bearing elements drawn on top of each other
  spill      text running out of the box that should hold it
  overflow   the document, or an element, wider than the viewport or column
  hscroll    a scroll container that needs sideways scrolling
  clipped    text cut off by overflow hidden with no working ellipsis
  grid       a grid row that leaves empty tracks at its end (auto-fill)
  flexrow    tiles in a flex row bunched to one side (judgement call)
  tablepad   table text within 6px of the table's visible border
  hidden     text in a zero-size or off-screen element
  icon       an svg or image far larger than an icon where an icon belongs
  tinytext   text drawn smaller than about 9.5px (svg labels included)
  contrast   text whose contrast against its background is below WCAG AA
  console    console errors, page errors, failed or 4xx/5xx requests

then copy checks over the visible text: broken plurals, doubled words and
spaces, stray " . " separators, raw codes (snake_case, None, nan, null),
unformatted numbers, raw ISO timestamps, entity leaks, dashes, confusing
shorthand, inconsistent capitalisation, and spelling when pyspellchecker is
installed.

    python scripts/qa_visual.py --base http://127.0.0.1:8812 \
        --user qaadmin --password <pw> --out <dir>

Options worth knowing: --widths 1440,1100,390   --only firm/   --no-pages
--firms 167626,104559   --plain-user qa (role pass, same password unless
--plain-password)   --max-crops 4 (per check per page state).

Writes <out>/report.md (grouped findings), <out>/findings.json, full-page
shots in <out>/pages/ and one crop per finding in <out>/crops/. It only
reads; it never changes data, though opening a firm page may queue the
same background refreshes a person opening it would.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright

DEFAULT_FIRMS = ["167626", "104559", "111334", "168595", "282244", "105215", "288566", "319334"]

# ---------------------------------------------------------------- browser side
AUDIT_JS = r"""
(opt) => {
  const W = document.documentElement.clientWidth;
  const SX = window.scrollX, SY = window.scrollY;
  const out = [];
  const csCache = new Map();
  const cs = (el) => { let s = csCache.get(el); if (!s) { s = getComputedStyle(el); csCache.set(el, s); } return s; };
  const R = (l, t, r, b) => ({left: l, top: t, right: r, bottom: b, width: r - l, height: b - t});
  const BIG = R(-1e9, -1e9, 1e9, 1e9);
  const inter = (a, b) => R(Math.max(a.left, b.left), Math.max(a.top, b.top), Math.min(a.right, b.right), Math.min(a.bottom, b.bottom));
  const union = (a, b) => R(Math.min(a.left, b.left), Math.min(a.top, b.top), Math.max(a.right, b.right), Math.max(a.bottom, b.bottom));
  function sel(el) {
    if (!el || el.nodeType !== 1) return '';
    const parts = [];
    let e = el;
    for (let i = 0; i < 4 && e && e.nodeType === 1 && e !== document.body; i++) {
      let s = e.tagName.toLowerCase();
      if (e.id && !/^(p-|vs\d|tab-|workspace-tab-)/.test(e.id)) { parts.unshift(s + '#' + e.id); break; }
      const cls = Array.from(e.classList).filter(c => !/^(on|hide|go)$/.test(c)).slice(0, 2);
      if (cls.length) s += '.' + cls.join('.');
      parts.unshift(s);
      e = e.parentElement;
    }
    return parts.join(' > ');
  }
  const add = (check, el, rect, text, detail, extra) => {
    const f = {check, sel: sel(el), text: String(text || '').replace(/\s+/g, ' ').trim().slice(0, 160), detail: detail || ''};
    if (rect) f.rect = {x: Math.round(rect.left + SX), y: Math.round(rect.top + SY), w: Math.round(rect.width), h: Math.round(rect.height)};
    if (extra) Object.assign(f, extra);
    out.push(f);
  };
  const visCache = new Map();
  function visible(el) {
    if (visCache.has(el)) return visCache.get(el);
    let v = el.checkVisibility ? el.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}) : !!el.getClientRects().length;
    if (v && el.closest('.sr-only')) v = false;
    visCache.set(el, v);
    return v;
  }
  const px = (v) => parseFloat(v) || 0;
  function padBox(el) {
    const r = el.getBoundingClientRect(), s = cs(el);
    return R(r.left + px(s.borderLeftWidth), r.top + px(s.borderTopWidth), r.right - px(s.borderRightWidth), r.bottom - px(s.borderBottomWidth));
  }
  function contentBox(el) {
    const b = padBox(el), s = cs(el);
    return R(b.left + px(s.paddingLeft), b.top + px(s.paddingTop), b.right - px(s.paddingRight), b.bottom - px(s.paddingBottom));
  }
  // Clipping applied to an element's own box by its ancestors.
  // all: every clipping ancestor; hidden: only overflow hidden/clip (not scroll containers).
  const clipCache = new Map();
  function clipOf(el) {
    if (clipCache.has(el)) return clipCache.get(el);
    let res = {all: BIG, hidden: BIG, clipper: null, scroller: null};
    const p = el.parentElement;
    if (p && p !== document.documentElement && p !== document.body) {
      const up = clipOf(p), s = cs(p);
      res = Object.assign({}, up);
      if (s.overflowX !== 'visible' || s.overflowY !== 'visible') {
        const pb = padBox(p);
        const scrolls = /auto|scroll/.test(s.overflowX + ' ' + s.overflowY);
        res = {all: inter(up.all, pb), hidden: scrolls ? up.hidden : inter(up.hidden, pb),
               clipper: scrolls ? up.clipper : p, scroller: scrolls ? p : up.scroller};
      }
    }
    if (cs(el).position === 'fixed') res = {all: BIG, hidden: BIG, clipper: null, scroller: null};
    clipCache.set(el, res);
    return res;
  }
  // Clip applied to text that lives directly in el (its ancestors plus el itself).
  function textClip(el) {
    const c = clipOf(el), s = cs(el);
    if (s.overflowX === 'visible' && s.overflowY === 'visible') return c;
    const pb = padBox(el);
    const scrolls = /auto|scroll/.test(s.overflowX + ' ' + s.overflowY);
    return {all: inter(c.all, pb), hidden: scrolls ? c.hidden : inter(c.hidden, pb),
            clipper: scrolls ? c.clipper : el, scroller: scrolls ? el : c.scroller};
  }
  function blockOf(el) {
    let e = el;
    while (e && e !== document.body) {
      const d = cs(e).display;
      if (d !== 'inline' && d !== 'contents') return e;
      e = e.parentElement;
    }
    return document.body;
  }
  const BLOCKY = ['block', 'inline-block', 'list-item', 'table-cell', 'flow-root'];
  function rgba(str) {
    const m = String(str).match(/rgba?\(([^)]+)\)/);
    if (!m) return [0, 0, 0, 0];
    const p = m[1].split(/[ ,\/]+/).filter(Boolean).map(Number);
    return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1];
  }
  function over(top, bot) {
    const a = top[3] + bot[3] * (1 - top[3]);
    if (a <= 0) return [0, 0, 0, 0];
    return [0, 1, 2].map(i => (top[i] * top[3] + bot[i] * bot[3] * (1 - top[3])) / a).concat([a]);
  }
  function effBg(el) {
    const layers = [];
    let e = el, img = false;
    while (e && e.nodeType === 1) {
      const s = cs(e);
      if (s.backgroundImage && s.backgroundImage !== 'none' && !/^url\(.*\.svg/.test(s.backgroundImage) && e !== el.ownerDocument.body) img = true;
      const c = rgba(s.backgroundColor);
      if (c[3] > 0) { layers.push(c); if (c[3] >= 0.99) break; }
      e = e.parentElement;
    }
    let col = [21, 21, 21, 1];
    for (let i = layers.length - 1; i >= 0; i--) col = over(layers[i], col);
    return {col, img};
  }
  const lum = (c) => { const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }; return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]); };
  const ratio = (a, b) => { const x = lum(a), y = lum(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); };

  const SKIP = 'script,style,noscript,template,select,option,textarea,title,head,datalist';
  const items = [];
  const seenClip = new Set(), seenSpill = new Set(), seenContrast = new Set(), seenHidden = new Set(), seenTiny = new Set();
  const mainEl = document.querySelector('.pg') || document.querySelector('main') || document.body;

  // ------------------------------------------------ text nodes
  const tw = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let n;
  while ((n = tw.nextNode())) {
    const t = n.nodeValue;
    if (!t || !t.trim()) continue;
    const pe = n.parentElement;
    if (!pe || pe.closest(SKIP)) continue;
    if (!visible(pe)) continue;
    const rg = document.createRange();
    rg.selectNodeContents(n);
    const rects = Array.from(rg.getClientRects()).filter(r => r.width > 0.5 && r.height > 0.5);
    const tc = textClip(pe);
    if (!rects.length) continue;
    const blk = blockOf(pe), bs = cs(blk);
    let lineClamp = false;
    for (let e = pe; e && e !== document.body; e = e.parentElement) {
      const lc = cs(e).webkitLineClamp;
      if (lc && lc !== 'none') { lineClamp = true; break; }
      if (e === tc.clipper) break;
    }
    const ellipsis = bs.textOverflow === 'ellipsis' && BLOCKY.includes(bs.display) && bs.overflowX !== 'visible';
    let tu = null;
    for (const r of rects) {
      tu = tu ? union(tu, r) : R(r.left, r.top, r.right, r.bottom);
      const shrunk = R(r.left, r.top + r.height * 0.18, r.right, r.bottom - r.height * 0.18);
      const vis = inter(shrunk, tc.all);
      if (vis.width > 1 && vis.height > 1) items.push({r: vis, owner: pe, text: t.trim(), kind: 'text'});
      // clipped text
      const hv = inter(r, tc.hidden);
      const cutX = r.width - Math.max(0, hv.width), cutY = r.height - Math.max(0, hv.height);
      if (tc.clipper && (cutX > 2 || (cutY > 3 && !lineClamp)) && !(ellipsis && cutY <= 3) && !seenClip.has(tc.clipper)) {
        seenClip.add(tc.clipper);
        let why = 'no text-overflow ellipsis';
        if (bs.textOverflow === 'ellipsis' && !BLOCKY.includes(bs.display)) why = 'text-overflow:ellipsis is set but has no effect on display:' + bs.display;
        else if (bs.textOverflow === 'ellipsis' && bs.overflowX === 'visible') why = 'ellipsis is on ' + sel(blk) + ' but the clipping happens on an ancestor';
        if (cutY > 3 && cutX <= 2) why = 'cut vertically (' + Math.round(cutY) + 'px), likely a fixed height';
        add('clipped', tc.clipper, tc.clipper.getBoundingClientRect(), t, `text cut by ${Math.round(Math.max(cutX, cutY))}px; ${why}`);
      }
    }
    // spill: text running outside its own block when that block does not clip
    if (tu && blk !== document.body && bs.overflowX === 'visible' && bs.overflowY === 'visible' && !seenSpill.has(blk)) {
      const bb = blk.getBoundingClientRect();
      const dx = Math.max(tu.right - bb.right, bb.left - tu.left);
      const vis = inter(tu, tc.all);
      if (dx > 2 && vis.width > 1 && bb.width > 0) {
        seenSpill.add(blk);
        add('spill', blk, union(bb, tu), t, `text runs ${Math.round(dx)}px outside ${sel(blk)} (box ${Math.round(bb.width)}px wide, display:${bs.display}, white-space:${bs.whiteSpace})`);
      }
    }
    // off-screen text
    if (tu && !tc.scroller && (tu.right < 0 || tu.bottom + SY < 0 || tu.left + SX > document.documentElement.scrollWidth + 5) && !seenHidden.has(pe)) {
      seenHidden.add(pe);
      add('hidden', pe, tu, t, 'visible text positioned off-screen' + (/^skip/i.test(t.trim()) ? ' (looks like a skip link; fine if it shows on focus)' : ''));
    }
    // zero-size owner holding text that is clipped away
    const pr = pe.getBoundingClientRect();
    if ((pr.width < 1 || pr.height < 1) && cs(pe).display !== 'inline' && cs(pe).display !== 'contents' && !seenHidden.has(pe)) {
      seenHidden.add(pe);
      add('hidden', pe, tu, t, `text inside a ${Math.round(pr.width)}x${Math.round(pr.height)} box`);
    }
    // tiny text: svg labels scale with their viewBox, so measure what is drawn
    if (tu && !seenTiny.has(pe)) {
      const inSvg = !!pe.closest('svg');
      const drawn = inSvg ? tu.height / 1.2 : px(cs(pe).fontSize);
      if (drawn > 0 && drawn < 9.5) {
        seenTiny.add(pe);
        add('tinytext', pe, tu, t, `text drawn at about ${drawn.toFixed(1)}px${inSvg ? ' (svg label scaled down with its viewBox)' : ''}`);
      }
    }
    // contrast
    const key = pe;
    if (!seenContrast.has(key) && !pe.closest('button:disabled,[aria-disabled=true],.chart')) {
      seenContrast.add(key);
      const s = cs(pe);
      let fg = rgba(s.color);
      let op = 1;
      for (let e = pe; e && e.nodeType === 1; e = e.parentElement) op *= parseFloat(cs(e).opacity);
      const bg = effBg(pe);
      if (!bg.img) {
        fg = over([fg[0], fg[1], fg[2], fg[3] * op], bg.col);
        const cr = ratio(fg, bg.col);
        const size = px(s.fontSize), wt = parseInt(s.fontWeight, 10) || 400;
        const large = size >= 24 || (size >= 18.66 && wt >= 700);
        const need = large ? 3 : 4.5;
        if (cr < need - 0.01) add('contrast', pe, tu, t, `contrast ${cr.toFixed(2)}:1, needs ${need}:1 (${size}px, weight ${wt}, color ${s.color}${op < 1 ? ', opacity ' + op.toFixed(2) : ''})`, {ratio: +cr.toFixed(2)});
      }
    }
  }

  // ------------------------------------------------ box items (chips, controls)
  document.querySelectorAll('.chip,.pill,.ftype,.cnt,.missing,kbd,.fchip,.badge,button,.btn,input:not([type=hidden]),select,textarea,img,.av,.mono-av').forEach(el => {
    if (!visible(el)) return;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return;
    const v = inter(R(r.left, r.top, r.right, r.bottom), clipOf(el).all);
    if (v.width > 1 && v.height > 1) items.push({r: v, owner: el, text: (el.innerText || el.value || el.getAttribute('aria-label') || el.alt || el.tagName).trim(), kind: 'box'});
  });

  // ------------------------------------------------ overlaps
  const BAND = 48, buckets = new Map();
  items.forEach((it, i) => {
    for (let b = Math.floor(it.r.top / BAND); b <= Math.floor(it.r.bottom / BAND); b++) {
      if (!buckets.has(b)) buckets.set(b, []);
      buckets.get(b).push(i);
    }
  });
  // Overlays (sticky bars, popups, fixed headers) float over content on purpose:
  // only compare items that share the same stacking layer.
  const layerCache = new Map();
  function layerOf(el) {
    if (layerCache.has(el)) return layerCache.get(el);
    let res = null;
    for (let e = el; e && e !== document.body; e = e.parentElement) {
      const s = cs(e);
      if (s.position === 'fixed' || s.position === 'sticky' || (s.position === 'absolute' && s.zIndex !== 'auto')) { res = e; break; }
    }
    layerCache.set(el, res);
    return res;
  }
  const seenPair = new Set();
  let nOver = 0;
  for (const list of buckets.values()) {
    for (let x = 0; x < list.length; x++) {
      for (let y = x + 1; y < list.length; y++) {
        const a = items[list[x]], b = items[list[y]];
        if (a.owner === b.owner || a.owner.contains(b.owner) || b.owner.contains(a.owner)) continue;
        if (layerOf(a.owner) !== layerOf(b.owner)) continue;
        const ov = inter(a.r, b.r);
        if (ov.width <= 2 || ov.height <= 2) continue;
        const pk = list[x] < list[y] ? list[x] + ':' + list[y] : list[y] + ':' + list[x];
        const ok2 = sel(a.owner) + '|' + sel(b.owner);
        if (seenPair.has(pk) || seenPair.has(ok2)) continue;
        seenPair.add(pk); seenPair.add(ok2);
        if (++nOver > 60) break;
        add('overlap', a.owner, union(a.r, b.r), a.text + '  <->  ' + b.text,
            `"${a.text.slice(0, 40)}" (${sel(a.owner)}) overlaps "${b.text.slice(0, 40)}" (${sel(b.owner)}) by ${Math.round(ov.width)}x${Math.round(ov.height)}px`,
            {other: sel(b.owner)});
      }
    }
  }

  // ------------------------------------------------ element pass
  const all = Array.from(document.body.querySelectorAll('*'));
  const docW = document.documentElement.scrollWidth;
  if (docW > W + 1) add('overflow', document.documentElement, R(0, 0, docW, 40), '', `document is ${docW}px wide in a ${W}px viewport (page scrolls sideways)`);
  let colBox = null;
  if (mainEl && mainEl !== document.body) {
    const mb = contentBox(mainEl);
    colBox = mb;
  }
  const seenOverflow = new Set();
  let nOverflow = 0;
  for (const el of all) {
    if (el.closest(SKIP) || el.closest('svg') && el.tagName.toLowerCase() !== 'svg') continue;
    if (!visible(el)) continue;
    const s = cs(el);
    if (s.display === 'contents') continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) continue;
    const clip = clipOf(el);
    // overflow past viewport or content column (report the outermost element)
    if ((s.position === 'fixed' || (s.position === 'absolute' && s.zIndex !== 'auto')) && r.width > 40 && r.height > 20 && el.tagName.toLowerCase() !== 'nav') {
      const H = window.innerHeight;
      if (r.right > W + 1 || r.left < -1) add('overflow', el, r, (el.innerText || '').slice(0, 60), `overlay (${s.position}) runs ${Math.round(Math.max(r.right - W, -r.left))}px outside the viewport`);
      if (s.position === 'fixed' && r.bottom > H + 1 && !/auto|scroll/.test(s.overflowY)) add('overflow', el, r, (el.innerText || '').slice(0, 60), `fixed panel is ${Math.round(r.height)}px tall in a ${H}px viewport and does not scroll; ${Math.round(r.bottom - H)}px cannot be reached`);
    }
    if (nOverflow < 25 && s.position !== 'fixed') {
      const vis = inter(R(r.left, r.top, r.right, r.bottom), clip.all);
      const p = el.parentElement;
      const pr = p ? p.getBoundingClientRect() : null;
      if (vis.width > 0 && vis.right > W + 1 && pr && pr.right <= W + 1) {
        nOverflow++;
        add('overflow', el, r, el.innerText || '', `extends ${Math.round(vis.right - W)}px past the right edge of the viewport`);
      } else if (colBox && mainEl.contains(el) && el !== mainEl && vis.width > 0 && s.position !== 'absolute') {
        const pin = p && p !== mainEl ? p.getBoundingClientRect() : null;
        const parentInside = !pin || (pin.right <= colBox.right + 2 && pin.left >= colBox.left - 2);
        if (parentInside && (vis.right > colBox.right + 2 || vis.left < colBox.left - 2) && !seenOverflow.has(el)) {
          seenOverflow.add(el); nOverflow++;
          const d = vis.right > colBox.right + 2 ? Math.round(vis.right - colBox.right) + 'px past the right' : Math.round(colBox.left - vis.left) + 'px past the left';
          add('overflow', el, r, el.innerText || '', `extends ${d} edge of the content column (${sel(mainEl)})`);
        }
      }
    }
    // scroll containers needing sideways scroll
    if (/auto|scroll/.test(s.overflowX) && el.scrollWidth > el.clientWidth + 2 && el.clientWidth > 0) {
      add('hscroll', el, r, (el.innerText || '').slice(0, 60), `content ${el.scrollWidth}px wide in a ${el.clientWidth}px box; ${el.scrollWidth - el.clientWidth}px hidden until scrolled sideways`, {hidden_px: el.scrollWidth - el.clientWidth});
    }
    // grids
    if (/grid/.test(s.display)) {
      const kids = [];
      const collect = (p) => { for (const c of p.children) { const cc = cs(c); if (cc.display === 'none' || cc.position === 'absolute' || cc.position === 'fixed') continue; if (cc.display === 'contents') { collect(c); continue; } if (!visible(c)) continue; kids.push(c); } };
      collect(el);
      const tracks = s.gridTemplateColumns.split(' ').map(parseFloat).filter(v => !isNaN(v));
      const cb = contentBox(el);
      if (kids.length && tracks.length >= 2 && cb.width >= 200) {
        const rows = [];
        kids.map(k => k.getBoundingClientRect()).sort((p, q) => p.top - q.top).forEach(kr => {
          const cy = (kr.top + kr.bottom) / 2;
          let row = rows.find(rw => (cy >= rw.top - 1 && cy <= rw.bottom + 1) || (rw.cy >= kr.top - 1 && rw.cy <= kr.bottom + 1));
          if (!row) { row = {top: kr.top, bottom: kr.bottom, cy, right: -1e9, n: 0}; rows.push(row); }
          row.right = Math.max(row.right, kr.right); row.n++;
        });
        rows.sort((a, b) => a.top - b.top);
        const bg = rgba(s.backgroundColor), bgVisible = bg[3] > 0.02;
        const first = rows[0], empty = cb.right - first.right;
        const gap = px(s.columnGap);
        const emptyTracks = tracks.filter((w, i) => { let x = cb.left + tracks.slice(0, i).reduce((a, v) => a + v + gap, 0); return x >= first.right - 1 && w > 0; }).length;
        if (rows.length === 1 && empty > 40) {
          add('grid', el, r, (kids[0].innerText || '').slice(0, 60), `${kids.length} item(s) in ${tracks.length} column tracks (${s.gridTemplateColumns.slice(0, 80)}); ${emptyTracks} empty track(s), ${Math.round(empty)}px unused at the end of the row${bgVisible ? '; the grid background shows through as a grey block' : ''}`, {empty_px: Math.round(empty)});
        } else if (rows.length > 1) {
          const last = rows[rows.length - 1], e2 = cb.right - last.right;
          if (e2 > 40 && (bgVisible || kids.length <= 6)) add('grid', el, r, (kids[0].innerText || '').slice(0, 60), `last row has ${last.n} of ${first.n} items; ${Math.round(e2)}px unused${bgVisible ? ' and the grid background shows through as a grey block' : ''} (judgement call)`, {empty_px: Math.round(e2), judgement: !bgVisible});
        }
      }
    }
    // flex rows of tiles bunched to one side
    if (/flex/.test(s.display) && !/column/.test(s.flexDirection) && /normal|flex-start|start|left/.test(s.justifyContent)) {
      const kids = Array.from(el.children).filter(c => visible(c) && cs(c).position !== 'absolute');
      const cb = contentBox(el);
      if (kids.length >= 2 && cb.width >= 500) {
        const rs = kids.map(c => c.getBoundingClientRect());
        const oneLine = rs.every(k => Math.abs(k.top - rs[0].top) < 4);
        const tiles = kids.every((c, i) => { const cc = cs(c); return rs[i].height >= 44 && (rgba(cc.backgroundColor)[3] > 0.02 || px(cc.borderLeftWidth) > 0 || px(cc.borderTopWidth) > 0); });
        if (oneLine && tiles) {
          const used = Math.max(...rs.map(k => k.right)) - cb.left;
          if (used < cb.width * 0.65) add('flexrow', el, r, (kids[0].innerText || '').slice(0, 60), `${kids.length} tiles use ${Math.round(used)}px of a ${Math.round(cb.width)}px row (${Math.round(100 - used * 100 / cb.width)}% empty on the right; judgement call)`, {judgement: true});
        }
      }
    }
    // icons
    const tag = el.tagName.toLowerCase();
    if ((tag === 'svg' || tag === 'img') && !el.closest('.chart,.spark,.usmap,.signin-emblem,.ring')) {
      const host = el.closest('button,.btn,.pill,.chip,.fchip,.ftype,nav a,.seg a,.tabs a,.secnav a,.cline,a.i,.missing,kbd,summary,.crumb,.sub,label,.find,.hint,.legend');
      if (host && (r.width > 40 || r.height > 40)) add('icon', el, r, host.innerText || '', `${tag} renders ${Math.round(r.width)}x${Math.round(r.height)}px inside ${sel(host)}`);
      else if (!host && tag === 'svg' && (r.width > 480 || r.height > 320) && !el.getAttribute('viewBox')?.match(/^0 0 (\d{3,})/)) add('icon', el, r, '', `unsized svg renders ${Math.round(r.width)}x${Math.round(r.height)}px`);
      if (tag === 'img' && el.complete && el.naturalWidth === 0) add('icon', el, r, el.alt || el.src, 'image failed to load');
    }
  }

  // ------------------------------------------------ table padding
  const tables = Array.from(document.querySelectorAll('table')).filter(visible);
  for (const tb of tables) {
    let B = null;
    for (let e = tb, i = 0; e && i < 5 && e !== mainEl && e !== document.body; e = e.parentElement, i++) {
      const s = cs(e);
      const bl = px(s.borderLeftWidth) > 0 && rgba(s.borderLeftColor)[3] > 0.03 && s.borderLeftStyle !== 'none';
      const bgc = rgba(s.backgroundColor);
      let bgv = false;
      if (bgc[3] > 0.02 && e.parentElement) {
        const pb = effBg(e.parentElement).col, mine = over(bgc, pb);
        bgv = Math.abs(mine[0] - pb[0]) + Math.abs(mine[1] - pb[1]) + Math.abs(mine[2] - pb[2]) > 6;
      }
      if (bl || bgv) { B = e; break; }
    }
    if (!B) continue;
    const bb = padBox(B);
    let left = 0, right = 0, worstL = 99, worstR = 99, sample = null, sampleR = null;
    const rows = Array.from(tb.rows).slice(0, 40);
    for (const tr of rows) {
      const cells = Array.from(tr.cells).filter(c => visible(c) && (c.innerText || '').trim() || c.querySelector('img,svg,input,select,button,.sbar'));
      if (!cells.length) continue;
      const c0 = cells[0], cl = cells[cells.length - 1];
      const r0 = c0.getBoundingClientRect(), s0 = cs(c0);
      const startX = r0.left + px(s0.borderLeftWidth) + px(s0.paddingLeft);
      const dl = startX - bb.left;
      if (r0.left - bb.left < 40 && dl < 6) { left++; if (dl < worstL) { worstL = dl; sample = c0; } }
      const r1 = cl.getBoundingClientRect(), s1 = cs(cl);
      const endX = r1.right - px(s1.borderRightWidth) - px(s1.paddingRight);
      const dr = bb.right - endX;
      if (bb.right - r1.right < 40 && dr < 6 && Math.abs(tb.getBoundingClientRect().right - bb.right) < 3 && dr > -3) { right++; if (dr < worstR) { worstR = dr; sampleR = cl; } }
    }
    if (left) add('tablepad', sample, sample.getBoundingClientRect(), sample.innerText, `${left} row(s): first-column text starts ${Math.round(worstL)}px from the visible edge of ${sel(B)} (cell padding-left ${cs(sample).paddingLeft})`);
    if (right) add('tablepad', sampleR, sampleR.getBoundingClientRect(), sampleR.innerText, `${right} row(s): last-column text ends ${Math.round(worstR)}px from the visible edge of ${sel(B)} (cell padding-right ${cs(sampleR).paddingRight})`);
  }

  // ------------------------------------------------ copy material
  const caps = [];
  document.querySelectorAll('h1,h2,h3,h4,button,.btn,th,.tabs a,.secnav a,.seg a,summary,.kpi .l,.facts .l,nav.side a.i,.workspace-tabs button,label,dt,.l,.eyebrow,.chip,.pill,.ftype').forEach(el => {
    if (!visible(el)) return;
    let t = '';
    for (const c of el.childNodes) if (c.nodeType === 3) t += c.nodeValue;
    t = t.replace(/\s+/g, ' ').trim();
    if (!t) t = (el.innerText || '').split('\n')[0].trim();
    if (t && t.length <= 70) caps.push({kind: el.tagName.toLowerCase() + (el.classList[0] ? '.' + el.classList[0] : ''), text: t, sel: sel(el)});
  });
  const textRoot = document.querySelector('main') || document.body;
  return {findings: out, caps, text: textRoot.innerText, side: (document.querySelector('nav.side') || {}).innerText || '',
          docW, W, H: document.documentElement.scrollHeight};
}
"""

LOCATE_JS = r"""
(needle) => {
  const norm = (s) => (s || '').replace(/\s+/g, ' ');
  let el = document.querySelector('main') || document.body;
  if (!norm(el.innerText).includes(needle)) el = document.body;
  if (!norm(el.innerText).includes(needle)) return null;
  for (;;) {
    let next = null;
    for (const c of el.children) {
      if (c.offsetParent === null && getComputedStyle(c).position !== 'fixed') continue;
      if (norm(c.innerText).includes(needle)) { next = c; break; }
    }
    if (!next) break;
    el = next;
  }
  const r = el.getBoundingClientRect();
  const parts = [];
  let e = el;
  for (let i = 0; i < 4 && e && e !== document.body; i++) {
    let s = e.tagName.toLowerCase();
    if (e.id) { parts.unshift(s + '#' + e.id); break; }
    const cls = Array.from(e.classList).slice(0, 2);
    if (cls.length) s += '.' + cls.join('.');
    parts.unshift(s);
    e = e.parentElement;
  }
  return {sel: parts.join(' > '), rect: {x: r.left + scrollX, y: r.top + scrollY, w: r.width, h: r.height}};
}
"""

# ---------------------------------------------------------------- copy checks
COUNT_NOUNS = ("firm|person|contact|signal|email|fund|amendment|filing|hire|departure|page|result|match|"
               "list|user|job|office|employee|client|rep|adviser|advisor|year|month|week|day|hour|minute|"
               "record|item|source|website|domain|address|phone|change|factor|product|row|view|note|"
               "custodian|officer|address|run|task|attempt|guess|lead|search|query|holding|brochure")
NOT_PLURAL = {"is", "was", "has", "this", "its", "yes", "plus", "less", "class", "access", "address",
              "business", "status", "process", "news", "series", "across", "gross", "bonus", "analysis",
              "basis", "alias", "canvas", "minus", "campus", "thus", "does", "goes", "means", "us", "as",
              "always", "miss", "pass", "loss", "boss", "express", "progress", "success", "unless",
              "focus", "census", "nexus", "consensus", "chassis", "lens", "atlas", "versus", "bus"}
PLURAL_ONLY = {"people", "children", "men", "women", "persons"}
LEGIT_NONE = re.compile(r"\bNone (on|listed|reported|found|yet|recorded|of|in|known|set|filed|scheduled|"
                        r"available|so|today|this|matched|are|were|is|have|has|on file)\b")
RAW_SNAKE = re.compile(r"(?<![\w@./:=-])([a-z][a-z0-9]*_[a-z0-9_]*[a-z0-9])(?![\w@/-]|\.[a-z]{2,})")
ISO_TS = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}|\s\d{2}:\d{2}:\d{2})(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?")
TZ_OFF = re.compile(r"[+-]00:00\b")
LONG_NUM = re.compile(r"(?<![\w,.\-/:#])(\$?)(\d{5,})(?![\w\-/:])")
FOUR_NUM = re.compile(r"(?<![\w,.\-/:#+$])(\d{4})\s+(firms|people|contacts|signals|emails|funds|rows|results|"
                      r"matches|records|advisers|reps|employees|clients|offices|filings|minutes|hours|days|pages|addresses)\b")
ID_CONTEXT = re.compile(r"(CRD|CIK|SEC|ZIP|IARD|801-|ID|No\.|number|#|Zip|postal|Postal|filing|Filing|"
                        r"accession|Accession|Form D|file)\W{0,3}$")
ZIP_CONTEXT = re.compile(r"\b[A-Z]{2}\s*$")
ENTITY = re.compile(r"&(?:amp|lt|gt|quot|middot|nbsp|rsquo|ldquo|rdquo|#\d+);")
DOUBLED = re.compile(r"\b([A-Za-z]{2,})\s+\1\b", re.I)
TIME_ODD = re.compile(r"(?:\bnext\b[^.;]{0,20}\bago\b)|(?:\b\d+\.\d+ (?:days?|hours?) ago\b)|(?:\bdue at \d+\.\d+\b)|(?:\b\d{4,} minutes\b)")
SHORTHAND = re.compile(r"(?:[+\u2212-]\d+\s+[+\u2212-]\d+)|(?:\bin\s+\d+\s?(?:m|mo|d|w|y|yr|h)\b)|(?:\b\d+(?:mo|yr|wk)\b)")
STRAY_DOT = re.compile(r"(?:\S\s\.\s\S)|(?:^\.\s)|(?:\s\.$)")
SEP_EDGE = re.compile(r"(?:^\s*[\u00b7,;]\s)|(?:\s[\u00b7,;]\s*$)|(?:\u00b7\s*\u00b7)|(?:,\s*,)|(?:\s,(?=\s|$))")
EMPTY_PAREN = re.compile(r"\(\s*\)|\[\s*\]|\{\s*\}|\[\'|\'\]")
NULLISH = re.compile(r"(?:^|[\s:(,$])(None|nan|NaN|null|undefined|True|False)(?=$|[\s),.%:;])")
DASHES = re.compile(r"[\u2013\u2014]")
PROPER = {"Bellwether", "AI", "SEC", "ADV", "CRD", "LinkedIn", "Microsoft", "Google", "Schwab", "Fidelity",
          "Glynac", "AcuBooth", "PHH", "CFP", "CFA", "Form", "Schedule", "Part", "Item", "IAPD", "EDGAR",
          "RIA", "RIAs", "AUM", "HNW", "13F", "CSV", "Excel", "XLSX", "SMTP", "MX", "API", "IAR", "IARs",
          "Workspace", "Office", "365", "Outlook", "Gmail", "Pershing", "Ameritrade", "TD", "LPL", "Ctrl",
          "K", "Reacher", "Anthropic", "Claude", "US", "U.S.", "ID", "CCO", "CEO", "CIO", "COO", "CFO",
          "Brave", "DuckDuckGo", "Bing", "Exa", "Tavily", "OpenAI", "I", "OK", "DST", "JV", "1031", "D"}
DOMAIN_WORDS = set("""bellwether crd adv raum aum ria rias hnw linkedin schwab custodian custodians iar iars cfp cfa
csv xlsx ai ok sec edgar iapd brochure brochures glynac acubooth phh smtp mx api json dsn url urls
oauth msal entra admin admins signin login logout username usernames webhook dedupe deduped
email emails emailed inbox mailbox catchall roster rosters prefetch autopilot crawl crawled crawler
crawlers enrich enriched enrichment scraper scrape scraped scraping websites dossier dossiers
rescore rescored rescoring rescores scorer workspace workspaces backfill backfilled reachable
unscored unverified verifier unreadable dropdown checkbox shortlist shortlisted lookups toggles
dst jv llc lp inc ltd covid fintech wealthtech proptech multifamily reit reits nnn sma smas uma umas
etf etfs ipo ira iras erisa hnwi ultra cpa cpas jd mba cima chfc clu ricp crpc aif aifa cfs
""".split())


def plural_issues(cell: str):
    for m in re.finditer(r"(?<![\d.,$#\w])1\s+([A-Za-z]+)\b", cell):
        w = m.group(1)
        lw = w.lower()
        if lw in PLURAL_ONLY or (lw.endswith("s") and len(lw) > 3 and lw not in NOT_PLURAL
                                 and not lw.endswith(("ss", "us", "is", "ous", "ies's"))):
            yield m.group(0), "count of 1 with a plural noun"
    for m in re.finditer(r"(?<![\d.,$#\w])(?:[2-9]|[1-9]\d+|\d{1,3}(?:,\d{3})+)\s+(" + COUNT_NOUNS + r")\b(?!s|\w)", cell):
        yield m.group(0), "count above 1 with a singular noun"


def copy_checks(text: str) -> list[tuple[str, str, str]]:
    """Return (kind, snippet, detail) for each copy problem in visible text."""
    found = []
    for raw in text.split("\n"):
        for cell in raw.split("\t"):
            line = cell.strip()
            if not line:
                continue
            for snip, why in plural_issues(line):
                found.append(("plural", snip, why))
            for m in DOUBLED.finditer(line):
                if m.group(1).isdigit():
                    continue
                found.append(("doubled-word", m.group(0), "same word twice in a row"))
            if "  " in line.replace("\u00a0", " "):
                i = line.replace("\u00a0", " ").index("  ")
                found.append(("double-space", line[max(0, i - 25):i + 25], "two spaces in a row"))
            for m in STRAY_DOT.finditer(line):
                i = m.start()
                found.append(("separator", line[max(0, i - 30):i + 30], "stray ' . ' used as a separator"))
            for m in SEP_EDGE.finditer(line):
                i = m.start()
                found.append(("separator", line[max(0, i - 30):i + 30].strip() or line[:60], "separator at the start or end, doubled, or space before comma"))
            for m in RAW_SNAKE.finditer(line):
                tok = m.group(1)
                ctx = line[max(0, m.start() - 20):m.end() + 20]
                if "@" in ctx or "http" in ctx or ".com" in ctx:
                    continue
                found.append(("raw-code", tok, "snake_case key shown to the user: " + ctx))
            for m in NULLISH.finditer(line):
                after = line[m.end(1):m.end(1) + 30]
                if m.group(1) == "None" and LEGIT_NONE.search(line[m.start(1):m.start(1) + 30]):
                    continue
                if m.group(1) in ("True", "False") and not re.match(r"\s*($|[,.)])", after):
                    continue
                found.append(("raw-code", line[max(0, m.start(1) - 25):m.end(1) + 15], f"raw '{m.group(1)}' shown to the user"))
            for m in EMPTY_PAREN.finditer(line):
                found.append(("raw-code", line[max(0, m.start() - 25):m.end() + 15], "empty brackets or a Python list repr"))
            for m in ISO_TS.finditer(line):
                found.append(("timestamp", m.group(0), "raw timestamp (T, seconds or UTC offset)"))
            if TZ_OFF.search(line) and not ISO_TS.search(line):
                found.append(("timestamp", line[:80], "raw UTC offset"))
            for m in LONG_NUM.finditer(line):
                before = line[:m.start()]
                if ID_CONTEXT.search(before[-14:]) or (ZIP_CONTEXT.search(before[-4:]) and len(m.group(2)) in (5, 9)):
                    continue
                if re.match(r"-\d{4}\b", line[m.end():m.end() + 5]):
                    continue
                if len(m.group(2)) == 5 and not m.group(1) and re.search(r"\b[A-Z]{2}\b", before[-6:] or ""):
                    continue
                found.append(("number", line[max(0, m.start() - 25):m.end() + 20], "long number without thousands separators (check: ID or count?)"))
            for m in FOUR_NUM.finditer(line):
                if 1900 <= int(m.group(1)) <= 2099:
                    continue
                found.append(("number", m.group(0), "count without thousands separator"))
            for m in ENTITY.finditer(line):
                found.append(("entity", line[max(0, m.start() - 25):m.end() + 15], "HTML entity shown literally (double escaped)"))
            for m in DASHES.finditer(line):
                found.append(("dash", line[max(0, m.start() - 25):m.end() + 15].encode("ascii", "replace").decode(), "em or en dash in visible text"))
            for m in TIME_ODD.finditer(line):
                found.append(("time", line[max(0, m.start() - 30):m.end() + 30], "time phrase that contradicts itself or shows a raw float"))
            for m in SHORTHAND.finditer(line):
                found.append(("shorthand", line[max(0, m.start() - 30):m.end() + 30], "terse shorthand that may read confusingly"))
    return found


def cap_style(text: str) -> str:
    words = re.findall(r"[A-Za-z][A-Za-z0-9'&.-]*", text)
    if len(words) < 2:
        return "single"
    rest = [w for w in words[1:] if w not in PROPER and not (len(w) > 1 and w.isupper())
            and not re.match(r"^[A-Z][a-z]+[A-Z]", w)]
    if not rest:
        return "single"
    upper = sum(1 for w in rest if w[0].isupper())
    small = {"and", "or", "of", "the", "a", "an", "to", "in", "on", "for", "by", "with", "at", "per", "vs"}
    if upper and upper >= len([w for w in rest if w.lower() not in small]) * 0.6:
        return "title"
    if upper:
        return "mixed"
    return "sentence"


# ---------------------------------------------------------------- driver
def slug(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-")
    return (s or "home")[:90]


class Audit:
    def __init__(self, args):
        self.args = args
        self.out = Path(args.out)
        (self.out / "pages").mkdir(parents=True, exist_ok=True)
        (self.out / "crops").mkdir(parents=True, exist_ok=True)
        self.findings = []
        self.copy = collections.defaultdict(lambda: {"where": [], "detail": "", "crop": None, "sel": ""})
        self.caps = []
        self.texts = {}
        self.crop_sigs = set()
        self.states = 0
        self.t0 = time.time()

    # -------------------------------------------- browser helpers
    def login(self, browser, width, user, password):
        ctx = browser.new_context(viewport={"width": width, "height": 900}, device_scale_factor=1)
        pg = ctx.new_page()
        if user:
            pg.goto(self.url("/login?pw=1"), wait_until="load")
            pg.fill("input[name=username]", user)
            pg.fill("input[name=password]", password)
            pg.click("form.pw button[type=submit]")
            pg.wait_for_load_state("load")
            if "/login" in pg.url:
                raise SystemExit(f"sign in failed for {user}")
        return ctx, pg

    def url(self, path):
        return urljoin(self.args.base.rstrip("/") + "/", path.lstrip("/"))

    def crop(self, pg, rect, name, doc_h):
        if not rect or self.args.no_crops:
            return None
        # Centre the element in a crop of at least 420x180 so the context shows.
        vw = pg.viewport_size["width"] if pg.viewport_size else 1440
        w = min(max(420, rect["w"] + 80), 1600, vw)
        h = min(max(180, rect["h"] + 80), 900)
        x = max(0, min(rect["x"] + rect["w"] / 2 - w / 2, vw - w))
        y = max(0, rect["y"] + rect["h"] / 2 - h / 2)
        h = min(h, max(1, doc_h - y))
        path = self.out / "crops" / (name + ".png")
        try:
            pg.screenshot(path=str(path), clip={"x": x, "y": y, "width": w, "height": h}, full_page=True)
            return path.relative_to(self.out).as_posix()
        except Exception as e:  # noqa: BLE001
            print("   crop failed", name, e)
            return None

    # -------------------------------------------- one page state
    def check_state(self, pg, role, path, tab, width, events):
        pg.evaluate("window.scrollTo(0,0)")
        pg.wait_for_timeout(120)
        try:
            res = pg.evaluate(AUDIT_JS, {})
        except Exception as e:  # noqa: BLE001
            print("   audit failed", path, tab, e)
            return
        self.states += 1
        label = path + (("#" + tab) if tab else "")
        sname = slug(f"{role}-{label}-{width}")
        doc_h = res["H"]
        if not self.args.no_pages:
            try:
                pg.screenshot(path=str(self.out / "pages" / (sname + ".png")),
                              clip={"x": 0, "y": 0, "width": res["W"], "height": min(doc_h, self.args.page_max_h)},
                              full_page=True)
            except Exception as e:  # noqa: BLE001
                print("   page shot failed", e)
        per_check = collections.Counter()
        for f in res["findings"]:
            f.update(page=label, role=role, width=width)
            sig = (f["check"], f["sel"], f["text"][:50])
            f["sig"] = "|".join(sig)
            per_check[f["check"]] += 1
            if sig not in self.crop_sigs and per_check[f["check"]] <= self.args.max_crops:
                self.crop_sigs.add(sig)
                f["crop"] = self.crop(pg, f.get("rect"), f"{sname}-{f['check']}-{per_check[f['check']]}", doc_h)
            self.findings.append(f)
        for kind, msg in events:
            self.findings.append({"check": "console", "sel": "", "text": msg[:200], "detail": kind,
                                  "page": label, "role": role, "width": width, "sig": "console|" + msg[:120]})
        events.clear()
        # copy: only once per state text, at the widest width (copy does not change with width)
        if width == self.args.widths[0] or role == "anon":
            for c in res["caps"]:
                c.update(page=label)
                self.caps.append(c)
            self.texts[label] = res["text"]
            for kind, snip, why in copy_checks(res["text"]) + copy_checks(res["side"]):
                key = (kind, snip)
                entry = self.copy[key]
                entry["detail"] = why
                if label not in entry["where"]:
                    entry["where"].append(label)
                if entry["crop"] is None and len(self.copy) < 400:
                    try:
                        loc = pg.evaluate(LOCATE_JS, re.sub(r"\s+", " ", snip.strip())[:60])
                    except Exception:  # noqa: BLE001
                        loc = None
                    if loc:
                        entry["sel"] = loc["sel"]
                        entry["crop"] = self.crop(pg, loc["rect"], f"copy-{slug(kind)}-{len(self.copy)}", doc_h) or ""
                    else:
                        entry["crop"] = ""
        n = len(res["findings"])
        print(f"  [{time.time() - self.t0:6.0f}s] {role:5} {width:4} {label:60.60} {n:3} findings")

    def tabs_of(self, pg):
        return pg.evaluate("""() => {
          const t = [];
          document.querySelectorAll('.secnav a[href^="#"]').forEach(a => t.push({kind: 'hash', key: a.getAttribute('href').slice(1)}));
          document.querySelectorAll('[data-workspace-tabs] button[data-panel]').forEach(b => t.push({kind: 'panel', key: b.dataset.panel}));
          return t;
        }""")

    def visit(self, pg, role, path, width, events):
        try:
            pg.goto(self.url(path), wait_until="load", timeout=90000)
        except Exception as e:  # noqa: BLE001
            self.findings.append({"check": "console", "sel": "", "text": f"page did not load: {e}"[:200],
                                  "detail": "navigation", "page": path, "role": role, "width": width,
                                  "sig": "console|nav|" + path})
            return
        pg.wait_for_timeout(self.args.settle)
        tabs = self.tabs_of(pg)
        if not tabs:
            self.check_state(pg, role, path, "", width, events)
        for t in tabs:
            if t["kind"] == "hash":
                pg.evaluate("(k) => { const a = document.querySelector('.secnav a[href=\"#' + k + '\"]'); if (a) a.click(); }", t["key"])
            else:
                pg.evaluate("(k) => { const b = document.querySelector('[data-workspace-tabs] button[data-panel=\"' + k + '\"]'); if (b) b.click(); }", t["key"])
            pg.wait_for_timeout(250)
            self.check_state(pg, role, path, t["key"], width, events)
        if self.args.details:
            opened = pg.evaluate("""() => {
              let n = 0;
              document.querySelectorAll('main details:not([open])').forEach(d => {
                if (d.closest('.fmore,.save-view,.row-actions')) return;
                d.open = true; n++;
              });
              return n;
            }""")
            if opened:
                cur = tabs[-1]["key"] if tabs else ""
                if tabs:
                    # details on every tab: show each tab again with everything open
                    for t in tabs:
                        if t["kind"] == "hash":
                            pg.evaluate("(k) => { const a = document.querySelector('.secnav a[href=\"#' + k + '\"]'); if (a) a.click(); }", t["key"])
                        else:
                            pg.evaluate("(k) => { const b = document.querySelector('[data-workspace-tabs] button[data-panel=\"' + k + '\"]'); if (b) b.click(); }", t["key"])
                        pg.wait_for_timeout(150)
                        pg.evaluate("() => document.querySelectorAll('main details:not([open])').forEach(d => { if (!d.closest('.fmore,.save-view,.row-actions')) d.open = true; })")
                        self.check_state(pg, role, path, t["key"] + "+open", width, events)
                else:
                    self.check_state(pg, role, path, "+open", width, events)
                del cur
            # popups: open the More filters panel alone and check it fits
            has_more = pg.evaluate("() => !!document.querySelector('details.fmore')")
            if has_more:
                pg.evaluate("() => { document.querySelectorAll('main details').forEach(d => d.open = false); const m = document.querySelector('details.fmore'); m.open = true; }")
                pg.wait_for_timeout(150)
                self.check_state(pg, role, path, "more-filters", width, events)

    # -------------------------------------------- page plan
    def plan(self, pg):
        a = self.args
        pg.goto(self.url("/"), wait_until="load")
        lists = pg.evaluate("() => Array.from(document.querySelectorAll('nav.side a[href^=\"/lists/\"]')).map(a => a.getAttribute('href'))")
        lists = list(dict.fromkeys(lists))
        pg.goto(self.url("/settings"), wait_until="load")
        settings = pg.evaluate("() => Array.from(document.querySelectorAll('main a[href^=\"/settings\"]')).map(a => a.getAttribute('href').split('#')[0])")
        settings = [s for s in dict.fromkeys(settings) if not s.endswith(".json") and "?" not in s]
        pg.goto(self.url("/enrichment"), wait_until="load")
        enrich = pg.evaluate("() => Array.from(document.querySelectorAll('main a[href^=\"/enrichment/\"]')).map(a => a.getAttribute('href').split('#')[0])")
        enrich = list(dict.fromkeys(enrich))
        src = [e for e in enrich if "/source/" in e][:2]
        enrich = [e for e in enrich if "/source/" not in e] + src
        pages = ["/", "/ask", "/people", "/people?view=email", "/people?view=verified", "/people?view=direct",
                 "/people?view=linkedin", "/people?view=hunting",
                 "/people?st=CA&role=leader&desig=cfp&view=email", "/firms", "/firms?view=contacts",
                 "/firms?st=NY&stat=new&on=any", "/signals", "/saved"]
        for i, l in enumerate(lists):
            pages += [l, l + "?view=disqualified", l + "?view=scoring"]
            if i == 0:
                pages.append(l + "?st=TX&sig=1")
        pages += [f"/firm/{c}" for c in a.firms]
        pages += ["/enrichment"] + enrich
        pages += ["/settings"] + [s for s in settings if s != "/settings"]
        plain = ["/", "/settings", (lists[0] + "?view=scoring") if lists else "/firms", f"/firm/{a.firms[0]}"]
        return list(dict.fromkeys(pages)), plain

    # -------------------------------------------- run
    def run(self):
        a = self.args
        with sync_playwright() as p:
            browser = p.chromium.launch()
            events = []
            role_ref = ["admin"]

            def hook(page):
                page.on("console", lambda m: events.append(("console " + m.type, m.text))
                        if m.type == "error" and not (role_ref[0] == "user" and "403" in m.text) else None)
                page.on("pageerror", lambda e: events.append(("pageerror", str(e))))
                page.on("requestfailed", lambda r: events.append(("requestfailed", f"{r.method} {r.url} {r.failure}"))
                        if "favicon" not in r.url else None)
                page.on("response", lambda r: events.append((f"http {r.status}", f"{r.request.method} {r.url}"))
                        if r.status >= 400 and not (r.status == 403 and role_ref[0] == "user"
                                                    and r.request.resource_type == "document") else None)

            ctx, pg = self.login(browser, a.widths[0], a.user, a.password)
            pages, plain = self.plan(pg)
            ctx.close()
            if a.only:
                rx = re.compile(a.only)
                pages = [x for x in pages if rx.search(x)]
                plain = [x for x in plain if rx.search(x)]
            print(f"{len(pages)} admin pages, {len(plain)} plain-user pages, widths {a.widths}")
            for width in a.widths:
                ctx, pg = self.login(browser, width, a.user, a.password)
                hook(pg)
                events.clear()
                for path in pages:
                    self.visit(pg, "admin", path, width, events)
                ctx.close()
                if a.plain_user and plain:
                    ctx, pg = self.login(browser, width, a.plain_user, a.plain_password or a.password)
                    role_ref[0] = "user"
                    hook(pg)
                    events.clear()
                    for path in plain:
                        self.visit(pg, "user", path, width, events)
                    ctx.close()
                    role_ref[0] = "admin"
                if not a.only or a.only_anon:
                    ctx, pg = self.login(browser, width, None, None)
                    hook(pg)
                    events.clear()
                    for path in ("/login", "/login?pw=1"):
                        self.visit(pg, "anon", path, width, events)
                    ctx.close()
            browser.close()
        self.spelling()
        self.write()

    # -------------------------------------------- spelling
    def spelling(self):
        self.misspelled = []
        try:
            from spellchecker import SpellChecker
        except ImportError:
            print("pyspellchecker not installed; spelling skipped")
            return
        sp = SpellChecker()
        words = collections.defaultdict(lambda: {"n": 0, "pages": set(), "ctx": ""})
        for label, text in self.texts.items():
            for line in text.split("\n"):
                for m in re.finditer(r"(?<![\w@./-])([A-Za-z][a-z']{2,})(?![\w@/-])", line):
                    w = m.group(1)
                    if w[0].isupper() and m.start() > 0:
                        continue  # likely a proper noun in data
                    lw = w.lower().strip("'")
                    if lw.endswith("'s"):
                        lw = lw[:-2]
                    if lw in DOMAIN_WORDS or len(lw) < 3:
                        continue
                    e = words[lw]
                    e["n"] += 1
                    e["pages"].add(label)
                    if not e["ctx"]:
                        e["ctx"] = line[max(0, m.start() - 30):m.end() + 30].strip()
        unknown = sp.unknown(list(words))
        for w in sorted(unknown, key=lambda x: -words[x]["n"]):
            e = words[w]
            self.misspelled.append({"word": w, "n": e["n"], "pages": sorted(e["pages"])[:4],
                                    "suggest": sp.correction(w), "ctx": e["ctx"]})

    # -------------------------------------------- report
    def write(self):
        out = self.out
        (out / "findings.json").write_text(json.dumps({
            "findings": self.findings,
            "copy": [{"kind": k[0], "snippet": k[1], **v} for k, v in self.copy.items()],
            "caps": self.caps,
            "spelling": getattr(self, "misspelled", []),
        }, indent=1, default=list), encoding="utf-8")
        groups = collections.OrderedDict()
        order = ["overlap", "spill", "overflow", "clipped", "grid", "tablepad", "flexrow", "hscroll",
                 "hidden", "icon", "tinytext", "contrast", "console"]
        for f in sorted(self.findings, key=lambda f: (order.index(f["check"]) if f["check"] in order else 99)):
            groups.setdefault(f["check"], collections.OrderedDict()).setdefault(f["sig"], []).append(f)
        lines = [f"# Bellwether visual audit", "",
                 f"{self.states} page states at widths {', '.join(map(str, self.args.widths))}; "
                 f"base {self.args.base}; {time.strftime('%Y-%m-%d %H:%M')}.", ""]
        for check, sigs in groups.items():
            lines += [f"## {check} ({len(sigs)} distinct, {sum(len(v) for v in sigs.values())} occurrences)", ""]
            for sig, occ in sigs.items():
                f0 = occ[0]
                where = sorted({f"{o['page']} @{o['width']}" + ("" if o["role"] == "admin" else f" ({o['role']})") for o in occ})
                crop = next((o.get("crop") for o in occ if o.get("crop")), None)
                lines.append(f"- **{f0['sel'] or '(page)'}**: {f0['detail']}")
                if f0["text"]:
                    lines.append(f"  - text: `{f0['text'][:120]}`")
                lines.append(f"  - where ({len(where)}): {'; '.join(where[:8])}{' ...' if len(where) > 8 else ''}")
                if crop:
                    lines.append(f"  - crop: {crop}")
            lines.append("")
        lines += ["## copy", ""]
        for (kind, snip), v in sorted(self.copy.items()):
            lines.append(f"- [{kind}] `{snip}`: {v['detail']}; on {len(v['where'])} page(s): "
                         f"{'; '.join(v['where'][:5])}{' ...' if len(v['where']) > 5 else ''}"
                         f"{'; ' + v['sel'] if v['sel'] else ''}{'; crop ' + v['crop'] if v['crop'] else ''}")
        lines += ["", "## capitalisation", ""]
        by_kind = collections.defaultdict(lambda: collections.defaultdict(set))
        for c in self.caps:
            by_kind[c["kind"]][(cap_style(c["text"]), c["text"])].add(c["page"])
        for kind, entries in sorted(by_kind.items()):
            styles = collections.Counter(s for s, _ in entries)
            odd = [(t, pgs) for (s, t), pgs in entries.items() if s in ("title", "mixed")]
            low = sorted({t for (s, t) in entries if t[:1].islower()})
            if low and kind.split(".")[-1] in ("chip", "pill", "ftype", "btn") or (low and kind in ("button", "th", "summary", "dt")):
                lines.append(f"- {kind}: starts in lower case: " + "; ".join(f"`{x}`" for x in low[:20]))
            if odd and styles["sentence"] >= 1:
                lines.append(f"- {kind}: {styles['sentence']} sentence case, {styles['title'] + styles['mixed']} title or mixed case:")
                for t, pgs in sorted(odd)[:25]:
                    lines.append(f"  - `{t}` ({'; '.join(sorted(pgs)[:3])})")
        lines += ["", "## spelling (unknown words, review by hand)", ""]
        for m in getattr(self, "misspelled", [])[:200]:
            lines.append(f"- `{m['word']}` x{m['n']} (suggest {m['suggest']}): {m['ctx'][:90]} [{'; '.join(m['pages'][:2])}]")
        (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"\n{len(self.findings)} layout findings, {len(self.copy)} copy findings; report at {out / 'report.md'}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base", default="http://127.0.0.1:8812")
    ap.add_argument("--user", default=os.environ.get("QA_USER", "qaadmin"))
    ap.add_argument("--password", default=os.environ.get("QA_PASSWORD", ""))
    ap.add_argument("--plain-user", default=os.environ.get("QA_PLAIN_USER", "qa"))
    ap.add_argument("--plain-password", default=os.environ.get("QA_PLAIN_PASSWORD", ""))
    ap.add_argument("--out", default="qa_visual_out")
    ap.add_argument("--widths", default="1440,1100,390")
    ap.add_argument("--firms", default=",".join(DEFAULT_FIRMS))
    ap.add_argument("--only", default="", help="regex; audit only matching paths")
    ap.add_argument("--only-anon", action="store_true", help="with --only, still audit the sign-in page")
    ap.add_argument("--max-crops", type=int, default=4)
    ap.add_argument("--settle", type=int, default=500, help="ms to wait after load")
    ap.add_argument("--page-max-h", type=int, default=7000)
    ap.add_argument("--no-pages", action="store_true", help="skip full-page screenshots")
    ap.add_argument("--no-crops", action="store_true")
    ap.add_argument("--no-details", dest="details", action="store_false",
                    help="do not re-check pages with every <details> opened")
    args = ap.parse_args()
    if not args.password:
        ap.error("--password (or QA_PASSWORD) is required")
    args.widths = [int(w) for w in args.widths.split(",") if w.strip()]
    args.firms = [f.strip() for f in args.firms.split(",") if f.strip()]
    Audit(args).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
