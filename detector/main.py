"""불법광고 탐지기 (프로토타입).

사용: python -m detector.main <진입URL> [--out 폴더(기본: 프로젝트 루트/exe 폴더)]
      [--time-budget 1740] [--llm-reserve 480] [--model 경로] [--no-ml]
산출: result.json (적격평가 스키마), report.json (판정 근거 포함 내부 리포트)
"""
import argparse
import asyncio
import collections
import json
import hashlib
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from xml.etree import ElementTree as ET

from playwright.async_api import async_playwright

from app_paths import app_root, resource_root, laya_model

from . import adscore
from .frontier import Frontier
from .textnorm import homoglyph_signals, jamo_signal, normalize

TOOL_VERSION = "0.2.0-preview.1"


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
                if r.status != 200:
                    return None
                data = r.read(SITEMAP_MAX_BYTES + 1)  # 끝없이 큰 응답으로 메모리를 채우지 못하게
                return data if len(data) <= SITEMAP_MAX_BYTES else None
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
SITEMAP_MAX_BYTES = 20 * 1024 * 1024
TOPIC = "불법광고"  # 요강 예시는 "TOPIC" 문자열 — 주최 측 확인 필요
KST = timezone(timedelta(hours=9))
EXTRACT_JS = (resource_root() / "detector" / "extract.js").read_text(encoding="utf-8")
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


# 목록에서 몇 쪽째로 들어왔는지만 나타내는 주소 인자 — 값이 달라도 같은 글이다 (hrd: 같은 글을 startPage만 다른 주소로 74번 재방문)
PAGING_PARAMS = re.compile(r"^(page|pageno|pagenum|pageindex|startpage|gotopage|cpage|curpage|currentpage|pg|listpage)$", re.I)


# 글 번호 인자 (dataSid=13045, idx=…, wr_id=…, bSeq=…) — 숫자 값일 때만 글 보기 주소로 본다
ITEM_PARAMS = re.compile(r"(sid|idx|seq|no|id|num|number|uid|nttsn)$", re.I)


def page_key(url):
    """중복 판단용 '같은 페이지' 키: 글 보기 주소(숫자 글 번호 인자가 있음)면 목록 위치 인자(PAGING_PARAMS)를 뺀다.
    글 번호가 없는 목록 주소(목록 1쪽·2쪽)는 내용이 다른 페이지이므로 그대로 둔다."""
    p = urlparse(norm_url(url))
    q = parse_qsl(p.query, keep_blank_values=True)
    if not any(ITEM_PARAMS.search(k) and v.isdigit() and not PAGING_PARAMS.match(k) for k, v in q):
        return norm_url(url)
    return urlunparse(p._replace(query=urlencode([(k, v) for k, v in q if not PAGING_PARAMS.match(k)])))


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


ENTRY_TIMEOUT_MS = 45000  # 시작 페이지 접속 제한 시간 (일반 페이지는 --timeout, 기본 15초)
# 느린 사이트 적응: 최근 SLOW_WINDOW번 방문 중 절반 이상이 시간 초과면 '느린 모드' — 제한 시간을 늘리고 동시 요청을 줄인다.
# s-solution은 응답에만 8~10초가 걸려, 탭 4개로 동시에 열면 15초를 넘겨 51쪽 중 49쪽이 실패했다.
SLOW_WINDOW, SLOW_TIMEOUT_MS, SLOW_CONCURRENCY = 10, 45000, 2
# 페이지 한 개에서 프레임 추출(extract.js)에 쓸 수 있는 시간. evaluate에는 Playwright 제한 시간이 없어,
# 페이지 스크립트나 추출이 끝나지 않으면 작업자가 영원히 기다렸다 (연수구청 synap 문서 미리보기 — 526쪽에서 멈춤).
EXTRACT_TIMEOUT_S = 30
# 크롤링 마감 뒤 진행 중인 페이지를 기다리는 시간. 접속(느린 모드 45초)+추출(30초)+닫기(10초)보다 길게 두고,
# 그래도 끝나지 않으면 브라우저를 닫아 크롤링을 끝낸다.
DEADLINE_GRACE_S = 90


class ScanCancelled(Exception):
    """사용자가 탐지를 취소함. 결과 파일(result.json·report.json)은 쓰지 않는다."""


# 접속하면 파일을 내려주는 주소(down.php 등): 다운로드를 막아 두어(accept_downloads=False) goto가 이 오류로 끝난다.
# 웹페이지가 아니라 광고가 있을 수 없으므로 접속 실패가 아니라 '다운로드 링크(건너뜀)'으로 센다
DOWNLOAD_ERROR = "Download is starting"


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


# ---------- 크롤러 보안: 점검하는 페이지에 심어진 악성 코드로부터 이 컴퓨터를 지킨다 ----------
# 불법광고가 심어진 페이지에는 도박·성인 사이트의 스크립트나 iframe이 함께 들어 있을 수 있다. 크롤러는 글자만 읽으면 되므로
# 그 밖의 브라우저 기능은 최대한 닫는다. 같은 사이트 문서의 스크립트는 그대로 둔다 — 스크립트로 늦게 끼워 넣는 광고도 봐야 하므로.
# 다른 사이트 iframe 문서: 글자는 읽되 스크립트·플러그인은 못 돌게 하는 보안 정책(CSP)을 붙여 준다
FOREIGN_FRAME_CSP = "script-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'"
FOREIGN_FRAME_TIMEOUT_MS = 10000
# 글자 탐지에 필요 없고 공격 면만 넓히는 요청 종류 (동영상·음성, 웹 앱 매니페스트)
BLOCKED_TYPES = {"media", "manifest"}
# 팝업 창(window.open)으로 다른 사이트를 여는 것을 막는다. 그래도 열린 창은 on_popup이 바로 닫는다
NO_POPUP_JS = "window.open = () => null;"
# 다시 보낼 때 빼는 응답 헤더: route.fetch가 압축을 풀어 주므로 원래 길이·압축 표시가 남으면 문서가 깨진다
HOP_HEADERS = {"content-encoding", "content-length", "transfer-encoding", "connection"}


async def launch_browser(pw):
    """Chromium을 운영체제 샌드박스 안에서 실행한다 (Playwright 기본값은 샌드박스 꺼짐).

    샌드박스는 브라우저 취약점이 뚫려도 페이지 코드가 파일·프로그램에 손대지 못하게 막는 마지막 방어선이다.
    샌드박스를 지원하지 않는 환경(일부 리눅스 컨테이너 등)에서는 경고를 남기고 끈 채로 실행한다.
    """
    try:
        return await pw.chromium.launch(headless=True, chromium_sandbox=True)
    except Exception as e:
        print(f"[warn] 브라우저 샌드박스를 켤 수 없어 끄고 실행합니다: {str(e).splitlines()[0]}")
        return await pw.chromium.launch(headless=True)


