"""불법광고 탐지기 (프로토타입).

사용: python -m detector.main <진입URL> [--out 폴더(기본: 프로젝트 루트/exe 폴더)] [--max-pages 0(제한 없음)]
      [--time-budget 1620] [--llm-reserve 360] [--model 경로] [--no-ml]
산출: result.json (적격평가 스키마), report.json (판정 근거 포함 내부 리포트)
"""
import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from xml.etree import ElementTree as ET

from playwright.async_api import async_playwright

from . import adscore
from .frontier import Frontier
from .textnorm import homoglyph_signals, jamo_signal, normalize

TOOL_VERSION = "0.1.0-proto"


async def _fetch_sitemap(entry: str, timeout_s: float = 8) -> list[str]:
    """sitemap.xml 에서 URL 목록을 반환. 실패하면 빈 리스트."""
    import urllib.request
    p = urlparse(entry)
    sitemap_url = f"{p.scheme}://{p.netloc}/sitemap.xml"

    def _get():
        try:
            req = urllib.request.Request(sitemap_url,
                                         headers={"User-Agent": "Mozilla/5.0 Ad-Purifier/0.1"})
            with urllib.request.urlopen(req, timeout=timeout_s) as r:
                return r.read() if r.status == 200 else None
        except Exception:
            return None

    xml = await asyncio.to_thread(_get)
    if not xml:
        return []
    try:
        root = ET.fromstring(xml)
        return [loc.text.strip()
                for loc in root.iter('{http://www.sitemaps.org/schemas/sitemap/0.9}loc')
                if loc.text]
    except Exception:
        return []
TOPIC = "불법광고"  # 요강 예시는 "TOPIC" 문자열 — 주최 측 확인 필요
KST = timezone(timedelta(hours=9))
EXTRACT_JS = (Path(__file__).parent / "extract.js").read_text(encoding="utf-8")
SKIP_EXT = (".pdf", ".hwp", ".hwpx", ".doc", ".docx", ".xls", ".xlsx", ".zip", ".jpg", ".jpeg", ".png", ".gif", ".mp4")

# Playwright 기본 UA에는 "HeadlessChrome"이 들어가 일부 사이트가 다른(또는 빈) 화면을 준다.
# 일반 브라우저와 같은 UA·로캘·언어 헤더로 맞춰 실제 사이트에서 정상 문서를 받도록 한다.
# (특정 사이트의 봇 차단을 뚫기 위한 지문 위조가 아니라, 크롤러가 평범한 한국어 클라이언트처럼 보이게 하는 기본 설정이다.)
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")


def _is_hash_route(fragment: str) -> bool:
    """#/path 또는 #!/path 형태의 SPA 해시 라우팅인지 판별."""
    return fragment.startswith('/') or fragment.startswith('!')


def norm_url(url):
    """채점 규칙과 같은 기준: http/https·끝 슬래시·# 무시, 쿼리 순서 무시.
    단, #/route 형태의 해시 라우팅은 경로의 일부이므로 보존한다."""
    p = urlparse(url)
    query = urlencode(sorted(parse_qsl(p.query, keep_blank_values=True)))
    frag = p.fragment if _is_hash_route(p.fragment) else ""
    return urlunparse(("", p.netloc.lower(), p.path.rstrip("/") or "/", "", query, frag))


def site_key(netloc):
    """같은 사이트 비교용 호스트: 대소문자와 맨 앞 'www.'를 무시한다 (example.com == www.example.com)."""
    n = netloc.lower()
    return n[4:] if n.startswith("www.") else n


def crawl_key(url):
    """크롤링 중복 방지용 키. norm_url과 같되 www 유무는 같은 페이지로 본다."""
    k = norm_url(url)
    return "//" + k[6:] if k.startswith("//www.") else k


def strip_fragment(url):
    p = urlparse(url)
    if _is_hash_route(p.fragment):
        return url  # 해시 라우팅은 그대로 유지
    return urlunparse(p._replace(fragment=""))


async def frame_prefix(frame):
    """iframe 체인을 'iframe[src="절대주소"] >>> ' 형식으로."""
    chain = []
    f = frame
    while f.parent_frame is not None:
        el = await f.frame_element()
        src = await el.get_attribute("src") or f.url
        chain.insert(0, f'iframe[src="{urljoin(f.parent_frame.url, src)}"]')
        f = f.parent_frame
    return "".join(c + " >>> " for c in chain)


