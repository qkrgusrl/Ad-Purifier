"""모의 공공기관 웹사이트 (탐지기 검증용).

정답(ground_truth.json)과 오탐 유도 요소(접근성 숨김 텍스트, 드롭다운 메뉴, ㅋㅋㅋ 댓글,
전각 기호, 불법도박 신고 안내 등)를 함께 심어 둔다.

실행: python testsite/server.py  ->  http://127.0.0.1:8765/
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765

CSS = """
body { font-family: sans-serif; background: #fff; color: #222; margin: 0; }
.skip-nav a { position: absolute; left: -9999px; top: 0; }
.skip-nav a:focus { left: 0; }
.sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0,0,0,0); white-space: nowrap; }
.blind { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); }
.gnb > li { display: inline-block; position: relative; margin-right: 20px; }
.gnb .sub { display: none; position: absolute; }
.gnb > li:hover .sub { display: block; }
.modal { display: none; }
.wrap { width: 960px; margin: 0 auto; }
.article-body { background: #fff; }
.white-box { background: #ffffff; padding: 8px; }
.footer { background: #333; color: #ccc; padding: 20px; }
.tab-panel.inactive { display: none; }
"""


def layout(title, body, extra_head=""):
    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><title>{title} | 가상시청</title>
<style>{CSS}</style>{extra_head}</head>
<body>
<div class="skip-nav"><a href="#content">본문 바로가기</a></div>
<header class="header">
  <h1 class="logo"><a href="/">가상시청</a></h1>
  <ul class="gnb">
    <li><a href="/board/list">자유게시판</a>
      <ul class="sub"><li><a href="/board/list">게시글 목록</a></li><li><a href="/board/list?page=2">이전 글</a></li></ul></li>
    <li><a href="/notice/view?id=1">공지사항</a>
      <ul class="sub"><li><a href="/notice/view?id=1">시정 소식</a></li><li><a href="/about">기관 소개</a></li></ul></li>
  </ul>
</header>
<main id="content" class="wrap">{body}</main>
<footer class="footer"><p class="addr">(00000) 가상시 가상구 시청로 1 <span class="sr-only">대표전화</span> 120</p>
<p class="copy">Copyright 가상시청. All rights reserved.</p></footer>
<div class="modal"><p class="msg">로그인이 필요한 서비스입니다.</p></div>
</body></html>"""


def page_index():
    return layout("메인", """
<section class="main-visual"><h2>시민과 함께하는 가상시</h2>
<p class="desc">2026 시민 참여 예산 공모가 진행 중입니다. <a href="/notice/view?id=1">자세히 보기<span class="sr-only">새창 열림</span></a></p></section>
<section class="latest"><h3>최신 게시글</h3>
<ul class="latest-list">
  <li><a href="/board/view?id=1001">도서관 운영시간 변경 문의</a></li>
  <li><a href="/board/view?id=1002#comments">주차장 이용 관련 건의</a></li>
  <li><a href="/board/view?id=1003">민원 처리 결과 공유</a></li>
</ul></section>
<div class="tab-wrap"><div class="tab-panel active">공지 탭 내용</div><div class="tab-panel inactive">보도자료 탭 내용입니다.</div></div>
<nav class="quick-nav">
  <ul>
    <li data-href="/data-nav" style="cursor:pointer">시설 예약</li>
    <li><button onclick="location.href='/onclick-nav'" type="button">행사 일정</button></li>
  </ul>
</nav>
<script>var quickLinks = ['/script-nav'];</script>
""")


def page_board_list(page):
    if page == "2":
        rows = [("1005", "봄맞이 하천 정화 활동 후기"), ("1006", "공원 산책로 조명 개선 요청")]
        extra = ""
    else:
        rows = [("1001", "도서관 운영시간 변경 문의"), ("1002", "주차장 이용 관련 건의"),
                ("1003", "민원 처리 결과 공유"), ("1004", "２０２６년 예산 공개（안내） Ｑ＆Ａ")]
        extra = ""
    trs = ""
    for i, (pid, title) in enumerate(rows, 1):
        trs += f'<tr><td class="num">{pid}</td><td class="title"><a href="/board/view?id={pid}">{title}</a></td><td class="writer">시민{i}</td></tr>\n'
    if page != "2":
        # 정답 HOMOGLYPH: 전각 문자로 위장한 도박 광고 제목 (5번째 행)
        trs += '<tr><td class="num">1007</td><td class="title"><a href="/board/view?id=1007">ｍｅｇａ－ＢＥＴ 첫충 30％ 지급</a></td><td class="writer">홍보</td></tr>\n'
    pager = '<div class="pager"><a href="/board/list">1</a> <a href="/board/list?page=2">2</a></div>'
    return layout("자유게시판", f"""
<h2>자유게시판</h2>
<div class="board-list"><table><caption class="blind">자유게시판 목록 - 번호, 제목, 작성자</caption>
<thead><tr><th>번호</th><th>제목</th><th>작성자</th></tr></thead>
<tbody>{trs}</tbody></table></div>{pager}{extra}""")


COMMENT_SCRIPT = """
<script>
fetch('/api/comments?post=%s').then(r => r.json()).then(list => {
  const box = document.querySelector('div.comment-list');
  for (const c of list) {
    const d = document.createElement('div'); d.className = 'comment';
    d.innerHTML = '<span class="author">' + c.author + '</span><p class="comment-text"' + (c.style ? ' style="' + c.style + '"' : '') + '>' + c.text + '</p>';
    box.appendChild(d);
  }
});
</script>"""


COMMENTS = {
    "1001": [
        {"author": "시민A", "text": "운영시간 연장 좋네요 ㅋㅋㅋ 감사합니다 ㅎㅎ"},
        # 정답 JAMO (댓글 2번째)
        {"author": "익명", "text": "ㅋㅏㅈㅣㄴㅗ ㅂㅗㄴㅓㅅㅡ 코드 LUCKY7"},
        {"author": "시민B", "text": "주말에도 여나요? ㅠㅠ"},
    ],
    "1002": [
        {"author": "시민C", "text": "저도 같은 생각입니다."},
        # 정답 TRANSPARENT + JAMO (같은 요소, 기법 2개)
        {"author": "guest", "text": "ㅂㅏㅋㅏㄹㅏ ㅅㅏㅇㅣㅌㅡ ㅈㅜㅅㅗ", "style": "color: transparent"},
    ],
    "1005": [
        {"author": "시민D", "text": "참여하고 싶어요! 다음 일정 공유 부탁드립니다."},
    ],
}


def page_board_view(pid):
    body = {
        "1001": """
<div class="article-body">
  <h3 class="subject">도서관 운영시간 변경 문의</h3>
  <p class="desc">중앙도서관 평일 운영시간이 22시까지 연장되었다고 들었습니다. 사실인가요?
    <span style="color: transparent">먹튀검증 안전놀이터 상담 텔레그램 @promo_777</span></p>
  <iframe src="/widget/notice" title="알림 위젯" width="300" height="80"></iframe>
</div>""",
        "1002": """
<div class="article-body">
  <h3 class="subject">주차장 이용 관련 건의</h3>
  <div class="white-box"><p>공영주차장 야간 요금 조정을 건의합니다.</p>
    <p class="note" style="color: #ffffff">바카라 실시간 입금보너스 카톡 vip8282</p></div>
  <p class="desc">주말 만차 문제도 함께 검토 부탁드립니다. <span class="tiny" style="font-size: 0px">성인 무료 영상 사이트 주소 hot19.xyz</span></p>
  <p class="ref">관련 링크: <a class="ext" href="#">саsіnо 가입코드 777</a></p>
  <div class="promo" style="opacity: 0; position: absolute; left: -9999px">토토사이트 추천 꽁머니 지급</div>
</div>""",
        "1003": """
<div class="article-body">
  <h3 class="subject">민원 처리 결과 공유</h3>
  <p>지난달 접수한 민원이 처리되었습니다. <a href="/notice/view?id=1">처리 결과 보기<span class="sr-only">새창 열림</span></a></p>
  <p class="report"><a href="/notice/view?id=1"><span class="blind">불법 도박 신고센터 바로가기</span>신고센터</a></p>
  <table class="info"><caption class="blind">처리 현황 표</caption><tr><td>접수</td><td>완료</td></tr></table>
  <div class="ad-slot" style="display: none">무직자 당일 대출 승인 신용불량 OK 010-1234-5678</div>
  <div class="accordion" style="display: none">자주 묻는 질문: 민원 처리 기간은 7일입니다.</div>
  <div class="collapse" style="height:0;overflow:hidden">당일대출 신용불량OK 즉시승인 카톡 loan_ok</div>
</div>""",
        "1004": """
<div class="article-body">
  <h3 class="subject">２０２６년 예산 공개（안내） Ｑ＆Ａ</h3>
  <div class="content">
    <p>예산 공개 일정을 안내합니다. 증가율 ５％, 문의는 № 3 창구로.</p>
    <p>세부 항목은 첨부파일을 참고하세요.</p>
    <p>ㅌㅗㅌㅗ ㅅㅏㅇㅣㅌㅡ 신규가입 ㅊㅓㅅㅊㅜㅇ 20%</p>
  </div>
  <p class="top" style="color: #fff; background: #fff">맨 위로</p>
  <style>.pseudo-promo::before{content:"바카라 생방송 텔레 @bac_live";color:rgba(255,255,255,0.01)}</style>
  <div class="pseudo-promo"></div>
</div>""",
        "1005": """
<div class="article-body">
  <h3 class="subject">봄맞이 하천 정화 활동 후기</h3>
  <p class="desc">시민 50여 명이 참여해 쓰레기 2톤을 수거했습니다.</p>
  <iframe src="/widget/outer" title="행사 위젯" width="400" height="200"></iframe>
</div>""",
        "1006": """
<div class="article-body">
  <h3 class="subject">공원 산책로 조명 개선 요청</h3>
  <p class="desc">야간에 산책로가 어둡습니다. 무료 체험 프로그램 신청 바로가기는 공지를 확인하세요.</p>
  <p class="hidden-msg" style="font-size: 1px">ｓｌｏｔ 무료스핀 100회 텔레 @spin_kr</p>
  <div class="scaled-ad" style="transform:scale(0)">필로폰 구매대행 텔레 @drug_kr</div>
</div>""",
        "1007": """
<div class="article-body"><h3 class="subject">삭제된 게시글입니다.</h3></div>""",
    }.get(pid)
    if body is None:
        return None
    comments = f'<div id="comments" class="comment-area"><h4>댓글</h4><div class="comment-list"></div></div>{COMMENT_SCRIPT % pid}'
    return layout("게시글", body + comments)


def page_data_nav():
    return layout("시설 예약", """
<div class="article-body">
  <h2>시설 예약 안내</h2>
  <p>온라인 예약은 로그인 후 이용 가능합니다.</p>
  <div class="data-nav-hidden" style="display:none">토토사이트 가입코드 FREE77 꽁머니 지급</div>
</div>""")


def page_onclick_nav():
    return layout("행사 일정", """
<div class="article-body">
  <h2>행사 일정 안내</h2>
  <p>이번 달 주요 행사 일정을 안내합니다.</p>
  <div class="onclick-nav-hidden" style="display:none">바카라 카지노 입금보너스 첫충 100% 카톡 abc123</div>
</div>""")


def page_script_nav():
    return layout("알림 서비스", """
<div class="article-body">
  <h2>알림 서비스</h2>
  <p>중요 공지를 문자로 받아보세요.</p>
  <div class="script-nav-hidden" style="display:none">슬롯 무료스핀 100회 홀덤 포커 텔레 @spin_vip</div>
</div>""")


def page_hidden_ad():
    return layout("숨겨진 광고 페이지", """
<div class="article-body">
  <h2>페이지</h2>
  <div class="sitemap-hidden" style="display:none">성인영상 야동 무료 다운로드 사이트 텔레 @adult_stream</div>
</div>""")


def page_notice():
    return layout("공지사항", """
<div class="notice-board">
  <h2>불법 도박 근절 캠페인 안내</h2>
  <p>온라인 카지노, 불법 토토 등 불법 도박 광고를 발견하시면 신고해 주세요. 신고 전화 1336.</p>
  <span class="visually-hidden" style="position:absolute; left:-10000px">불법 도박 예방 캠페인 영상 설명: 도박 중독 상담은 1336</span>
</div>""")


def page_about():
    return layout("기관 소개", """
<div class="about"><h2>기관 소개</h2><p>가상시청은 시민 중심 행정을 추구합니다.</p>
<p class="hidden-version" style="visibility: hidden">v2.3.1 build 20260901</p></div>""")


WIDGETS = {
    "/widget/notice": """<!doctype html><html><head><meta charset="utf-8">
<style>.wrap{padding:4px}.hidden-link{position:absolute;left:-9999px}</style></head>
<body><div class="wrap"><p class="msg">이번 주 도서관 휴관일: 월요일</p>
<span class="hidden-link"><a href="#">무료 슬롯 체험 바로가기</a></span></div></body></html>""",
    "/widget/outer": """<!doctype html><html><head><meta charset="utf-8"></head>
<body><div class="event"><p>행사 사진 갤러리</p><iframe src="/widget/inner" width="300" height="100"></iframe></div></body></html>""",
    "/widget/inner": """<!doctype html><html><head><meta charset="utf-8"></head>
<body><div class="gallery"><p class="caption">하천 정화 활동 사진</p>
<div class="hide" style="display:none">홀덤 포커 24시 환전 텔레 @hold_em</div></div></body></html>""",
}


def sitemap_xml(base):
    urls = [
        "", "/board/list", "/board/list?page=2",
        "/board/view?id=1001", "/board/view?id=1002", "/board/view?id=1003",
        "/board/view?id=1004", "/board/view?id=1005", "/board/view?id=1006",
        "/notice/view?id=1", "/about", "/hidden-ad",
    ]
    locs = "\n".join(f"  <url><loc>{base}{u}</loc></url>" for u in urls)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{locs}
</urlset>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, status, body, ctype="text/html; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        p = u.path.rstrip("/") or "/"
        html = None
        if p == "/":
            html = page_index()
        elif p == "/board/list":
            html = page_board_list(q.get("page", "1"))
        elif p == "/board/view":
            html = page_board_view(q.get("id", ""))
        elif p == "/notice/view":
            html = page_notice()
        elif p == "/about":
            html = page_about()
        elif p == "/data-nav":
            html = page_data_nav()
        elif p == "/onclick-nav":
            html = page_onclick_nav()
        elif p == "/script-nav":
            html = page_script_nav()
        elif p == "/hidden-ad":
            html = page_hidden_ad()
        elif p == "/sitemap.xml":
            base = f"http://127.0.0.1:{PORT}"
            return self._send(200, sitemap_xml(base), "application/xml; charset=utf-8")
        elif p in WIDGETS:
            html = WIDGETS[p]
        elif p == "/api/comments":
            return self._send(200, json.dumps(COMMENTS.get(q.get("post", ""), []), ensure_ascii=False),
                              "application/json; charset=utf-8")
        if html is None:
            return self._send(404, "<h1>404</h1>")
        self._send(200, html)


if __name__ == "__main__":
    print(f"test site: http://127.0.0.1:{PORT}/")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