async def secure_context(browser, user_agent):
    """크롤링용 브라우저 컨텍스트. 다운로드·서비스 워커·팝업을 막고 요청마다 secure_route를 거친다.

    권한(위치·카메라·알림 등)은 하나도 주지 않는다(기본값). 쿠키·저장소는 이 컨텍스트에만 있고 점검이 끝나면 사라진다.
    """
    ctx = await browser.new_context(viewport={"width": 1280, "height": 900}, ignore_https_errors=True,
                                    accept_downloads=False, service_workers="block",
                                    user_agent=user_agent, locale="ko-KR",
                                    extra_http_headers={"Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7"})
    await ctx.add_init_script(NO_POPUP_JS)
    ctx.on("page", on_popup)
    await ctx.route("**/*", secure_route)
    return ctx


async def on_popup(page):
    """크롤러가 연 페이지가 아니라 페이지 스크립트가 띄운 창이면 바로 닫는다."""
    try:
        if await page.opener() is not None:
            await page.close()
    except Exception:
        pass


async def secure_route(route, request):
    """모든 요청이 마지막으로 거치는 보안 규칙 (guard_navigation 다음)."""
    try:
        if request.resource_type in BLOCKED_TYPES:
            return await route.abort()
        frame = request.frame
        if request.is_navigation_request() and frame.parent_frame is not None:
            top = urlparse(frame.page.url).netloc
            if top and site_key(urlparse(request.url).netloc) != site_key(top):
                return await fulfill_without_scripts(route)
    except Exception:  # 서비스 워커 요청 등 frame이 없는 요청은 그대로 보낸다
        pass
    await route.continue_()


async def fulfill_without_scripts(route):
    """다른 사이트 iframe 문서를 대신 받아, 스크립트를 금지하는 CSP를 붙여 돌려준다. 받지 못하면 iframe을 비운다."""
    try:
        resp = await route.fetch(timeout=FOREIGN_FRAME_TIMEOUT_MS)
    except Exception:
        return await route.fulfill(status=204, body="")
    headers = {k: v for k, v in resp.headers.items() if k.lower() not in HOP_HEADERS}
    old = headers.pop("content-security-policy", None)
    # 정책을 쉼표로 이으면 둘 다 적용된다 — 사이트 원래 정책을 약하게 바꾸지 않고 우리 정책만 더한다
    headers["content-security-policy"] = f"{old}, {FOREIGN_FRAME_CSP}" if old else FOREIGN_FRAME_CSP
    await route.fulfill(response=resp, headers=headers)


async def guard_navigation(page, on_site, blocked):
    """페이지가 스크립트로 다른 사이트로 넘어가려 하면 막는다(blocked에 그 주소를 기록).

    공공 사이트에 심어진 기생 페이지는 열자마자 성인·도박 사이트로 넘긴다. 막지 않으면 크롤러가 광고 사이트에 접속하고,
    점검 대상인 기생 페이지 자체는 '다른 사이트로 이동'으로 빠져 검사되지 않는다. 문서 요청만 보고 나머지는 바로 통과시킨다.
    서버 리다이렉트(302)는 브라우저가 처리해 여기서 막히지 않으므로 scan_page의 최종 주소 확인이 따로 맡는다.
    """
    async def handler(route, request):
        if (request.resource_type == "document" and request.frame == page.main_frame
                and request.url.startswith(("http://", "https://")) and not on_site(request.url)):
            blocked.append(request.url)
            # abort하면 크롬이 오류 화면으로 바꿔 원래 문서가 사라진다. 204(내용 없음)로 답하면 이동만 취소되고 그대로 남는다.
            await route.fulfill(status=204, body="")
        else:
            await route.fallback()  # 크롤러 보안 규칙(secure_route)으로 넘김
    await page.route("**/*", handler)


async def open_page(ctx, url, timeout_ms, on_site=None, blocked=None):
    """(페이지, 응답). 접속 실패 시 실패한 페이지는 바로 닫고 새 페이지로 1회 재시도, 그래도 안 되면 PageLoadError.

    재시도는 끝 슬래시를 붙인 주소가 있으면 그 주소로, 없으면 같은 주소로 한다.
    같은 페이지에서 goto를 연달아 실패시키면 오류 화면 전환이 겹쳐 page.close()가 멈추므로 시도마다 새 페이지를 쓴다.
    on_site를 주면 다른 사이트로 넘어가는 스크립트 이동을 막고 blocked에 기록한다(guard_navigation).
    """
    attempts = [url, slash_variant(url) or url]
    first_err = None
    for attempt, target in enumerate(attempts, 1):
        page = await ctx.new_page()
        if on_site is not None:
            await guard_navigation(page, on_site, blocked if blocked is not None else [])
        try:
            resp = await page.goto(target, wait_until="domcontentloaded", timeout=timeout_ms)
            if target != url:
                print(f"[info] {url} 접속 실패 → 끝 슬래시를 붙인 {target}로 접속")
            return page, resp
        except Exception as e:
            await close_page(page)
            first_err = first_err or e
            if attempt == len(attempts) or DOWNLOAD_ERROR in str(e):  # 파일 응답은 다른 주소로 다시 시도하지 않는다
                e = first_err  # 원래 주소의 실패 원인을 보고
                raise PageLoadError(f"{type(e).__name__}: {str(e).splitlines()[0]}") from e
            await asyncio.sleep(1)