class PageLoadError(Exception):
    """페이지에 접속하지 못함(연결 거부·DNS 실패·시간 초과 등). 브라우저 오류 화면을 '깨끗한 페이지'로 오판하지 않기 위해 구분한다."""


async def close_page(page):
    """페이지 닫기. 드물게 Chromium 오류 화면에서 close가 응답하지 않아도 전체 탐지가 멈추지 않도록 시간 제한."""
    try:
        await asyncio.wait_for(page.close(), 10)
    except Exception:
        pass


def slash_variant(url):
    """끝 슬래시를 붙인 URL. 붙일 대상이 아니면(이미 '/'로 끝남, 파일 경로) None.

    일부 서버는 '/govpay'를 내부 주소(예: http://192.168.x.x:8080/govpay/)로 잘못 리다이렉트해
    접속이 끊기지만 '/govpay/'는 정상 응답한다.
    """
    p = urlparse(url)
    if not p.path or p.path.endswith("/") or "." in p.path.rsplit("/", 1)[-1]:
        return None
    return urlunparse(p._replace(path=p.path + "/"))


async def open_page(ctx, url, timeout_ms):
    """(페이지, 응답). 접속 실패 시 실패한 페이지는 바로 닫고 새 페이지로 1회 재시도, 그래도 안 되면 PageLoadError.

    재시도는 끝 슬래시를 붙인 주소가 있으면 그 주소로, 없으면 같은 주소로 한다.
    같은 페이지에서 goto를 연달아 실패시키면 오류 화면 전환이 겹쳐 page.close()가 멈추므로 시도마다 새 페이지를 쓴다.
    """
    attempts = [url, slash_variant(url) or url]
    first_err = None
    for attempt, target in enumerate(attempts, 1):
        page = await ctx.new_page()
        try:
            resp = await page.goto(target, wait_until="domcontentloaded", timeout=timeout_ms)
            if target != url:
                print(f"[info] {url} 접속 실패 → 끝 슬래시를 붙인 {target}로 접속")
            return page, resp
        except Exception as e:
            await close_page(page)
            first_err = first_err or e
            if attempt == len(attempts):
                e = first_err  # 원래 주소의 실패 원인을 보고
                raise PageLoadError(f"{type(e).__name__}: {str(e).splitlines()[0]}") from e
            await asyncio.sleep(1)


async def scan_page(ctx, url, timeout_ms, on_site=None):
    """(최종 URL, 프레임별 추출 결과, HTTP 상태). 접속 자체가 안 되면 PageLoadError.

    on_site(최종 URL)가 False면 다른 사이트로 넘어간 것(예: 게시판 link.php가 외부로 302)이므로
    더 기다리지 않고 프레임 목록 자리에 None을 돌려준다.
    """
    t0 = time.perf_counter()
    page, resp = await open_page(ctx, url, timeout_ms)
    try:
        status = resp.status if resp else None
        # about:blank 등 http(s)가 아닌 도착 주소(경고창 후 history.back 하는 오류 페이지 등)는 외부 이동이 아님
        web = page.url.startswith(("http://", "https://"))
        if on_site is not None and web and not on_site(page.url):
            return strip_fragment(page.url), None, status
        try:  # 스크립트로 늦게 삽입되는 광고까지 보도록 네트워크가 잠잠해질 때까지 대기 (남은 시간 안에서)
            left = timeout_ms - (time.perf_counter() - t0) * 1000
            await page.wait_for_load_state("networkidle", timeout=max(1000, left))
        except Exception:
            await page.wait_for_timeout(500)  # networkidle 실패 시 현재 DOM으로 진행
        try:  # 스크롤로 IntersectionObserver 기반 lazy-load 콘텐츠 유도
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(300)
            await page.evaluate("window.scrollTo(0, 0)")
        except Exception:
            pass
        frames = []
        for fr in page.frames:
            try:
                data = await fr.evaluate(EXTRACT_JS)
                data["frame_url"] = fr.url
                frames.append((await frame_prefix(fr), data))
            except Exception:
                continue
        return strip_fragment(page.url if page.url.startswith(("http://", "https://")) else url), frames, status
    finally:
        await close_page(page)


