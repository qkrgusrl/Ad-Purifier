// 프레임(문서) 하나에서 탐지 후보를 추출한다. Playwright frame.evaluate()로 실행.
// 반환: { hidden: [...], texts: [...], links: [...] }
//  - hidden: 은닉 스타일을 "직접 선언한" 요소와 기법 신호 (TRANSPARENT / OFFSCREEN 후보)
//  - texts : 직접 텍스트를 가진 요소 (HOMOGLYPH / JAMO 후보)
//  - links : 다음 페이지 후보 [URL, 링크 글자] — 링크 글자(게시글 제목 등)는 크롤링 우선순위에 쓴다
() => {
  const SKIP = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'HEAD', 'META', 'LINK', 'TITLE', 'SVG', 'IFRAME']);
  const docW = Math.max(document.documentElement.scrollWidth, window.innerWidth);
  const docH = Math.max(document.documentElement.scrollHeight, window.innerHeight);

  // ---- 선택자 ----
  const esc = (s) => (window.CSS && CSS.escape) ? CSS.escape(s) : s;
  function step(el) {
    const tag = el.tagName.toLowerCase();
    const cls = [...el.classList].map(c => '.' + esc(c)).join('');
    let s = tag + cls;
    const parent = el.parentElement;
    if (parent) {
      const sameTag = [...parent.children].filter(c => c.tagName === el.tagName);
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
  // 요소 안의 글자 (textContent와 같되 script/style 등 코드 내용은 뺀다 — 숨김 블록 안의 CSS·JS가 광고 문구로 채점되지 않도록)
  const CODE_SEL = 'script,style,noscript,template';
  function textOf(el) {
    if (!el.querySelector(CODE_SEL)) return el.textContent;
    let t = '';
    const w = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    for (let n = w.nextNode(); n; n = w.nextNode())
      if (!n.parentElement.closest(CODE_SEL)) t += n.nodeValue;
    return t;
  }

  const hidden = [], texts = [], source_blocks = [];
  const owners = new WeakMap();
  const collected = new Set();
  let nextOwner = 0;
  // 주변 문맥 HTML 상한. 문서 뷰어(연수구청 synap 미리보기)는 큰 div 하나에 글자 조각 수천 개를 넣어,
  // 조각마다 그 div 전체를 문맥으로 만들면 합계가 수백 MB가 되어 evaluate가 끝나지 않았다.
  // 넘으면 대상 요소 쪽으로 좁힌다 — Gemma는 문맥 중 대상 앞뒤 50자만 쓰므로 판정 입력은 같다.
  const CONTEXT_HTML_MAX = 20000;
  const htmlLength = new WeakMap();
  const outerLength = n => {
    if (!htmlLength.has(n)) htmlLength.set(n, n.outerHTML.length);
    return htmlLength.get(n);
  };
  const nearbyCache = new Map();  // 문맥 요소 → (기사 → {html, text}): 같은 컨테이너의 조각끼리 한 번만 만든다
  function nearbyOf(local, article) {
    return [local.previousElementSibling, local, local.nextElementSibling]
      .filter(n => n && (n === local || (!n.matches('article,section,[role="article"]') &&
        (!article || article.contains(n)))));
  }
  function source(el, suffix = '') {
    if (!owners.has(el)) owners.set(el, 'dom-' + (++nextOwner));
    // 본문 전체나 다른 기사의 교육 문맥을 넘기지 않는다.
    const container = el.closest('p,li,td,th,figcaption,blockquote,div,article,section') || el;
    const local = container === document.body ? el : container;
    const article = el.closest('article,section,[role="article"],.article,.post,.entry');
    const title = article ? article.querySelector('h1,h2,h3,header') : null;
    // local에서 el까지 내려가며 앞뒤 형제를 합쳐도 상한 안인 가장 넓은 요소를 문맥으로 쓴다. 없으면 대상 요소만.
    let ctxEl = null, nearby = [el];
    for (let e = el; e; e = e.parentElement) {
      const candidate = nearbyOf(e, article);
      if (candidate.reduce((sum, n) => sum + outerLength(n), 0) <= CONTEXT_HTML_MAX) { ctxEl = e; nearby = candidate; }
      if (e === local) break;
    }
    const key = ctxEl || el;
    if (!nearbyCache.has(key)) nearbyCache.set(key, new Map());
    const byArticle = nearbyCache.get(key);
    if (!byArticle.has(article)) {
      byArticle.set(article, { html: nearby.map(n => n.outerHTML).join('\n'), text: nearby.map(textOf).join('\n') });
    }
    const near = byArticle.get(article);
    const titleHtml = title && !nearby.some(n => n.contains(title)) && outerLength(title) <= CONTEXT_HTML_MAX
      ? title.outerHTML + '\n' : '';
    return { source_id: owners.get(el) + suffix,
             source_html: el.outerHTML,
             source_context: titleHtml + near.html,
             source_near_text: (title ? title.textContent + '\n' : '') + near.text };
  }
  function collect(row) {
    if (!collected.has(row.source_id)) {
      source_blocks.push(row);
      collected.add(row.source_id);
    }
    return row;
  }

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
    // 0이 아니어도 사람 눈에 안 보이는 불투명도(0.01 등)까지 — 요강 예시 opacity:0의 변형
    if (parseFloat(cs.opacity) <= 0.05) signals.push(['TRANSPARENT', 'opacity:' + cs.opacity]);
    const fop = (cs.filter || '').match(/opacity\(([\d.]+)(%?)\)/);
    if (fop && parseFloat(fop[1]) / (fop[2] ? 100 : 1) <= 0.05) signals.push(['TRANSPARENT', 'filter:' + fop[0]]);
    // 글자 채움색만 투명 — 그라데이션 글자(background-clip:text)는 실제로 보이므로 제외
    const fill = rgba(cs.webkitTextFillColor || '');
    const clipText = cs.webkitBackgroundClip === 'text' || cs.backgroundClip === 'text';
    if (fill && fill.a === 0 && !clipText && (!pcs || cs.webkitTextFillColor !== pcs.webkitTextFillColor))
      signals.push(['TRANSPARENT', '-webkit-text-fill-color:transparent']);
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

    // translate·음수 margin으로 화면 밖으로 밀어낸 요소 (position 없이도 가능). 999px 이상 옮겼고 실제로 화면 밖일 때만 —
    // 캐러셀은 바깥 트랙만 옮기므로 각 슬라이드에는 걸리지 않는다
    const tm = cs.transform && cs.transform.match(/^matrix\(([^)]+)\)/);
    const tx = tm ? Number(tm[1].split(',')[4]) : 0, ty = tm ? Number(tm[1].split(',')[5]) : 0;
    const farShift = Math.abs(tx) >= 999 || Math.abs(ty) >= 999;
    const farMargin = parseFloat(cs.marginLeft) <= -999 || parseFloat(cs.marginTop) <= -999;
    if ((farShift || farMargin) && cs.display !== 'none') {
      // 상자가 아니라 글자 범위로 잰다 — 음수 margin 블록은 상자 폭이 늘어나 오른쪽 끝이 화면 안에 남는다
      const rng = document.createRange();
      rng.selectNodeContents(el);
      const r = rng.getBoundingClientRect();
      if (r.right + window.scrollX <= 0 || r.bottom + window.scrollY <= 0 || r.left >= Math.max(docW, window.innerWidth) - 1)
        signals.push(['OFFSCREEN', farShift ? 'transform:translate(' + tx + 'px,' + ty + 'px)' : 'margin:' + cs.marginLeft + ' ' + cs.marginTop]);
    }

    // clip-path로 완전히 숨김 (inset(100%) 등)
    if (cs.clipPath && cs.clipPath !== 'none') {
      if (/inset\(\s*100%/.test(cs.clipPath) || /inset\(\s*50%\s+0\s+50%/.test(cs.clipPath))
        signals.push(['OFFSCREEN', 'clip-path:' + cs.clipPath.slice(0, 50)]);
    }

    const own = ownText(el);
    const rawFull = signals.length ? textOf(el) : '';
    const full = norm(rawFull);
    const link = el.closest('a[href]');
    if (signals.length && full) {
      const sel = inShadow(el) ? shadowSelector(el) : selector(el);
      hidden.push(collect({ ...source(el), selector: sel, tag: el.tagName.toLowerCase(), cls: el.className || '',
                    text: full, raw: rawFull, signals, href: link ? link.href : null }));
    }
    if (own) {
      const sel = inShadow(el) ? shadowSelector(el) : selector(el);
      // href: 링크 글자면 링크 주소 — 본문에 같은 외부 사이트 링크를 여러 개 끼워 넣은 '삽입형' 판정에 쓴다
      texts.push(collect({ ...source(el), signals, selector: sel, tag: el.tagName.toLowerCase(), cls: el.className || '', text: own,
                   raw: [...el.childNodes].filter(n => n.nodeType === 3).map(n => n.nodeValue).join(''),
                   href: link ? link.href : null }));
    }
  }

  // ---- 속성 텍스트 (alt/title/aria-label) ----
  for (const el of document.querySelectorAll('[alt],[title],[aria-label]')) {
    if (SKIP.has(el.tagName.toUpperCase())) continue;
    for (const a of ['alt', 'title', 'aria-label']) {
      const v = norm(el.getAttribute(a));
      if (v) texts.push(collect({ ...source(el, '@' + a), signals: [], href: el.closest('a[href]')?.href || null, selector: selector(el), tag: el.tagName.toLowerCase(), cls: el.className || '', text: v, raw: el.getAttribute(a), attr: a }));
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
      if (parseFloat(pcs.opacity) <= 0.05) pSignals.push(['TRANSPARENT', 'pseudo:opacity:' + pcs.opacity]);
      const pCol = rgba(pcs.color);
      if (pCol && pCol.a === 0) pSignals.push(['TRANSPARENT', 'pseudo:color:transparent']);
      else if (pCol && pCol.a > 0) {
        const bg = effectiveBg(el);
        const ratio = contrast(pCol, bg);
        if (ratio < 1.15) pSignals.push(['TRANSPARENT', 'pseudo:same-color(' + ratio.toFixed(2) + ')']);
      }
      if (pSignals.length)
        hidden.push(collect({ ...source(el, pseudo), selector: pSel, tag: el.tagName.toLowerCase(), cls: el.className || '',
                      text, raw: raw_c, signals: pSignals, href: null }));
      texts.push(collect({ ...source(el, pseudo), signals: pSignals, href: null, selector: pSel, tag: el.tagName.toLowerCase(), cls: el.className || '',
                   text, raw: raw_c, attr: pseudo }));
    }
  }

  // ---- 링크 수집 (a, area, meta refresh, form GET) ----
  const linkMap = new Map();  // URL → 링크 글자 (같은 URL이면 글자가 있는 쪽을 남김)
  const addLink = (url, text = '') => {
    try {
      const u = new URL(url, location.href).href;
      if (!linkMap.get(u)) linkMap.set(u, text);
    } catch (e) {}
  };

  for (const a of document.querySelectorAll('a[href]'))
    addLink(a.href, norm(a.textContent || a.getAttribute('title') || '').slice(0, 120));
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

  // ---- 문서 제목·메타 설명 (head) ----
  // 기생 페이지는 본문 없이 제목·메타에만 광고 문구를 넣고 바로 다른 사이트로 넘긴다(검색엔진 노출용).
  // 보통 페이지에서는 게시글 제목과 겹치므로, 후보로 쓸지는 main.candidates_from이 정한다.
  const head = [];
  const titleEl = document.querySelector('head > title');
  if (titleEl && norm(titleEl.textContent))
    head.push(collect({ ...source(titleEl), selector: 'head > title', tag: 'title', cls: '', href: null, signals: [], text: norm(titleEl.textContent), raw: titleEl.textContent }));
  for (const m of document.querySelectorAll('meta[name="description"], meta[name="keywords"], meta[property="og:title"], meta[property="og:description"]')) {
    const v = norm(m.getAttribute('content'));
    const key = m.hasAttribute('name') ? 'name' : 'property';
    if (v) head.push(collect({ ...source(m), selector: `meta[${key}="${m.getAttribute(key)}"]`, tag: 'meta', cls: '', href: null, signals: [], text: v, raw: m.getAttribute('content') }));
  }

  // Array.from 대신 전개 구문: Prototype.js 등 옛 라이브러리가 Array.from을 덮어써 Set을 빈 배열로 만든다
  const links = [...linkMap];
  return { hidden, texts, links, head, source_blocks };
}