async def scan_page(ctx, url, timeout_ms, on_site=None, guard=None):
    """(최종 URL, 프레임별 추출 결과, HTTP 상태). 접속 자체가 안 되면 PageLoadError.

    on_site(최종 URL)가 False면 다른 사이트로 넘어간 것(예: 게시판 link.php가 외부로 302)이므로
    더 기다리지 않고 프레임 목록 자리에 None을 돌려준다.
    guard: 스크립트로 다른 사이트로 넘어가는 이동을 막을 기준 (없으면 on_site). 시작 페이지는 on_site 없이 guard만 준다 —
    서버 리다이렉트(www 추가·도메인 이전)는 따라가되, 기생 페이지의 스크립트 이동은 막기 위해서다.
    """
    t0 = time.perf_counter()
    blocked = []
    page, resp = await open_page(ctx, url, timeout_ms, guard or on_site, blocked)
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
        extract_until = time.perf_counter() + EXTRACT_TIMEOUT_S
        try:  # 스크롤로 IntersectionObserver 기반 lazy-load 콘텐츠 유도
            await asyncio.wait_for(page.evaluate("window.scrollTo(0, document.body.scrollHeight)"), 5)
            await page.wait_for_timeout(300)
            await asyncio.wait_for(page.evaluate("window.scrollTo(0, 0)"), 5)
        except Exception:
            pass
        frames = []
        for frame_index, fr in enumerate(page.frames):
            try:
                # 페이지의 모든 프레임이 추출 시간 하나를 나눠 쓴다. 넘으면 그 프레임은 수집 실패로 남긴다(frame_failures).
                left = extract_until - time.perf_counter()
                if left <= 0:
                    raise TimeoutError(f"프레임 추출 {EXTRACT_TIMEOUT_S}초 초과")
                try:
                    data = await asyncio.wait_for(fr.evaluate(EXTRACT_JS), left)
                except asyncio.TimeoutError:
                    raise TimeoutError(f"프레임 추출 {EXTRACT_TIMEOUT_S}초 초과") from None
                data["frame_url"] = fr.url
                namespace = hashlib.sha256(f"{url}|{page.url}|{frame_index}|{fr.url}".encode()).hexdigest()[:24]
                for group in ("hidden", "texts", "head", "source_blocks"):
                    for row in data.get(group, []):
                        if row.get("source_id"):
                            row["source_id"] = namespace + ":" + row["source_id"]
                frames.append((await frame_prefix(fr), data))
            except Exception as exc:
                frames.append(("", {"frame_url": fr.url, "hidden": [], "texts": [], "head": [], "links": [],
                                    "source_blocks": [], "collection_error": f"{type(exc).__name__}: {exc}"}))
        if blocked and frames:  # 다른 사이트로 넘기려던 페이지 — 판정 때 그 페이지 문구에 가산점 (candidates_from)
            frames[0][1]["blocked_nav"] = sorted({urlparse(b).netloc for b in blocked})
        return strip_fragment(page.url if page.url.startswith(("http://", "https://")) else url), frames, status
    finally:
        await close_page(page)


async def crawl(entry, concurrency, timeout_ms, progress=None, user_agent=DEFAULT_UA, deadline=None, cancel=None):
    """(페이지 목록, 문제 목록). progress(완료 페이지 수, 발견 페이지 수, 방금 끝난 URL) — UI 진행률 표시용.

    방문 순서는 Frontier가 정한다(게시판·수상한 제목 우선, 나머지도 버리지 않음 — frontier.py 참고).
    페이지 수 제한은 없다. deadline: time.perf_counter() 기준 크롤링 마감 시각(None = 없음).
    마감이 되면 새 페이지는 열지 않고, 열어 둔 페이지만 마저 검사한 뒤 끝낸다.
    cancel: threading.Event. 켜지면 브라우저를 바로 닫아 열려 있던 페이지 대기까지 끊고 돌아온다(결과는 버림 — run이 처리).
    문제 목록: {"failed": 접속 실패 [{url, reason}], "http_errors": 4xx/5xx 응답 [{url, status}], "downloads": 파일 다운로드 주소(건너뜀) [{url}],
               "external_skipped": 다른 사이트로 넘어가 제외한 페이지 [{url, final_url}],
               "stopped_by_time": 마감으로 멈췄는지, "left_in_queue": 못 본 대기 주소 수, "dropped_endless": 버린 검색·정렬 주소 수}
    점검 범위는 진입 사이트(www 유무 무시)뿐이다. 링크 주소뿐 아니라 리다이렉트 이후의 최종 주소도 검사한다.
    """
    site = {"host": urlparse(entry).netloc}

    def on_site(u):
        return site_key(urlparse(u).netloc) == site_key(site["host"])

    frontier, results = Frontier(), []
    failed, http_errors, external, downloads = [], [], [], []
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
    state = {"inflight": 0, "stopped_by_time": False, "slow": False, "forced_stop": False}
    recent = collections.deque(maxlen=SLOW_WINDOW)  # 최근 방문의 시간 초과 여부
    retried = set()  # 시간 초과로 한 번 다시 시도한 주소
    t_start = time.perf_counter()
    visit_log = []  # [크롤링 시작 후 초, 최종 URL, 요청 URL] — 시간에 따라 무엇을 찾았는지(발견 곡선)·리디렉트 분석용
    async with async_playwright() as pw:
        browser = await launch_browser(pw)
        ctx = await secure_context(browser, user_agent)

        async def worker(idx):
            while True:
                if cancel is not None and cancel.is_set():
                    return
                if deadline is not None and time.perf_counter() >= deadline:
                    state["stopped_by_time"] = state["stopped_by_time"] or len(frontier) > 0
                    return
                if state["slow"] and idx >= SLOW_CONCURRENCY:  # 느린 모드: 탭 수를 줄여 서버 부담을 덜어 줌
                    if not len(frontier) and state["inflight"] == 0:
                        return
                    await asyncio.sleep(0.5)
                    continue
                item = frontier.pop()
                if item is None:
                    if state["inflight"] == 0:  # 대기열도 비고 검사 중인 페이지도 없음 → 끝
                        return
                    await asyncio.sleep(0.2)  # 다른 작업자가 새 링크를 찾을 수 있으니 잠시 기다림
                    continue
                url, depth = item
                state["inflight"] += 1
                try:
                    is_entry = url == entry  # 시작 주소가 www 등 다른 주소로 넘어가면 그곳을 사이트로 삼는다
                    # 시작 페이지를 못 열면 점검 전체가 0건이 된다 — 첫 응답만 8~10초 걸리는 사이트(s-solution 등)도 있어 넉넉히 기다린다
                    page_timeout = max(timeout_ms, ENTRY_TIMEOUT_MS) if is_entry else \
                        max(timeout_ms, SLOW_TIMEOUT_MS) if state["slow"] else timeout_ms
                    # 시작 페이지도 스크립트 이동은 막는다 — 시작 주소가 기생 페이지면 성인 사이트로 넘어가 그 사이트를
                    # 점검 범위로 삼던 문제(bom.or.kr → bam99.club). 서버 리다이렉트(www 등)는 그대로 따라간다
                    final_url, frames, status = await scan_page(ctx, url, page_timeout, None if is_entry else on_site,
                                                                guard=on_site)
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
                        http_errors.append({"url": url, "status": status, "final_url": final_url})
                    frame_urls.update(norm_url(d["frame_url"]) for pre, d in frames if pre)
                    frontier.add_links(crawlable(lt for _, data in frames for lt in data["links"]), depth + 1)
                    recent.append(False)
                except Exception as e:  # 한 페이지 실패가 전체를 멈추지 않게 (점검 페이지 수에는 넣지 않음)
                    if state["forced_stop"]:  # 마감 강제 종료로 닫힌 페이지: 실패가 아니라 시간 안에 못 본 페이지
                        state["stopped_by_time"] = True
                        continue
                    if DOWNLOAD_ERROR in str(e) and url != entry:  # 파일 다운로드 링크: 접속 실패로 세지 않음
                        downloads.append({"url": url})
                        continue
                    timed_out = "Timeout" in str(e)
                    recent.append(timed_out)
                    if (not state["slow"] and len(recent) >= SLOW_WINDOW // 2
                            and sum(recent) * 2 >= len(recent)):
                        state["slow"] = True
                        print(f"[info] 시간 초과가 잦아 느린 모드로 전환 (제한 {SLOW_TIMEOUT_MS // 1000}초, 동시 {SLOW_CONCURRENCY}개)")
                    if timed_out and url not in retried:  # 시간 초과는 한 번 더 (느린 모드라면 늘어난 제한 시간으로)
                        retried.add(url)
                        frontier.requeue(url, depth)
                    else:
                        failed.append({"url": url, "reason": str(e) if isinstance(e, PageLoadError) else f"{type(e).__name__}: {e}"})
                        print(f"[warn] {url}: {e}")
                finally:
                    state["inflight"] -= 1
                    if progress:
                        progress(len(results) + len(failed) + len(external) + len(downloads), len(frontier.seen), url)

        async def watch_stop():
            # 취소되거나, 마감 뒤 DEADLINE_GRACE_S가 지나도 끝나지 않은 페이지가 있으면 브라우저를 닫는다.
            # 열려 있던 페이지 접속·대기가 곧바로 오류로 끝나 작업자들이 빠져나온다.
            while not (cancel is not None and cancel.is_set()):
                if deadline is not None and time.perf_counter() >= deadline + DEADLINE_GRACE_S:
                    state["forced_stop"] = True
                    print(f"[warn] 크롤링 마감 {DEADLINE_GRACE_S}초 뒤에도 끝나지 않은 페이지가 있어 강제 종료")
                    break
                await asyncio.sleep(0.2)
            await browser.close()

        watcher = asyncio.create_task(watch_stop()) if cancel is not None or deadline is not None else None
        await asyncio.gather(*(worker(i) for i in range(concurrency)))
        if watcher:
            watcher.cancel()
        await browser.close()
    # 순서상 iframe 문서가 먼저 큐에 들어갔던 경우까지 정리
    pages = [(u, f) for u, f in results if norm_url(u) not in frame_urls]
    return pages, {"failed": failed, "http_errors": http_errors, "downloads": downloads, "stopped_by_time": state["stopped_by_time"],
                   "left_in_queue": len(frontier), "dropped_endless": frontier.dropped_endless,
                   "skipped_utility": frontier.skipped_utility, "visit_log": visit_log,
                   "external_skipped": external, "slow_mode": state["slow"], "retried": len(retried)}