async def crawl(entry, max_pages, concurrency, timeout_ms, progress=None, user_agent=DEFAULT_UA, deadline=None):
    """(페이지 목록, 문제 목록). progress(완료 페이지 수, 발견 페이지 수, 방금 끝난 URL) — UI 진행률 표시용.

    방문 순서는 Frontier가 정한다(게시판·수상한 제목 우선, 나머지도 버리지 않음 — frontier.py 참고).
    max_pages: 방문할 최대 페이지 수(0 = 제한 없음). deadline: time.perf_counter() 기준 크롤링 마감 시각(None = 없음).
    마감이 되면 새 페이지는 열지 않고, 열어 둔 페이지만 마저 검사한 뒤 끝낸다.
    문제 목록: {"failed": 접속 실패 [{url, reason}], "http_errors": 4xx/5xx 응답 [{url, status}],
               "external_skipped": 다른 사이트로 넘어가 제외한 페이지 [{url, final_url}],
               "stopped_by_time": 마감으로 멈췄는지, "left_in_queue": 못 본 대기 주소 수, "dropped_endless": 버린 검색·정렬 주소 수}
    점검 범위는 진입 사이트(www 유무 무시)뿐이다. 링크 주소뿐 아니라 리다이렉트 이후의 최종 주소도 검사한다.
    """
    site = {"host": urlparse(entry).netloc}

    def on_site(u):
        return site_key(urlparse(u).netloc) == site_key(site["host"])

    frontier, results = Frontier(), []
    failed, http_errors, external = [], [], []
    frame_urls = set()  # iframe으로 삽입된 문서: 상위 페이지 기준으로 보고하므로 단독 페이지로 크롤링하지 않음
    frontier.add(entry, crawl_key(entry), depth=0)

    def crawlable(links):
        """같은 사이트의 문서 링크만 [(url, key, text)]로. iframe 문서는 상위 페이지 기준으로 보고하므로 제외."""
        out = []
        for link, text in links:
            p = urlparse(link)
            if p.scheme not in ("http", "https") or not on_site(link) or p.path.lower().endswith(SKIP_EXT):
                continue
            if norm_url(link) not in frame_urls:
                out.append((strip_fragment(link), crawl_key(link), text))
        return out

    # sitemap.xml에 등록된 URL도 대기열에 (링크 탐색으로 닿지 않는 고아 페이지 대응). 우선순위는 다른 주소와 똑같이 매긴다.
    sitemap = await _fetch_sitemap(entry, timeout_s=min(timeout_ms / 1000, 8))
    frontier.add_links(crawlable((su, "") for su in sitemap), 2)
    state = {"inflight": 0, "visited": 0, "stopped_by_time": False}
    t_start = time.perf_counter()
    visit_log = []  # [크롤링 시작 후 초, 최종 URL, 요청 URL] — 시간에 따라 무엇을 찾았는지(발견 곡선)·리디렉트 분석용
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(viewport={"width": 1280, "height": 900}, ignore_https_errors=True,
                                         user_agent=user_agent, locale="ko-KR",
                                         extra_http_headers={"Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7"})

        async def worker():
            while True:
                if deadline is not None and time.perf_counter() >= deadline:
                    state["stopped_by_time"] = state["stopped_by_time"] or len(frontier) > 0
                    return
                if max_pages and state["visited"] >= max_pages:
                    return
                item = frontier.pop()
                if item is None:
                    if state["inflight"] == 0:  # 대기열도 비고 검사 중인 페이지도 없음 → 끝
                        return
                    await asyncio.sleep(0.2)  # 다른 작업자가 새 링크를 찾을 수 있으니 잠시 기다림
                    continue
                url, depth = item
                state["inflight"] += 1
                state["visited"] += 1
                try:
                    is_entry = url == entry  # 시작 주소가 www 등 다른 주소로 넘어가면 그곳을 사이트로 삼는다
                    final_url, frames, status = await scan_page(ctx, url, timeout_ms, None if is_entry else on_site)
                    if is_entry and not on_site(final_url):
                        site["host"] = urlparse(final_url).netloc
                        print(f"[info] 시작 주소가 {site['host']}로 이동 → 이 사이트를 점검 범위로 사용")
                    frontier.seen.add(crawl_key(final_url))  # 리다이렉트 도착지를 또 크롤링하지 않도록
                    if frames is None:  # 다른 사이트로 넘어감: 점검·링크 추적 모두 하지 않는다
                        external.append({"url": url, "final_url": final_url})
                        continue
                    results.append((final_url, frames))
                    visit_log.append([round(time.perf_counter() - t_start, 1), final_url, url])
                    if status and status >= 400:  # 오류 응답이어도 본문은 검사하되 기록은 남김
                        http_errors.append({"url": url, "status": status})
                    frame_urls.update(norm_url(d["frame_url"]) for pre, d in frames if pre)
                    frontier.add_links(crawlable(lt for _, data in frames for lt in data["links"]), depth + 1)
                except Exception as e:  # 한 페이지 실패가 전체를 멈추지 않게 (점검 페이지 수에는 넣지 않음)
                    failed.append({"url": url, "reason": str(e) if isinstance(e, PageLoadError) else f"{type(e).__name__}: {e}"})
                    print(f"[warn] {url}: {e}")
                finally:
                    state["inflight"] -= 1
                    if progress:
                        progress(len(results) + len(failed) + len(external), len(frontier.seen), url)

        await asyncio.gather(*(worker() for _ in range(concurrency)))
        await browser.close()
    # 순서상 iframe 문서가 먼저 큐에 들어갔던 경우까지 정리
    pages = [(u, f) for u, f in results if norm_url(u) not in frame_urls]
    return pages, {"failed": failed, "http_errors": http_errors, "stopped_by_time": state["stopped_by_time"],
                   "left_in_queue": len(frontier), "dropped_endless": frontier.dropped_endless,
                   "skipped_utility": frontier.skipped_utility, "visit_log": visit_log,
                   "external_skipped": external}


