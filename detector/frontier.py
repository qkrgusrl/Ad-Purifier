"""크롤링 대기열(frontier): 어떤 페이지를 먼저 볼지 정한다.

순서: ① 진입 페이지 주변(메뉴 지도) → ② 누구나 글을 쓰는 게시판·목록 페이지 → ③ 목록에서 제목이 수상한 글
→ ④ 나머지 글·일반 페이지. 우선순위만 낮출 뿐 버리지 않는다 — 숨김 광고(적격평가 4종)는 제목이 멀쩡한 글
본문에도 있을 수 있으므로 시간이 남는 대로 전부 본다.

예외로 버리는 것은 검색·정렬·달력처럼 같은 형태의 주소가 끝없이 생기는 경우뿐이다(형태별 상한).
게시판 하나가 시간을 독점하지 않도록 같은 게시판의 주소는 많이 쌓일수록 조금씩 뒤로 민다(게시판 간 번갈아 방문).

목록 페이지는 한 번에 글 제목 20여 개를 보여 주므로 글 상세보다 먼저 본다. 특히 수상한 제목이 여러 개 걸린
목록(스팸이 쌓인 게시판)에서 나온 다음 쪽 목록은 크게 앞당긴다 — 실사이트 비교에서 이전(BFS) 방식이 이기던 이유.
"""
import functools
import heapq
import itertools
import math
import re
from urllib.parse import parse_qsl, unquote, urlparse

from . import adscore
from .textnorm import normalize

# 게시판 주소 모양 (그누보드·XE·카페24·전자정부 표준프레임워크·자체 CMS에서 흔한 형태)
BOARD_URL = re.compile(
    r"bbs|board|article|xboard|bo_table=|wr_id=|board_no=|board_code=|nttid=|bbsid=|selectboard|"
    r"/notice|/qna|/faq|/free|/review|/gallery|mode=(?:list|view)|[ab]mode=|read\.htm|"
    r"(?:list|view)\.(?:do|htm|html|php|jsp|asp)", re.I)
# 글 하나(상세 페이지) 주소 모양
VIEW_URL = re.compile(
    r"wr_id=|[?&](?:idx|seq|no|number|nttid|articleno|document_srl|board_seq)=\d|mode=view|[ab]mode=view|"
    r"read\.htm|view\.(?:do|htm|html|php|jsp|asp)|/article/[^/]+/\d+/\d+", re.I)
# 누구나 글을 쓸 수 있는 게시판 (불법광고가 실제로 올라오는 곳) — 주소나 링크 글자로 판단
WRITABLE = re.compile(
    r"qna|q&a|free|inquiry|review|guest|community|자유|질문|문의|묻고|상담|후기|방명록|참여|제안|칭찬|"
    r"건의|커뮤니티|게시판|민원|소통|토론|사진", re.I)
# 값이 있으면 주소가 끝없이 늘어나는 매개변수 (검색어·정렬·달력)
ENDLESS_PARAMS = {"q", "query", "keyword", "keywords", "search", "searchword", "searchkeyword", "searchwrd", "stx", "sfl",
                  "sop", "sca", "sst", "sod", "sort", "order", "orderby", "date", "year", "month", "day", "cal", "ym",
                  "searchtype", "searchcnd", "keyset", "tag"}
ENDLESS_CAP = 20  # 검색·정렬·달력 주소는 같은 형태당 이만큼만
# 글 상세 주소에서 '어느 목록 페이지에서 왔는지'만 나타내는 매개변수 — 내용은 같은 글이므로 중복 판별 때 무시
# (p는 워드프레스 글 번호(?p=123)라 넣지 않음)
LIST_POS_PARAMS = {"page", "cpage", "pg", "pageindex", "pageno", "page_no", "start", "offset"}
# 광고가 있을 수 없는 기능 페이지 (비밀번호 입력·글쓰기·로그인·다운로드 등) — 방문하지 않음.
# 카페24 글쓰기(write.html)는 봇 확인 페이지로 넘어가 시간만 쓴다(실사이트에서 방문의 40%).
UTILITY_URL = re.compile(
    r"(?:password|write|modify|delete|download)\.(?:php|html?|do|jsp|aspx?)|/download|login|logout|/join|register|signup|"
    r"/member/(?:login|join|agreement|id|pw|find)|print\.(?:php|html)|/rss|[?&](?:w|mode)=(?:write|reply|modify|u)\b|"
    r"/challenge\?auth|/cart|/order|/myshop|reply\.html|board_act=|/exec/front/", re.I)