BODY_CONTEXT = True  # 본문 맥락 감점 사용 (평가 비교용 스위치)
LINK_FARM_MIN = 3      # 같은 외부 사이트로 가는 본문 링크가 이만큼 이상이고
LINK_FARM_TEXT_MAX = 30  # 링크 글자가 짧은 키워드(이 글자 수 이하)이며
LINK_FARM_BONUS = 4    # 그중 하나라도 광고 키워드면 → 그 사이트로 가는 링크 전부 확정 (10-22: 이력 40건에서 링크 농장은 스팸 글에만 있었고,
                       #   3점일 땐 '강남'·'안마' 같은 짧은 링크 글자를 Laya가 정상으로 넘김 — counselpsy 수천 건 놓침)
REDIRECT_BONUS = 2     # 페이지가 스크립트로 다른 사이트로 넘기려 함(기생 페이지) → 광고 근거가 있는 문구에 가산점
HEAD_TEXT_MIN = 10     # 본문 글자 요소가 이보다 적은 페이지는 제목·메타 태그 문구도 검사
UI_CONTROL = re.compile(r"신고하기|신고 하기|고객센터|관련\s*글|목록|이전\s*글|다음\s*글|댓글|로그인|회원가입|바로가기")
# 메뉴·탭·모바일 전용 같은 UI 부품은 display:none으로 접혀 있다가 누르면 보인다 — 숨김 광고가 아니다.
# 클래스·태그 토큰이 부품 이름일 때만 본다 ('data-nav-hidden'처럼 이름 중간에 섞인 것은 아님).
# 2026-10-20 외부 시험: gnb-submenu·util-menu mobile-only·탭 패널·breadcrumb의 '카지노 교육기관' 같은 항목을 Laya가 숨김 광고로 올리던 오탐
UI_WIDGET = re.compile(r"^(?:gnb|lnb|snb|nav|navi|navbar|menu|submenu|dropdown|tab|tabs|accordion|modal|popup|mobile|util|allmenu|breadcrumbs?)(?:$|[-_\d])"
                       r"|[-_](?:menu|submenu|tab|tabs|dropdown)$", re.I)


def ui_widget_hidden(c):
    """숨김 후보가 UI 부품을 display:none으로 접어 둔 것뿐인가 (다른 숨김 신호가 함께 있으면 아님)."""
    if set(c.get("signals", [])) != {"display:none"}:
        return False
    return any(UI_WIDGET.search(tok) for tok in re.split(r"[\s>.#:()\[\]=\"']+", c["location"]) if tok)


def link_farms(page_url, texts):
    """본문에 끼워 넣은 광고 링크 묶음 찾기 → {링크 글자 항목 id: 외부 호스트}.

    공공 게시판 스팸의 전형: 정상 글(등산 정보 등) 문장 사이사이에 같은 외부 사이트로 가는 <a>를 여러 개 넣고
    링크 글자만 '출장마사지·홈타이·토닥이…'로 바꾼다 (C-11·C-12). 링크 글자 하나만 보면 '마사지'처럼 약하지만
    '같은 외부 사이트로 가는 짧은 링크 여러 개 + 그중 광고 키워드'라는 묶음 자체가 강한 신호다.
    관련 사이트·SNS 목록(서로 다른 사이트, 같은 글자 반복)은 조건(같은 호스트·서로 다른 글자 2개 이상)에서 걸러진다.
    """
    host = site_key(urlparse(page_url).netloc)
    by_host = {}
    for t in texts:
        href = t.get("href") or ""
        h = site_key(urlparse(href).netloc) if href.startswith(("http://", "https://")) else ""
        if h and h != host and len(t["text"]) <= LINK_FARM_TEXT_MAX:
            by_host.setdefault(h, []).append(t)
    farms = {}
    for h, ts in by_host.items():
        if len(ts) < LINK_FARM_MIN or len({t["text"] for t in ts}) < 2:
            continue
        if any(adscore.score(normalize(t["text"]))[0] >= 2.5 for t in ts):
            farms.update((id(t), h) for t in ts)
    return farms