def candidates_from(pages, detect_posted=True):
    """페이지 스캔 결과 → (url, location, technique) 후보.

    detect_posted: 숨기지 않고 '게시된' 불법광고도 후보로 잡는다(기법 POSTED). 요강의 추가 탐지 유형.
    은닉 기법(TRANSPARENT/OFFSCREEN/HOMOGLYPH/JAMO)과 달리 '보인다'는 것 자체는 단서가 아니므로,
    룰 점수가 정상 확정(LOW 이하)을 넘는 텍스트만 후보로 올려 오탐·후보 폭증을 막는다.
    """
    cands = {}
    for url, frames in pages:
        for prefix, data in frames:
            for h in data["hidden"]:
                for tech, why in h["signals"]:
                    key = (norm_url(url), prefix + h["selector"], tech)
                    c = cands.setdefault(key, {"url": url, "location": prefix + h["selector"], "technique": tech,
                                               "evidence_text": h["text"], "signals": []})
                    c["signals"].append(why)
            for t in data["texts"]:
                checks = []
                hg = homoglyph_signals(t["raw"])
                if hg:
                    checks.append(("HOMOGLYPH", hg))
                if jamo_signal(t["raw"]):
                    checks.append(("JAMO", ["decomposed-syllables"]))
                for tech, why in checks:
                    key = (norm_url(url), prefix + t["selector"], tech)
                    c = cands.setdefault(key, {"url": url, "location": prefix + t["selector"], "technique": tech,
                                               "evidence_text": t["text"], "signals": []})
                    c["signals"].extend(why)
                if detect_posted and not checks:  # 닮은꼴·자모가 아닌 '보이는' 텍스트: 내용이 광고다울 때만
                    s, kw = adscore.score(normalize(t["text"]))
                    if adscore.verdict(s) != "clean":
                        key = (norm_url(url), prefix + t["selector"], "POSTED")
                        cands.setdefault(key, {"url": url, "location": prefix + t["selector"], "technique": "POSTED",
                                               "evidence_text": t["text"], "signals": [f"keyword:{','.join(kw[:6])}"]})
    return list(cands.values())


def _parent_of(location):
    """'… > ul > a:nth-of-type(8)' → ('… > ul', 'a') : 같은 줄(부모)·같은 태그인지 비교용."""
    head, _, last = location.rpartition(" > ")
    return head, last.split(":nth-of-type(")[0]


