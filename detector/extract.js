// 프레임(문서) 하나에서 탐지 후보를 추출한다. Playwright frame.evaluate()로 실행.
// 반환: { hidden: [...], texts: [...], links: [...] }
//  - hidden: 은닉 스타일을 "직접 선언한" 요소와 기법 신호 (TRANSPARENT / OFFSCREEN 후보)
//  - texts : 직접 텍스트를 가진 요소 (HOMOGLYPH / JAMO 후보)
//  - links : 다음 페이지 후보 URL
() => {
  const SKIP = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'HEAD', 'META', 'LINK', 'TITLE', 'SVG', 'IFRAME']);
  const docW = Math.max(document.documentElement.scrollWidth, window.innerWidth);
  const docH = Math.max(document.documentElement.scrollHeight, window.innerHeight);

  // ---- 선택자 ----
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
    try {
      if (document.querySelectorAll(sel).length !== 1) sel = 'body > ' + sel;
    } catch (e) {}
    return sel;
  }

  // ---- 색상 유틸 ----
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

  // ---- 메인 DOM 순회 (Shadow DOM 포함) ----
  function* walkDOM(root) {
    for (const el of (root.querySelectorAll ? root.querySelectorAll('*') : [])) {
      yield el;
      if (el.shadowRoot) yield* walkDOM(el.shadowRoot);
    }
  }

  // Shadow DOM 요소의 선택자는 호스트 선택자 + ">> shadow" 접두어 사용
  function shadowSelector(el) {
    let root = el.getRootNode();
    if (!root || root === document || !root.host) return selector(el);
    return selector(root.host) + ' >> shadow > ' + step(el);
  }
  function inShadow(el) {
    const root = el.getRootNode();
    return root !== document && root instanceof ShadowRoot;
  }

  for (const el of document.body ? walkDOM(document.body) : []) {
    if (SKIP.has(el.tagName.toUpperCase())) continue;
    const cs = getComputedStyle(el);
    const parent = el.parentElement;
    const pcs = parent ? getComputedStyle(parent) : null;
    const signals = [];

    // TRANSPARENT ------------------------------------------------------------
    if (parseFloat(cs.opacity) === 0) signals.push(['TRANSPARENT', 'opacity:0']);
    const col = rgba(cs.color);
    const colorDeclaredHere = !pcs || cs.color !== pcs.color;
    if (col && col.a === 0 && colorDeclaredHere) signals.push(['TRANSPARENT', 'color:transparent']);
    else if (col && col.a > 0) {
      const bg = effectiveBg(el);
      const ratio = contrast(col, bg);
      const bgDeclaredHere = rgba(cs.backgroundColor) && rgba(cs.backgroundColor).a > 0.05;
      if (ratio < 1.15 && (colorDeclaredHere || bgDeclaredHere))
        signals.push(['TRANSPARENT', 'same-color(' + ratio.toFixed(2) + ')']);
    }

    // OFFSCREEN: 기존 신호 ---------------------------------------------------
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

    // OFFSCREEN: 추가 신호 ---------------------------------------------------
    // height/width:0 + overflow:hidden (접이식 콘텐츠 은닉)
    // 부모에서 상속된 overflow:hidden 제외 — 이 요소에서 직접 선언한 경우만
    const elH = parseFloat(cs.height), elW = parseFloat(cs.width);
    const ovfDeclaredHere = !pcs || cs.overflow !== pcs.overflow
                            || cs.overflowY !== pcs.overflowY
                            || cs.overflowX !== pcs.overflowX;
    if (elH === 0 && ovfDeclaredHere && (cs.overflow === 'hidden' || cs.overflowY === 'hidden'))
      signals.push(['OFFSCREEN', 'height:0+overflow:hidden']);
    if (elW === 0 && ovfDeclaredHere && (cs.overflow === 'hidden' || cs.overflowX === 'hidden'))
      signals.push(['OFFSCREEN', 'width:0+overflow:hidden']);

    // transform: scale(0) — matrix(a,b,c,d,tx,ty): sx=√(a²+b²), sy=√(c²+d²)
    if (cs.transform && cs.transform !== 'none') {
      const mat = cs.transform.match(/^matrix\(([^)]+)\)/);
      if (mat) {
        const n = mat[1].split(',').map(Number);
        const sx = Math.sqrt(n[0] * n[0] + n[1] * n[1]);
        const sy = Math.sqrt(n[2] * n[2] + n[3] * n[3]);
        if (sx < 0.01 && sy < 0.01) signals.push(['OFFSCREEN', 'transform:scale(~0)']);
      }
    }

    // clip-path로 완전히 숨김 (inset(100%) 등)
    if (cs.clipPath && cs.clipPath !== 'none') {
      if (/inset\(\s*100%/.test(cs.clipPath) || /inset\(\s*50%\s+0\s+50%/.test(cs.clipPath))
        signals.push(['OFFSCREEN', 'clip-path:' + cs.clipPath.slice(0, 50)]);
    }

    const own = ownText(el);
    const full = norm(el.textContent);
    if (signals.length && full) {
      const sel = inShadow(el) ? shadowSelector(el) : selector(el);
      hidden.push({ selector: sel, tag: el.tagName.toLowerCase(), cls: el.className || '',
                    text: full, raw: el.textContent, signals, href: el.closest('a') ? el.closest('a').href : null });
    }
    if (own) {
      const sel = inShadow(el) ? shadowSelector(el) : selector(el);
      texts.push({ selector: sel, tag: el.tagName.toLowerCase(), cls: el.className || '', text: own,
                   raw: Array.from(el.childNodes).filter(n => n.nodeType === 3).map(n => n.nodeValue).join('') });
    }
  }

  // ---- 속성 텍스트 (alt/title/aria-label) ----
  for (const el of document.querySelectorAll('[alt],[title],[aria-label]')) {
    if (SKIP.has(el.tagName.toUpperCase())) continue;
    for (const a of ['alt', 'title', 'aria-label']) {
      const v = norm(el.getAttribute(a));
      if (v) texts.push({ selector: selector(el), tag: el.tagName.toLowerCase(), cls: el.className || '', text: v, raw: el.getAttribute(a), attr: a });
    }
  }

  // ---- CSS 가상 요소(::before / ::after) 콘텐츠 ----
  // DOM에 없으므로 별도 패스로 추출한다.
  for (const el of document.body ? document.body.querySelectorAll('*') : []) {
    if (SKIP.has(el.tagName.toUpperCase())) continue;
    for (const pseudo of ['::before', '::after']) {
      const pcs = getComputedStyle(el, pseudo);
      const raw_c = pcs.content;
      if (!raw_c || raw_c === 'none' || raw_c === 'normal' || raw_c === '""' || raw_c === "''") continue;
      const text = norm(raw_c.replace(/^["']|["']$/g, ''));
      if (text.length < 2) continue;
      // pseudo-element은 querySelectorAll로 선택 불가 → 호스트 요소 선택자를 위치로 사용
      const pSel = selector(el);
      // 가상 요소 자체의 은닉 여부 확인
      const pSignals = [];
      if (parseFloat(pcs.opacity) === 0) pSignals.push(['TRANSPARENT', 'pseudo:opacity:0']);
      const pCol = rgba(pcs.color);
      if (pCol && pCol.a === 0) pSignals.push(['TRANSPARENT', 'pseudo:color:transparent']);
      else if (pCol && pCol.a > 0) {
        const bg = effectiveBg(el);
        const ratio = contrast(pCol, bg);
        if (ratio < 1.15) pSignals.push(['TRANSPARENT', 'pseudo:same-color(' + ratio.toFixed(2) + ')']);
      }
      if (pSignals.length)
        hidden.push({ selector: pSel, tag: el.tagName.toLowerCase(), cls: el.className || '',
                      text, raw: raw_c, signals: pSignals, href: null });
      texts.push({ selector: pSel, tag: el.tagName.toLowerCase(), cls: el.className || '',
                   text, raw: raw_c, attr: pseudo });
    }
  }

  // ---- 링크 수집 (a, area, meta refresh, form GET) ----
  const linkSet = new Set();
  const addLink = (url) => { try { linkSet.add(new URL(url, location.href).href); } catch(e) {} };

  for (const a of document.querySelectorAll('a[href]')) addLink(a.href);
  for (const a of document.querySelectorAll('area[href]')) addLink(a.href);

  // <meta http-equiv="refresh" content="0;url=...">
  const metaRefresh = document.querySelector('meta[http-equiv="refresh"]');
  if (metaRefresh) {
    const m = (metaRefresh.getAttribute('content') || '').match(/\burl=([^\s;]+)/i);
    if (m) addLink(m[1].trim());
  }

  // <form method="get"> action URL
  for (const f of document.querySelectorAll('form:not([method]), form[method="get"], form[method="GET"]')) {
    if (f.action && !f.action.startsWith('javascript:')) addLink(f.action);
  }

  // data-* 속성 네비게이션 (프레임워크·CMS 계열)
  const DATA_NAV = ['data-href', 'data-url', 'data-link', 'data-action', 'data-path', 'data-page', 'data-route', 'data-goto'];
  for (const el of document.querySelectorAll(DATA_NAV.map(a => '[' + a + ']').join(','))) {
    for (const a of DATA_NAV) {
      const v = el.getAttribute(a);
      if (v && (v.startsWith('/') || v.startsWith('http://') || v.startsWith('https://'))) addLink(v);
    }
  }

  // onclick 인라인 핸들러 (공공기관 JSP 레거시에서 흔한 location.href=, goPage() 패턴)
  for (const el of document.querySelectorAll('[onclick]')) {
    const oc = el.getAttribute('onclick');
    for (const m of oc.matchAll(/['"](\/[a-z가-힣0-9][a-z가-힣0-9\-_.\/]*(?:\?[^'"\s]*)?)['"]/gi))
      addLink(m[1]);
  }

  // <script> 인라인 내부 경로 문자열 (라우트 배열, nextPage 변수 등)
  // /api, /static 등 애셋·API 경로는 제외
  const ASSET_RE = /^\/(?:api|static|assets?|images?|img|css|js|fonts?|icon)/i;
  for (const s of document.querySelectorAll('script:not([src])')) {
    for (const m of s.textContent.matchAll(/['"](\/[a-z가-힣0-9][a-z가-힣0-9\-_.\/]*(?:\?[^'"\s]*)?)['"]/gi)) {
      if (!ASSET_RE.test(m[1])) addLink(m[1]);
    }
  }

  const links = Array.from(linkSet);
  return { hidden, texts, links };
}