def source_fields(row):
    """수집 원문은 점수용 context와 분리해 보존한다."""
    fields = ("source_id", "source_html", "source_context", "source_near_text",
              "raw", "href", "tag", "cls", "attr", "inspection")
    result = {k: row[k] for k in fields if k in row}
    if isinstance(row.get("source_html"), str):
        value = {"html": row["source_html"], "context": row.get("source_context", "")}
        result["source_sha256"] = hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return result


def candidates_from(pages, detect_posted=True, *, recover_posted=False):
    """페이지 스캔 결과 → (url, location, technique) 후보.

    detect_posted: 숨기지 않고 '게시된' 불법광고도 후보로 잡는다(기법 POSTED). 요강의 추가 탐지 유형.
    은닉 기법(TRANSPARENT/OFFSCREEN/HOMOGLYPH/JAMO)과 달리 '보인다'는 것 자체는 단서가 아니므로,
    룰 점수가 정상 확정(LOW 이하)을 넘는 텍스트나 본문 삽입 링크 묶음(link_farms)만 후보로 올려 오탐·후보 폭증을 막는다.
    """
    cands = {}
    for url, frames in pages:
        for prefix, data in frames:
            farms = link_farms(data.get("frame_url") or url, data["texts"]) if detect_posted else {}
            redirect = [(f"external-redirect:{h}", REDIRECT_BONUS) for h in data.get("blocked_nav", [])][:1]
            for h in data["hidden"]:
                for tech, why in h["signals"]:
                    key = (norm_url(url), prefix + h["selector"], tech, h.get("source_id"), h.get("attr", ""), h["text"], h["raw"])
                    c = cands.setdefault(key, {"url": url, "location": prefix + h["selector"], "technique": tech,
                                               "evidence_text": h["text"], "signals": []})
                    c.update(source_fields(h))
                    c["signals"].append(why)
            # 제목·메타(head)는 본문이 거의 없거나 다른 사이트로 넘기려던 페이지(기생 페이지)에서만 본다.
            # 보통 게시판 페이지는 제목이 게시글 제목과 같아 같은 문구를 두 번 보고하게 되므로 쓰지 않는다.
            all_head = data.get("head", [])
            title = next((h["text"] for h in all_head if h["selector"] == "head > title"), "")
            # 제목·메타(head)는 본문이 거의 없거나 다른 사이트로 넘기려던 페이지(기생 페이지), 또는 제목만 광고로 바꿔치기된
            # 페이지(제목이 광고 점수인데 본문에 그 제목이 없음)에서만 본다. 보통은 게시글 제목과 겹쳐 같은 문구를 두 번 보고하게 된다.
            body = {t["text"] for t in data["texts"]}
            swapped = adscore.score(normalize(title))[0] >= adscore.HIGH and not any(title in b or b in title for b in body if len(b) >= 8)
            head = all_head if (redirect or swapped or len(data["texts"]) < HEAD_TEXT_MIN) else []
            # 페이지 맥락: 문서 제목 + 본문의 제목 요소(h1~h3, class에 tit·subject·title). 사이트 공통 <title>("공지사항")만으로는 부족
            # 버튼·메뉴 문구('게시글 신고하기', '고객센터')는 제외하고 앞쪽 제목 3개만 — cafe24 게시판마다 있는
            # '게시글 신고하기' 버튼이 소제목으로 잡혀 스팸 페이지 전체가 '신고' 감점을 받던 문제(thetoollab 518쪽 중 181쪽)
            # 본문 제목일 가능성이 높은 것(h1, class에 tit·subject·headline)을 먼저, 그다음 h2·h3 ('Global Menu' 같은 메뉴 제목이 자리를 차지하지 않게)
            heads = [t for t in data["texts"] if len(t["text"]) >= 8 and not UI_CONTROL.search(t["text"])]
            is_title = lambda t: t["tag"] == "h1" or any(k in str(t.get("cls", "")).lower() for k in ("tit", "subject", "headline"))
            headings = ([t["text"] for t in heads if is_title(t)] + [t["text"] for t in heads if t["tag"] in ("h2", "h3") and not is_title(t)])[:3]
            page_ctx = adscore.page_context(normalize(" ".join([title] + headings))) if detect_posted else []
            # 본문 맥락: 글 제목이 span이라 위에서 못 잡은 예방교육·보도 글 (함께학교 도박 예방 안내의 은어 설명 그림 등)
            if detect_posted and not page_ctx and BODY_CONTEXT:
                page_ctx = adscore.body_context([normalize(t["text"]) for t in data["texts"]])
            for t in data["texts"] + head:
                checks = []
                hg = homoglyph_signals(t["raw"])
                if hg:
                    checks.append(("HOMOGLYPH", hg))
                if jamo_signal(t["raw"]):
                    checks.append(("JAMO", ["decomposed-syllables"]))
                for tech, why in checks:
                    key = (norm_url(url), prefix + t["selector"], tech, t.get("source_id"), t.get("attr", ""), t["text"], t["raw"])
                    c = cands.setdefault(key, {"url": url, "location": prefix + t["selector"], "technique": tech,
                                               "evidence_text": t["text"], "signals": []})
                    c.update(source_fields(t))
                    c["signals"].extend(why)
                if detect_posted and not checks:  # 닮은꼴·자모가 아닌 '보이는' 텍스트: 내용이 광고다울 때만
                    s, kw = adscore.score(normalize(t["text"]))
                    farm = farms.get(id(t))
                    selected_by_rules = adscore.verdict(s) != "clean" or farm or (redirect and s > 0)
                    if selected_by_rules or recover_posted:  # 후보 선정은 문장 자체 점수로
                        key = (norm_url(url), prefix + t["selector"], "POSTED", t.get("source_id"), t.get("attr", ""), t["text"], t["raw"])
                        c = cands.setdefault(key, {"url": url, "location": prefix + t["selector"], "technique": "POSTED",
                                                   "evidence_text": t["text"], "signals": [f"keyword:{','.join(kw[:6])}"]})
                        c.update(source_fields(t))
                        if not selected_by_rules:
                            c["recovered_without_rule_hit"] = True
                        context = [(f"link-farm:{farm}", LINK_FARM_BONUS)] if farm else []
                        context += redirect if s > 0 else []
                        local_ctx = page_ctx
                        if "source_near_text" in t:
                            local_ctx = adscore.page_context(normalize(t["source_near_text"]))
                            if not local_ctx and BODY_CONTEXT:
                                local_ctx = adscore.body_context([normalize(t["source_near_text"])])
                        context += local_ctx  # 제목·본문이 경고·보도·교육 글이면 감점 (게시형만 — 숨김형 광고는 그대로)
                        if context:  # 판정 때 점수에 더할 문맥 가산점 (judge 참고)
                            c["signals"].extend(n for n, _ in context)
                            c["context"] = context
    return list(cands.values())