def dedupe(cands):
    """같은 페이지에서 같은 광고를 여러 번 보고하는 것을 하나로 합친다. (남길 후보, 합친 후보 목록)

    불법광고 판정(ad)만 대상이며, 아래 경우 하나만 남긴다.
    1) 주소·내용이 같고 위치가 같은 줄 — 한쪽이 다른 쪽을 감싸거나(div > ul 둘 다 숨김 신호), 같은 부모 아래 형제
       (목록에 같은 링크 글자가 두 번) → 은닉 기법이 게시형보다 우선, 그다음 바깥 요소·앞 형제
       단, 같은 요소에 서로 다른 은닉 기법이 함께 잡힌 경우는 둘 다 남긴다
    2) 숨김 블록(은닉 4종) 안의 요소가 게시형으로 따로 잡힌 경우 — 숨김 블록 한 건으로 보고
    """
    def rank(c):  # 남길 우선순위: 은닉 기법 > 게시형, 바깥 요소 > 안쪽, 앞 형제 > 뒤 형제
        return (c["technique"] not in CORE_TECHNIQUES, c["location"].count(" > "), c["location"])

    ads = sorted((c for c in cands if c["verdict"] == "ad"), key=rank)
    kept, dups = [], []
    by_page = {}
    for c in ads:
        text = " ".join(c["evidence_text"].split())
        page = by_page.setdefault(norm_url(c["url"]), [])
        for k in page:
            k_text = " ".join(k["evidence_text"].split())
            if c["location"] == k["location"] and c["technique"] != k["technique"] \
                    and {c["technique"], k["technique"]} <= CORE_TECHNIQUES:
                continue  # 한 요소에 은닉 기법이 둘(예: 투명+화면 밖) — 채점이 기법까지 보므로 둘 다 남긴다
            nested = (c["location"] == k["location"] or c["location"].startswith(k["location"] + " > ")
                      or k["location"].startswith(c["location"] + " > "))
            same_line = _parent_of(c["location"]) == _parent_of(k["location"])
            # 숨김 블록 안의 요소는 글자가 조금 달라도(링크 글자 제외 등) 그 블록의 일부 — 블록 한 건으로 본다
            inside_hidden = nested and k["technique"] in CORE_TECHNIQUES and c["technique"] not in CORE_TECHNIQUES
            if (text == k_text and (nested or same_line)) or inside_hidden:
                c["duplicate_of"] = {"location": k["location"], "technique": k["technique"]}
                dups.append(c)
                break
        else:
            page.append(c)
            kept.append(c)
    dup_ids = {id(c) for c in dups}
    return [c for c in cands if id(c) not in dup_ids], dups


def judge(cands, model_path, use_ml, deadline=None):
    """룰 점수로 1차 판정, 경계 구간만 Laya로 2차 판정.

    deadline(time.perf_counter() 기준)을 넘기면 남은 경계 문구는 Laya 없이 규칙 판정(정상 처리)으로 둔다.
    """
    for c in cands:
        c["normalized"] = normalize(c["evidence_text"])
        c["score"], c["hits"] = adscore.score(c["normalized"])
        c["verdict"] = adscore.verdict(c["score"])
        c["decided_by"] = "rules"
    border = [c for c in cands if c["verdict"] == "borderline"]
    if border and use_ml:
        try:
            from laya.agent import Agent

            from training.make_dataset import QUESTION
            agent = Agent(model_path, device="cpu")
            texts = sorted({c["normalized"] for c in border})
            prob = {}
            for i in range(0, len(texts), 64):  # 64개씩 끊어서, 마감이 지나면 멈춤
                if deadline is not None and time.perf_counter() >= deadline:
                    print(f"[warn] 판정 시간 제한: 경계 문구 {len(texts) - i}개는 AI 판정 없이 정상 처리")
                    break
                chunk = texts[i:i + 64]
                out = agent.predict_batch(chunk, QUESTION, batch_size=16)
                prob.update((t, o["answers"]["is_illegal_ad"]["noul"]) for t, o in zip(chunk, out))
        except Exception as e:  # 모델 문제로 크롤링 결과 전체를 잃지 않도록 규칙 판정(--no-ml과 동일)으로 대체
            print(f"[warn] Laya 판정 실패, 경계 구간은 정상 처리: {type(e).__name__}: {e}")
            prob = None
        for c in border if prob else []:
            if c["normalized"] not in prob:
                continue
            c["ml_prob"] = round(prob[c["normalized"]], 4)
            c["verdict"] = "ad" if c["ml_prob"] >= 0.5 else "clean"
            c["decided_by"] = "laya"
    return cands


