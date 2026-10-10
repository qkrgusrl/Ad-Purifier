// 프레임(문서) 하나에서 탐지 후보를 추출한다. Playwright frame.evaluate()로 실행.
// 반환: { hidden: [...], texts: [...] }
//  - hidden: 은닉 스타일을 "직접 선언한" 요소와 기법 신호 (TRANSPARENT / OFFSCREEN 후보)
//  - texts : 직접 텍스트를 가진 요소 (HOMOGLYPH / JAMO 후보)
() => {
  const SKIP = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'HEAD', 'META', 'LINK', 'TITLE', 'SVG', 'IFRAME']);
  const docW = Math.max(document.documentElement.scrollWidth, window.innerWidth);
  const docH = Math.max(document.documentElement.scrollHeight, window.innerHeight);

  // ---- 선택자: 상위부터 "tag.class > ..." , 클래스로 형제 구분이 안 되면 :nth-of-type(n)
  const esc = (s) => (window.CSS && CSS.escape) ? CSS.escape(s) : s;
  function step(el) {
    const tag = el.tagName.toLowerCase();
    const cls = Array.from(el.classList).map(c => '.' + esc(c)).join('');
    let s = tag + cls;
    const parent = el.parentElement;
    if (parent) {
      const sameTag = Array.from(parent.children).filter(c => c.tagName === el.tagName);
      const clash = sameTag.filter(c => c !== el && (cls === '' || c.matches(s))).length > 0;
      if (clash) s += ':nth-of-type(' + (sameTag.indexOf(el) + 1) + ')';
    }
    return s;
  }
  function selector(el) {
    const parts = [];
    for (let e = el; e && e !== document.body && e !== document.documentElement; e = e.parentElement) {
      parts.unshift(step(e));
    }
    let sel = parts.join(' > ');
    // 고유성 검증: 2개 이상을 가리키면 body부터 전체 경로로 고정
    try {
      if (document.querySelectorAll(sel).length !== 1) sel = 'body > ' + sel;
    } catch (e) { /* 무시 */ }
    return sel;
  }

  // ---- 색상 유틸
  function rgba(str) {
    const m = str && str.match(/rgba?\(([^)]+)\)/);
    if (!m) return null;
    const p = m[1].split(/[ ,/]+/).filter(Boolean).map(Number);
    return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
  }
  function effectiveBg(el) {
    for (let e = el; e; e = e.parentElement) {
      const c = rgba(getComputedStyle(e).backgroundColor);
      if (c && c.a > 0.05) return c;
    }
    return { r: 255, g: 255, b: 255, a: 1 };
  }
  function lum(c) {
    const f = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b);
  }
  function contrast(a, b) {
    const l1 = lum(a), l2 = lum(b);
    return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
  }

  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  function ownText(el) {
    let t = '';
    for (const n of el.childNodes) if (n.nodeType === 3) t += n.nodeValue;
    return norm(t);
  }

  const hidden = [], texts = [];
  for (const el of document.body ? document.body.querySelectorAll('*') : []) {
    if (SKIP.has(el.tagName.toUpperCase())) continue;
    const cs = getComputedStyle(el);
    const parent = el.parentElement;
    const pcs = parent ? getComputedStyle(parent) : null;
    const signals = [];

    // TRANSPARENT ------------------------------------------------------------
    if (parseFloat(cs.opacity) === 0) signals.push(['TRANSPARENT', 'opacity:0']);
    const col = rgba(cs.color);
    const pcol = pcs ? rgba(pcs.color) : null;
    const colorDeclaredHere = !pcs || cs.color !== pcs.color;
    if (col && col.a === 0 && colorDeclaredHere) signals.push(['TRANSPARENT', 'color:transparent']);
    else if (col && col.a > 0) {
      const bg = effectiveBg(el);
      const ratio = contrast(col, bg);
      const bgDeclaredHere = rgba(cs.backgroundColor) && rgba(cs.backgroundColor).a > 0.05;
      if (ratio < 1.15 && (colorDeclaredHere || bgDeclaredHere))
        signals.push(['TRANSPARENT', 'same-color(' + ratio.toFixed(2) + ')']);
    }

    // OFFSCREEN --------------------------------------------------------------
    if (cs.display === 'none') signals.push(['OFFSCREEN', 'display:none']);
    const fs = parseFloat(cs.fontSize);
    const pfs = pcs ? parseFloat(pcs.fontSize) : 16;
    if (fs <= 1 && pfs > 1) signals.push(['OFFSCREEN', 'font-size:' + fs + 'px']);
    if (cs.position === 'absolute' || cs.position === 'fixed') {
      const left = parseFloat(cs.left), top = parseFloat(cs.top);
      if (left <= -999 || top <= -999) signals.push(['OFFSCREEN', 'position:' + cs.position + ' left:' + cs.left + ' top:' + cs.top]);
      else if (cs.display !== 'none') {
        const r = el.getBoundingClientRect();
        if (r.width > 0 && (r.right + window.scrollX <= 0 || r.bottom + window.scrollY <= 0 || r.left > docW || r.top > docH))
          signals.push(['OFFSCREEN', 'offscreen-rect']);
      }
    }
    if (parseFloat(cs.textIndent) <= -999) signals.push(['OFFSCREEN', 'text-indent:' + cs.textIndent]);
    const tiny = parseFloat(cs.width) <= 1 && parseFloat(cs.height) <= 1 && cs.overflow === 'hidden';
    if ((cs.clip && cs.clip.startsWith('rect(0')) || tiny) signals.push(['OFFSCREEN', 'clip/1px(sr-only)']);
    if (cs.visibility === 'hidden' && (!pcs || pcs.visibility !== 'hidden')) signals.push(['OFFSCREEN', 'visibility:hidden']);

    const own = ownText(el);
    const full = norm(el.textContent);
    if (signals.length && full) {
      hidden.push({ selector: selector(el), tag: el.tagName.toLowerCase(), cls: el.className || '',
                    text: full, raw: el.textContent, signals, href: el.closest('a') ? el.closest('a').href : null });
    }
    if (own) {
      texts.push({ selector: selector(el), tag: el.tagName.toLowerCase(), cls: el.className || '', text: own,
                   raw: Array.from(el.childNodes).filter(n => n.nodeType === 3).map(n => n.nodeValue).join('') });
    }
  }
  // 속성 텍스트(alt/title/aria-label)도 후보에 포함
  for (const el of document.querySelectorAll('[alt],[title],[aria-label]')) {
    if (SKIP.has(el.tagName.toUpperCase())) continue;
    for (const a of ['alt', 'title', 'aria-label']) {
      const v = norm(el.getAttribute(a));
      if (v) texts.push({ selector: selector(el), tag: el.tagName.toLowerCase(), cls: el.className || '', text: v, raw: el.getAttribute(a), attr: a });
    }
  }
  const links = Array.from(document.querySelectorAll('a[href]')).map(a => a.href);
  return { hidden, texts, links };
}