def dedupe(cands):
    """같은 페이지에서 같은 광고를 여러 번 보고하는 것을 하나로 합친다. (남길 후보, 합친 후보 목록)

    '같은 페이지'는 page_key — 목록 몇 쪽에서 들어왔는지(startPage 등)만 다른 주소는 같은 글이다.
    불법광고 판정(ad)만 대상이며, 아래 경우 하나만 남긴다.
    1) 은닉 4종(result.json): 요강 집계 단위 (url + location + technique)가 같을 때만 (2026-10-21 변경).
       같은 요소·같은 기법이면 문구가 달라도 1건(요강: 동일 요소 내 같은 기법 문자열은 1건).
       같은 문구라도 위치가 다르면 각각 남긴다. 같은 요소의 서로 다른 은닉 기법도 각각 남긴다.
    2) 게시형(result_extra.json): 같은 페이지에서 문구가 같으면 위치가 달라도 (글 제목과 아래 목록 링크 등)
       → 은닉 기법이 게시형보다 우선, 그다음 바깥 요소·앞 형제
    3) 숨김 블록(은닉 4종) 안의 요소가 게시형으로 따로 잡힌 경우 — 숨김 블록 한 건으로 보고
    """
    def rank(c):  # 남길 우선순위: 은닉 기법 > 게시형, 바깥 요소 > 안쪽, 앞 형제 > 뒤 형제
        return (c["technique"] not in CORE_TECHNIQUES, c["location"].count(" > "), c["location"])

    ads = sorted((c for c in cands if c["verdict"] == "ad"), key=rank)
    kept, dups = [], []
    by_page = {}
    for c in ads:
        text = " ".join(c["evidence_text"].split())
        page = by_page.setdefault(page_key(c["url"]), [])
        for k in page:
            k_text = " ".join(k["evidence_text"].split())
            if {c["technique"], k["technique"]} <= CORE_TECHNIQUES:
                # 은닉 4종은 요강의 집계 단위 (url + location + technique)로만 합친다.
                # 같은 요소·같은 기법이면 문구가 달라도 1건, 위치나 기법이 다르면 같은 문구라도 각각 남긴다.
                if c["location"] == k["location"] and c["technique"] == k["technique"]:
                    c["duplicate_of"] = {"location": k["location"], "technique": k["technique"]}
                    dups.append(c)
                    break
                continue
            nested = (c["location"] == k["location"] or c["location"].startswith(k["location"] + " > ")
                      or k["location"].startswith(c["location"] + " > "))
            # 숨김 블록 안의 요소는 글자가 조금 달라도(링크 글자 제외 등) 그 블록의 일부 — 블록 한 건으로 본다
            inside_hidden = nested and k["technique"] in CORE_TECHNIQUES and c["technique"] not in CORE_TECHNIQUES
            if text == k_text or inside_hidden:
                c["duplicate_of"] = {"location": k["location"], "technique": k["technique"]}
                dups.append(c)
                break
        else:
            page.append(c)
            kept.append(c)
    dup_ids = {id(c) for c in dups}
    return [c for c in cands if id(c) not in dup_ids], dups


def _shape(location):
    """위치에서 몇 번째 요소인지(nth-of-type)를 뺀 모양 — 게시판 목록에서 글이 밀려 줄 번호가 바뀌어도 같은 자리로 본다."""
    return re.sub(r":nth-of-type\(\d+\)", "", location)


def collapse_repeats(cands):
    """여러 페이지에 반복된 같은 광고를 대표 1건으로 합친다. (남길 후보, 합친 건수)

    같은 문구·같은 기법·같은 자리(nth-of-type 제외)가 여러 페이지에 나오면 — 모든 글 아래에 붙는 게시판 목록,
    사이트 공통 하단·사이드바, 같은 글의 다른 주소(?page=…) — 실제로는 광고 하나다.
    hyojae 점검에서 숨김 문자를 넣은 스팸 제목 4개가 목록 위젯 때문에 202페이지마다 잡혀 608건이 됐다.
    채점은 '광고를 심은 글(또는 목록)' 기준일 가능성이 높아 대표 1건만 제출하고(정확도 35점 보호),
    나머지 페이지는 대표의 also_on에 남겨 화면·청정 등급(오염 페이지 수)에 반영한다.
    대표: 주소가 가장 짧은 페이지(목록 첫 페이지·글 자체 페이지가 보통 가장 짧다), 같으면 사전순.
    """
    groups = {}
    for c in cands:
        if c["verdict"] == "ad":
            groups.setdefault((" ".join(c["evidence_text"].split()), c["technique"], _shape(c["location"])), []).append(c)
    drop = set()
    for g in groups.values():
        if len({norm_url(c["url"]) for c in g}) < 2:
            continue
        g.sort(key=lambda c: (len(c["url"]), c["url"], c["location"]))
        keep = g[0]
        keep["also_on"] = [{"url": c["url"], "location": c["location"]} for c in g[1:]]
        drop.update(id(c) for c in g[1:])
    return [c for c in cands if id(c) not in drop], len(drop)


def judge(cands, model_path, use_ml, deadline=None, cancel=None):
    """룰 점수로 1차 판정, 경계 구간만 Laya로 2차 판정.

    확정 규칙은 유지하고 경계 후보만 AI 경로로 보낸다.
    """
    for c in cands:
        c["normalized"] = normalize(c["evidence_text"])
        # 항목별 점수 [근거, 가중치] — 글자 밖 단서(본문 삽입 링크 묶음 등) 가산점도 함께. hits는 이름만 모은 것
        _, detail = adscore.explain(c["normalized"], urlparse(c["url"]).netloc)  # 자기 사이트 도메인은 가산하지 않음
        c["score_detail"] = [[n, w] for n, w in detail + list(c.get("context", []))]
        c["score"] = round(sum(w for _, w in c["score_detail"]), 2)
        c["hits"] = [n if w > 0 else "-" + n for n, w in c["score_detail"]]
        c["verdict"] = adscore.verdict(c["score"])
        c["decided_by"] = "rules"
        if c["verdict"] == "borderline" and c["technique"] in CORE_TECHNIQUES and ui_widget_hidden(c):
            c["verdict"] = "clean"  # 규칙이 확정(≥4)하지 않은 UI 부품 숨김은 AI로 넘기지 않는다
            c["hits"].append("-ui-widget-hidden")
    if use_ml:
        from .ai import analyze
        analyze(cands, [], model_path, use_ml, deadline, cancel, cancel_error=ScanCancelled)
    return cands