def app_dir():
    """결과 파일 기본 위치: exe로 묶였으면 exe 폴더, 아니면 프로젝트 루트 (요강: 구동 파일 폴더에 생성)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


DEFAULT_MODEL = app_dir() / "models" / "laya-illegal-ad"


# 적격평가 대상 4종 (result.json). 그 외 추가 유형은 result_extra.json으로 분리한다.
CORE_TECHNIQUES = {"HOMOGLYPH", "JAMO", "TRANSPARENT", "OFFSCREEN"}
EXTRA_LABEL = {"POSTED": "게시형 불법광고(은닉 없음)"}


# 시간 예산(초). 적격평가 검출 시간 상한은 30분 — 넘기면 시간 점수 0점, result.json이 없으면 평가 제외될 수 있다.
TIME_BUDGET = 27 * 60   # 전체 목표: 30분 상한에서 3분 여유
JUDGE_RESERVE = 2 * 60  # 로컬 판정(룰+Laya)·저장 몫. 실사이트 11곳 측정 시 판정은 0.4~11초
LLM_RESERVE = 6 * 60    # 나중에 붙일 LLM 최종 검증 몫(아직 미구현 — 자리만 확보)


def run(entry_url, out, max_pages=0, concurrency=4, timeout=15000, model=DEFAULT_MODEL, use_ml=True,
        progress=None, phase=None, user_agent=DEFAULT_UA, detect_posted=True,
        time_budget=TIME_BUDGET, llm_reserve=LLM_RESERVE):
    """크롤링 → 판정 → result.json/report.json 저장. report 딕셔너리를 돌려준다.

    phase(단계명): UI에 현재 단계(crawl/judge/save)를 알리는 콜백.
    detect_posted: 게시형(은닉 없는) 불법광고도 탐지해 result_extra.json으로 분리 저장.
    max_pages: 0이면 페이지 수 제한 없이 시간 예산 안에서 전부 탐색.
    time_budget: 전체 목표 시간(초). 크롤링은 time_budget - llm_reserve - 판정 몫까지만 한다.
    """
    phase = phase or (lambda name: None)
    started = datetime.now(KST)
    t0 = time.perf_counter()
    crawl_deadline = t0 + max(60, time_budget - llm_reserve - JUDGE_RESERVE)
    judge_deadline = t0 + max(90, time_budget - llm_reserve - 30)
    phase("crawl")
    pages, issues = asyncio.run(crawl(entry_url, max_pages, concurrency, timeout, progress, user_agent, crawl_deadline))
    t_crawl = time.perf_counter() - t0
    phase("judge")
    cands = judge(candidates_from(pages, detect_posted), str(model), use_ml, judge_deadline)
    cands, dups = dedupe(cands)
    finished = datetime.now(KST)
    elapsed = round(time.perf_counter() - t0, 2)

    phase("save")
    hits = sorted((c for c in cands if c["verdict"] == "ad"), key=lambda c: (c["url"], c["location"], c["technique"]))
    meta = {"topic": TOPIC, "entry_url": entry_url, "started_at": started.isoformat(timespec="seconds"),
            "finished_at": finished.isoformat(timespec="seconds"), "elapsed_sec": elapsed, "tool_version": TOOL_VERSION}
    # 적격평가 4종 → result.json, 그 외(게시형 등) → result_extra.json (technique="ETC" 고정)
    core_hits = [c for c in hits if c["technique"] in CORE_TECHNIQUES]
    extra_hits = [c for c in hits if c["technique"] not in CORE_TECHNIQUES]
    findings = [{"id": f"f_{i:03d}", "url": c["url"], "is_violation": True, "location": c["location"],
                 "evidence_text": c["evidence_text"], "technique": c["technique"]} for i, c in enumerate(core_hits, 1)]
    extra_findings = [{"id": f"f_{i:03d}", "url": c["url"], "location": c["location"],
                       "evidence_text": c["evidence_text"], "technique": "ETC",
                       "extra_finding": EXTRA_LABEL.get(c["technique"], c["technique"])}
                      for i, c in enumerate(extra_hits, 1)]
    # 접속 실패 정보는 내부 리포트에만 기록 (result.json은 요강 스키마 그대로 유지)
    entry_failed = any(f["url"] == entry_url for f in issues["failed"])
    report = {"meta": meta, "pages": len(pages), "crawl_sec": round(t_crawl, 2), "entry_failed": entry_failed,
              "failed_pages": issues["failed"], "http_errors": issues["http_errors"],
              "stopped_by_time": issues["stopped_by_time"], "left_in_queue": issues["left_in_queue"],
              "dropped_endless": issues["dropped_endless"], "skipped_utility": issues["skipped_utility"], "time_budget": time_budget, "llm_reserve": llm_reserve,
              "visit_log": issues["visit_log"], "external_skipped": issues["external_skipped"],
              "extra_count": len(extra_findings), "candidates": cands,
              # 중복으로 합친 탐지 (같은 주소·내용·줄) — 어디에 합쳐졌는지(duplicate_of)만 남긴다
              "duplicates": [{"url": c["url"], "location": c["location"], "technique": c["technique"],
                              "evidence_text": c["evidence_text"], "duplicate_of": c["duplicate_of"]} for c in dups]}
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "result.json").write_text(json.dumps({"meta": meta, "findings": findings}, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    # result_extra.json: 추가 유형이 있을 때만 생성 (적격평가 제출물과 섞이지 않도록 별도 파일)
    extra_path = out / "result_extra.json"
    if extra_findings:
        extra_path.write_text(json.dumps({"meta": meta, "findings": extra_findings}, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    elif extra_path.exists():
        extra_path.unlink()  # 지난 실행의 result_extra.json이 남아 오해를 주지 않도록 정리
    return report


def main():
    # Windows 콘솔(cp949)에서 한글·특수문자 출력 시 UnicodeEncodeError 방지
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("entry_url")
    ap.add_argument("--out", default=str(app_dir()))
    ap.add_argument("--max-pages", type=int, default=0, help="방문할 최대 페이지 수 (기본 0 = 제한 없음, 시간 예산까지 전부)")
    ap.add_argument("--time-budget", type=int, default=TIME_BUDGET, help=f"전체 목표 시간(초, 기본 {TIME_BUDGET})")
    ap.add_argument("--llm-reserve", type=int, default=LLM_RESERVE, help=f"LLM 검증 몫으로 남길 시간(초, 기본 {LLM_RESERVE})")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--timeout", type=int, default=15000)
    ap.add_argument("--model", default=str(DEFAULT_MODEL))
    ap.add_argument("--no-ml", action="store_true", help="경계 구간을 Laya 없이 정상으로 처리")
    ap.add_argument("--user-agent", default=DEFAULT_UA,
                    help="크롤러 User-Agent (기본: 일반 Chrome). 점검 기관에 신원을 밝혀야 하면 뒤에 도구명을 덧붙여 쓰세요.")
    ap.add_argument("--no-extra", action="store_true",
                    help="게시형(은닉 없는) 불법광고 탐지를 끄고 적격평가 4종만 탐지")
    args = ap.parse_args()

    report = run(args.entry_url, args.out, args.max_pages, args.concurrency, args.timeout, args.model, not args.no_ml,
                 user_agent=args.user_agent, detect_posted=not args.no_extra,
                 time_budget=args.time_budget, llm_reserve=args.llm_reserve)
    cands = report["candidates"]
    findings = sum(c["verdict"] == "ad" for c in cands)
    by = {}
    for c in cands:
        by.setdefault((c["decided_by"], c["verdict"]), 0)
        by[(c["decided_by"], c["verdict"])] += 1
    print(f"pages={report['pages']} candidates={len(cands)} findings={findings} "
          f"(적격 4종 {findings - report['extra_count']} + 게시형 {report['extra_count']}) "
          f"elapsed={report['meta']['elapsed_sec']}s (crawl {report['crawl_sec']:.1f}s)")
    print("판정 분포:", {f"{k[0]}/{k[1]}": v for k, v in sorted(by.items())})
    if report["stopped_by_time"]:
        print(f"[warn] 시간 예산으로 크롤링을 멈춤 — 아직 못 본 대기 주소 {report['left_in_queue']}개")
    if report["dropped_endless"]:
        print(f"[info] 검색·정렬·달력 주소 {report['dropped_endless']}개는 같은 형태가 많아 건너뜀")
    if report["failed_pages"]:
        print(f"[warn] 접속 실패 {len(report['failed_pages'])}페이지 (report.json의 failed_pages 참고)")
    if report["entry_failed"]:
        print(f"[error] 진입 페이지에 접속하지 못했습니다: {args.entry_url} — 결과(0건)를 신뢰할 수 없습니다.")
        sys.exit(2)


if __name__ == "__main__":
    main()