# 게시판을 구분하는 매개변수 (같은 게시판인지 묶을 때)
BOARD_PARAMS = ("bo_table", "bbsid", "bbs_id", "tbnum", "board_code", "board_no", "board", "code", "mid", "menu_id")

ENTRY_ZONE = 40      # 진입 페이지에서 1단계: 메뉴 지도를 먼저 그린다
BOARD_BONUS = 30
WRITABLE_BONUS = 20
LIST_BONUS = 10      # 목록 페이지 = 글 제목 미리보기
HOT_LIST_BONUS = 40  # 수상한 제목이 HOT_MIN개 이상 걸린 목록에서 나온 다른 쪽 목록 (실사이트 11곳 비교에서 25보다 나음)
HOT_MIN = 3
SUSPICIOUS_BONUS = 30  # 제목이 수상한 글 (목록에서 이미 제목은 잡았으므로 다음 쪽 목록보다는 뒤)
ENDLESS_PENALTY = 60
GROUP_PENALTY = 8    # 같은 게시판·같은 종류(목록/글) 주소가 쌓일수록 log2 비례로 뒤로


def _params(url):
    return [(k.lower(), v) for k, v in parse_qsl(urlparse(url).query, keep_blank_values=True)]


def endless_shape(url):
    """검색·정렬·달력 값이 들어간 주소면 그 '형태', 아니면 None. 빈 값(searchword=)은 무시.

    형태 = 경로 + 나머지 매개변수 값 + 검색류 매개변수 이름. 검색어만 바뀌는 주소는 같은 형태로 묶이지만,
    글 번호(idx 등)가 다르면 다른 형태라 상한에 걸리지 않는다 — 검색 상태(q=…)를 모든 링크에 붙이는 사이트 대응.
    """
    ps = _params(url)
    names = sorted(k for k, v in ps if k in ENDLESS_PARAMS and v.strip())
    if not names:
        return None
    rest = sorted((k, v) for k, v in ps if k not in ENDLESS_PARAMS and k not in LIST_POS_PARAMS)
    return urlparse(url).path + "?" + "&".join(f"{k}={v}" for k, v in rest) + "|" + "&".join(names)


# 카페24 게시판 목록: /board/<이름>/<번호>/?page=N 과 /board/<영문>/list.html?board_no=<번호>&page=N 이 같은 목록
CAFE24_LIST = re.compile(r"^/board/[^/]+/(\d+)/?$|^/board/[^/]+/list\d*\.html$")


def content_key(url, key):
    """중복 판별 키. 글 상세 주소는 목록 위치·검색 상태 매개변수와 카페24의 /page/N/ 꼬리를 빼서,
    같은 글을 다른 주소로 여러 번 보지 않게 한다. 카페24 목록은 두 가지 주소 모양을 (게시판 번호, 쪽)으로 합친다.
    그 밖에는 원래 키(norm_url) 그대로."""
    u = unquote(url)
    if not VIEW_URL.search(u):
        p = urlparse(url)
        m = CAFE24_LIST.match(unquote(p.path))
        q = dict(_params(url))
        board_no = q.get("board_no") or (m.group(1) if m else None)
        if m and board_no and not (set(q) - {"board_no", "page", "category"}):
            # list2.html(갤러리형 보기)도 같은 글 목록이라 같은 키
            return f"{p.netloc.lower()}|cafe24-list|{board_no}|{q.get('category', '')}|{q.get('page') or '1'}"
        return key
    p = urlparse(url)
    path = re.sub(r"/page/\d+/?$", "/", p.path)
    rest = sorted((k, v) for k, v in _params(url) if k not in LIST_POS_PARAMS and k not in ENDLESS_PARAMS)
    return p.netloc.lower() + path.rstrip("/") + "?" + "&".join(f"{k}={v}" for k, v in rest)