def app_dir():
    """결과 파일 기본 위치: exe로 묶였으면 exe 폴더, 아니면 프로젝트 루트 (요강: 구동 파일 폴더에 생성)."""
    return app_root()


DEFAULT_MODEL = laya_model()


# 적격평가 대상 4종 (result.json). 그 외 추가 유형은 result_extra.json으로 분리한다.
CORE_TECHNIQUES = {"HOMOGLYPH", "JAMO", "TRANSPARENT", "OFFSCREEN"}
EXTRA_LABEL = {"POSTED": "게시형 불법광고(은닉 없음)"}


# 시간 예산(초). 적격평가 검출 시간 상한은 30분 — 넘기면 시간 점수 0점, result.json이 없으면 평가 제외될 수 있다.
# 순서: 크롤링(19분) → Laya 판정(26분 30초까지) → Gemma 검증(28분 30초까지) → 저장. 페이지 수 제한은 없다.
TIME_BUDGET = 29 * 60   # 전체 목표: 30분 상한에서 1분 여유
JUDGE_RESERVE = 2 * 60  # 크롤링 뒤 Laya 판정 몫. 실사이트 11곳 측정 시 판정은 0.4~11초
LLM_RESERVE = 8 * 60    # 크롤링 뒤 AI 판정(Laya·Gemma) 몫 — 크롤링은 time_budget - llm_reserve - JUDGE_RESERVE = 19분까지
GEMMA_SEC_PER_ITEM = 10 # Gemma 1건에 넉넉잡아 10초 → Gemma에 보내는 건수 = 남은 시간 ÷ 10초 (8분이면 약 48건)
# Laya는 Gemma 몫으로 최소 이만큼 남기고 판정 마감(28분 30초)까지 쓴다. Gemma는 남은 시간에 맞춰 건수를 줄이므로
# Laya를 21분에 고정해 자를 필요가 없다 (10-22 counselpsy: Laya가 21분에 끊겨 794건 보류, 점검은 22분 반에 끝남)
GEMMA_MIN = 2 * 60


def run(entry_url, out, concurrency=4, timeout=15000, model=DEFAULT_MODEL, use_ml=True,
        progress=None, phase=None, user_agent=DEFAULT_UA, detect_posted=True,
        time_budget=TIME_BUDGET, llm_reserve=LLM_RESERVE, collapse=False, cancel=None,
        laya_confidence_threshold=0.8, complete_validation=False, telemetry=None, residual_needs_rule_clue=True,
        gemma_sec_per_item=GEMMA_SEC_PER_ITEM):
    """크롤링 → 판정 → result.json/report.json 저장. report 딕셔너리를 돌려준다.

    phase(단계명): UI에 현재 단계(crawl/judge/save)를 알리는 콜백.
    detect_posted: 게시형(은닉 없는) 불법광고도 탐지해 result_extra.json으로 분리 저장.
    time_budget: 전체 목표 시간(초). 페이지 수 제한 없이 크롤링은 time_budget - llm_reserve - 판정 몫까지,
    Laya 판정은 time_budget - llm_reserve까지, Gemma는 time_budget - 30초까지 한다.
    complete_validation: 수집 시간 상한을 유지하고 수집된 항목의 AI 판정은 시간 상한 없이 완료.
    residual_needs_rule_clue: 규칙 후보가 아닌 잔여 원문 중 규칙 단서가 하나도 없는 문구는 Laya·Gemma에 보내지 않음(기본).
    gemma_sec_per_item: Gemma 1건 예상 시간(초). 판단 곤란 점수가 높은 것부터 남은 시간 ÷ 이 값만큼만 Gemma로 보내고
        나머지·마감 후 남은 것은 Laya 판정으로 확정. None이면 넘김 제한 없이 기존 동작(마감 후 보류).
    cancel: threading.Event. 켜지면 ScanCancelled를 일으키고 결과 파일은 건드리지 않는다(지난 제출용 result.json 보존).
    """
    from .ai_telemetry import AuditPublisher
    publisher = AuditPublisher(out, telemetry)
    model = Path(model)
    if not model.is_absolute():
        model = app_dir() / model
    phase = phase or (lambda name: None)
    started = datetime.now(KST)
    t0 = time.perf_counter()
    crawl_deadline = t0 + max(60, time_budget - llm_reserve - JUDGE_RESERVE)
    laya_deadline = None if complete_validation else t0 + max(60, time_budget - 30 - GEMMA_MIN)
    judge_deadline = None if complete_validation else t0 + max(0, time_budget - 30)
    if complete_validation:  # 시간 상한 없이 끝까지 판정하는 모드는 Gemma 넘김도 제한하지 않는다
        gemma_sec_per_item = None
    phase("crawl")
    pages, issues = asyncio.run(crawl(entry_url, concurrency, timeout, progress, user_agent, deadline=crawl_deadline, cancel=cancel))
    cancelled = lambda: cancel is not None and cancel.is_set()
    if cancelled():
        raise ScanCancelled()
    t_crawl = time.perf_counter() - t0
    phase("judge")
    from .ai import analyze
    cands = judge(candidates_from(pages, detect_posted), str(model), False)
    # 404 등 오류 응답 페이지는 대부분 공통 메뉴만 남은 빈 페이지 — 규칙으로만 판정하고 Laya·Gemma로 넘기지 않는다
    # (hanyanglaw: 404 263페이지의 '관련 사이트' 메뉴 등이 잔여 선별로 AI에 들어감). 규칙 확정 광고는 그대로 보고한다
    error_pages = {norm_url(u) for e in issues["http_errors"] for u in (e["url"], e.get("final_url") or e["url"])}
    for c in cands:
        if c["verdict"] == "borderline" and norm_url(c["url"]) in error_pages:
            c["verdict"] = "clean"
            c["hits"].append("-error-page")
    ai_pages = [(u, f) for u, f in pages if norm_url(u) not in error_pages]
    try:
        ai = analyze(cands, ai_pages, str(model), use_ml, judge_deadline, cancel,
                     laya_confidence_threshold=laya_confidence_threshold, cancel_error=ScanCancelled,
                     detect_posted=detect_posted, telemetry=publisher, laya_deadline=laya_deadline,
                     residual_needs_rule_clue=residual_needs_rule_clue, gemma_sec_per_item=gemma_sec_per_item)
        ai["telemetry"] = publisher.enrich(ai["telemetry"])
    except BaseException as exc:
        if publisher.snapshot is not None:
            publisher({"event": "progress", "snapshot": {**publisher.snapshot, "stage": "incomplete"}})
        if isinstance(exc, asyncio.CancelledError):
            # Gemma context 정리 후 UI가 처리하는 취소 예외로 전달한다.
            raise ScanCancelled() from exc
        raise
    unresolved = sum(c["verdict"] == "uncertain" for c in cands) + len(ai["unresolved_sources"])
    if cancelled():
        raise ScanCancelled()
    cands, dups = dedupe(cands)
    # 여러 페이지 반복 합치기는 기본으로 끔(팀 결정: 다른 페이지의 같은 문구는 따로 센다). --collapse-repeats로 켬
    cands, n_repeats = collapse_repeats(cands) if collapse else (cands, 0)
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
    # is_violation: 요강 예시엔 없지만 findings 필드 표에서 필수라 result.json과 같이 넣는다
    extra_findings = [{"id": f"f_{i:03d}", "url": c["url"], "is_violation": True, "location": c["location"],
                       "evidence_text": c["evidence_text"], "technique": "ETC",
                       "extra_finding": EXTRA_LABEL.get(c["technique"], c["technique"])}
                      for i, c in enumerate(extra_hits, 1)]
    # 접속 실패 정보는 내부 리포트에만 기록 (result.json은 요강 스키마 그대로 유지)
    entry_failed = any(f["url"] == entry_url for f in issues["failed"])
    frame_failures = [{"url": url, "frame_url": data["frame_url"], "reason": data["collection_error"]}
                      for url, frames in pages for _, data in frames if data.get("collection_error")]
    coverage = {"visited_pages": len(pages), "failed_frames": len(frame_failures), "pending_pages": issues["left_in_queue"],
                "failed_pages": len(issues["failed"]), "http_error_pages": len(issues["http_errors"]),
                "stopped_by_time": issues["stopped_by_time"],
                "discovered_pages": len(pages) + issues["left_in_queue"] + len(issues["failed"])}
    crawl_incomplete = bool(issues["stopped_by_time"] or issues["left_in_queue"] or issues["failed"] or issues["http_errors"] or frame_failures)
    report = {"validation_mode": "complete" if complete_validation else "bounded",
              "needs_review": bool(unresolved or crawl_incomplete or not use_ml), "crawl_incomplete": crawl_incomplete,
              "coverage": coverage, "failed_frames": frame_failures, "unresolved_count": unresolved, "ai": ai, "meta": meta, "pages": len(pages), "crawl_sec": round(t_crawl, 2), "entry_failed": entry_failed,
              "failed_pages": issues["failed"], "http_errors": issues["http_errors"],
              "download_links": issues.get("downloads", []),  # 파일 다운로드 주소(건너뜀) — 접속 실패·미확인 범위가 아님
              "stopped_by_time": issues["stopped_by_time"], "left_in_queue": issues["left_in_queue"],
              "dropped_endless": issues["dropped_endless"], "skipped_utility": issues["skipped_utility"], "time_budget": time_budget, "llm_reserve": llm_reserve,
              "visit_log": issues["visit_log"], "external_skipped": issues["external_skipped"],
              "slow_mode": issues["slow_mode"], "retried": issues["retried"],
              "extra_count": len(extra_findings), "candidates": cands,
              "model": Path(str(model)).name if use_ml else None,  # 경계 구간을 판정한 모델 폴더 이름 (규칙만이면 None)
              "repeats_collapsed": n_repeats,  # 여러 페이지 반복으로 대표에 합친 건수 (대표의 also_on에 페이지 목록)
              # 중복으로 합친 탐지 (같은 주소·내용·줄) — 어디에 합쳐졌는지(duplicate_of)만 남긴다
              "duplicates": dups}
    if cancelled():
        raise ScanCancelled()
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "result.json").write_text(json.dumps({"meta": meta, "findings": findings}, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    # result_extra.json: 추가 탐지(detect_posted)를 켜면 0건이어도 빈 findings로 생성 (요강 붙임4 0건 예시).
    # 끄면 만들지 않고 지난 실행 파일도 지운다. 적격평가 제출물(result.json)과는 별도 파일.
    extra_path = out / "result_extra.json"
    if detect_posted:
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
    ap.add_argument("--time-budget", type=int, default=TIME_BUDGET, help=f"전체 목표 시간(초, 기본 {TIME_BUDGET})")
    ap.add_argument("--llm-reserve", type=int, default=LLM_RESERVE, help=f"LLM 검증 몫으로 남길 시간(초, 기본 {LLM_RESERVE})")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--timeout", type=int, default=15000)
    ap.add_argument("--model", default=str(DEFAULT_MODEL))
    ap.add_argument("--laya-confidence-threshold", type=float, default=0.8)
    ap.add_argument("--complete-validation", action="store_true",
                    help="수집 시간 상한 유지; 수집된 항목의 AI 판정은 시간 상한 없이 완료")
    ap.add_argument("--no-ml", action="store_true", help="진단 모드: AI를 생략하고 미판정 항목은 검토 필요로 표시")
    ap.add_argument("--user-agent", default=DEFAULT_UA,
                    help="크롤러 User-Agent (기본: 일반 Chrome). 점검 기관에 신원을 밝혀야 하면 뒤에 도구명을 덧붙여 쓰세요.")
    ap.add_argument("--no-extra", action="store_true",
                    help="게시형(은닉 없는) 불법광고 탐지를 끄고 적격평가 4종만 탐지")
    ap.add_argument("--collapse-repeats", action="store_true",
                    help="여러 페이지에 반복된 같은 광고(게시판 목록·공통 하단 등)를 대표 1건으로 합쳐 제출 (기본: 페이지마다 따로)")
    args = ap.parse_args()

    report = run(args.entry_url, args.out, args.concurrency, args.timeout, args.model, not args.no_ml,
                 user_agent=args.user_agent, detect_posted=not args.no_extra,
                 time_budget=args.time_budget, llm_reserve=args.llm_reserve, collapse=args.collapse_repeats,
                 laya_confidence_threshold=args.laya_confidence_threshold, complete_validation=args.complete_validation)
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