def is_view(url):
    return bool(VIEW_URL.search(unquote(url)))


def board_group(url):
    """같은 게시판 묶음 키: 숫자를 지운 경로 + 게시판 구분 매개변수 값 + 종류(목록/글).
    목록과 글을 따로 묶어, 글 주소가 많이 쌓여도 그 게시판의 다음 쪽 목록이 함께 밀리지 않게 한다."""
    p = urlparse(url)
    m = CAFE24_LIST.match(unquote(p.path))
    if m:  # 카페24 목록의 두 가지 주소 모양을 한 게시판으로
        return "cafe24-list|" + (dict(_params(url)).get("board_no") or m.group(1) or "") + "|list"
    path = re.sub(r"\d+", "N", unquote(p.path))
    if "/article/" in path:  # 카페24: /article/<게시판명>/<번호>/<글번호>/ → 게시판명까지
        path = path.split("/article/")[0] + "/article/" + path.split("/article/")[1].split("/")[0]
    q = dict(_params(url))
    kind = "view" if is_view(url) else "list"
    return path + "|" + "|".join(f"{k}={q[k]}" for k in BOARD_PARAMS if q.get(k)) + "|" + kind


@functools.lru_cache(maxsize=20000)  # 같은 메뉴·제목 글자가 페이지마다 반복된다
def suspicious(text):
    """링크 글자(주로 게시글 제목)가 룰 점수상 정상 확정을 넘으면 True."""
    if not text or len(text) < 2:
        return False
    s, _ = adscore.score(normalize(text))
    return adscore.verdict(s) != "clean"


class Frontier:
    def __init__(self):
        self._heap = []
        self._seq = itertools.count()  # 같은 점수면 먼저 발견한 순서 (BFS처럼)
        self.seen = set()
        self.group_count = {}
        self.endless_count = {}
        self.dropped_endless = 0
        self.skipped_utility = 0

    def __len__(self):
        return len(self._heap)

    def priority(self, url, text, depth, hot=False):
        u = unquote(url)
        s = 0.0
        if depth <= 1:
            s += ENTRY_ZONE
        board = bool(BOARD_URL.search(u))
        if board:
            s += BOARD_BONUS
            if not VIEW_URL.search(u):
                s += LIST_BONUS + (HOT_LIST_BONUS if hot else 0)
        if WRITABLE.search(u) or (text and WRITABLE.search(text)):
            s += WRITABLE_BONUS
        if suspicious(text):
            s += SUSPICIOUS_BONUS
        if endless_shape(url):
            s -= ENDLESS_PENALTY
        if board:
            g = board_group(url)
            n = self.group_count.get(g, 0)
            self.group_count[g] = n + 1
            s -= GROUP_PENALTY * math.log2(1 + n)
        return s - depth  # 같은 조건이면 얕은 페이지 먼저

    def add_links(self, links, depth):
        """한 페이지에서 나온 링크 [(url, key, text)]를 넣는다. 수상한 제목이 여러 개 걸린 페이지(스팸이 쌓인 목록)면
        그 페이지에서 나온 목록 링크(다음 쪽 등)를 앞당긴다."""
        hot = sum(suspicious(t) for _, _, t in links) >= HOT_MIN
        for url, key, text in links:
            self.add(url, key, text, depth, hot)

    def add(self, url, key, text="", depth=0, hot=False):
        """새 주소면 대기열에 넣고 True. key = 중복 판별용 정규화 주소."""
        if depth > 0 and UTILITY_URL.search(unquote(url)):
            self.skipped_utility += 1
            return False
        key = content_key(url, key)
        if key in self.seen:
            return False
        shape = endless_shape(url)
        if shape:
            n = self.endless_count.get(shape, 0)
            if n >= ENDLESS_CAP:
                self.dropped_endless += 1
                return False
            self.endless_count[shape] = n + 1
        self.seen.add(key)
        heapq.heappush(self._heap, (-self.priority(url, text, depth, hot), next(self._seq), url, depth))
        return True

    def pop(self):
        """가장 우선순위가 높은 (url, depth), 비었으면 None."""
        if not self._heap:
            return None
        _, _, url, depth = heapq.heappop(self._heap)
        return url, depth
