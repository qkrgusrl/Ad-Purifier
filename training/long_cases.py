"""학습 데이터 v3: 긴 글·연락처 줄·링크 앵커·숫자 위장 유형 (2026-10-16).

팀원이 조사한 공공기관 URL 28개(A 정상 10 / B 경계 3 / C 도박 12 / D 성인 3)에서 확인한 약점을 메운다.
- 정상: 도박·마약·성인 예방교육·통계·보도, 은어 설명, 직업훈련·관광 채용, 소비자 피해 안내, 기사 속 인용, 연락처 줄
- 광고: 긴 SEO 스팸, 기호 도배, 현금게임, 폰팅·채팅(숫자 위장), 짧은 링크 앵커, 정상 문단 뒤에 붙인 스팸
그 URL의 문구 자체는 학습에 넣지 않는다(training/real_cases.jsonl은 평가 전용). 비슷한 '유형'만 어휘·문형을 바꿔 만들고,
make_dataset.py가 생성 후 8글자 이상 겹치는 문장이 있는지 검사한다.
어휘·문형 목록은 make_dataset.split_pools가 20%를 dev 전용으로 떼어 둔다 (외운 표현이 아닌 일반화를 보려고).
"""
import re

# ---------------------------------------------------------------- 공통 어휘 (분할하지 않음)
SUBJ = ["중·고등학생", "청소년", "대학생", "20대 청년", "군 장병", "직장인", "초등학생 학부모"]
REGION = ["도내 9개 시군", "전국 17개 시도", "수도권", "부산·경남 지역", "강원권", "충청권", "호남권"]
AGENCY = ["교육청", "경찰청", "도박문제 상담센터", "통계청", "청소년상담복지센터", "여성가족부", "권익위원회", "보건복지부"]
NEG_IDS = ["kim.minho", "staff", "info", "contact", "pr", "ha.jina", "manager", "jb.lee", "edu", "support"]
NEG_MAIL_HOST = ["korea.kr", "hanmail.net", "gmail.com", "naver.com", "daum.net", "incruit.com", "go.kr"]
SPAM_TLD = ["com", "net", "xyz", "vip", "club", "top"]
SYMBOLS = [("◀", "▶"), ("☞", "☜"), ("★", "★"), ("【", "】"), ("《", "》"), ("♠", "♠")]

# ---------------------------------------------------------------- 분할되는 풀 (어휘 + 문형)
GAME = ["바카라", "스포츠 토토", "홀덤", "슬롯", "룰렛", "블랙잭", "파워볼", "사다리 게임", "경마", "온라인 카지노", "다이사이", "식보"]
JOBS = ["마사지 치료사", "안마사", "스포츠 마사지사", "이료 전문가", "물리치료사", "피부관리사", "카지노 딜러",
        "관광통역안내사", "바리스타", "호텔 지배인", "네일아티스트", "요양보호사"]
SVC = ["마사지", "피부관리", "헬스장 회원권", "결혼정보 서비스", "온라인 강의", "여행 패키지", "에스테틱"]
QUOTES = ["야동 한 번이라도 안 본 사람 있냐", "몰카 영상 공유방 아직도 있냐", "도박 한 판만 해 보자", "토토 같이 할 사람 구함",
          "불법 사이트 주소 알려줘", "그 영상 어디서 봤냐", "대리 입금 좀 해 줘"]
BRANDS = ["우리", "에이스", "퀸", "로얄", "제이", "메가", "킹덤", "프리미엄", "엘리트", "오공", "마블", "골드"]
CATS = ["카지노사이트", "슬롯사이트", "홀덤사이트", "바카라사이트", "토토사이트", "온라인카지노", "안전놀이터", "스포츠배팅"]
SYMBOL_KW = ["드래곤타이거", "바카라", "룰렛", "다이사이", "홀덤", "슬롯", "온라인카지노", "생방송카지노", "라이브카지노"]
CASHW = ["포커", "맞고", "섯다", "슬롯", "바둑이", "고스톱", "하이로우"]
ANCHOR_POS = ["출장마사지", "출장안마", "출장홈케어", "홈타이", "출장홈타이", "출장 타이마사지", "스웨디시 출장", "출장샵 후기",
              "콜걸 대기중", "안마방 후기", "오피 후기 모음", "유흥 정보", "룸살롱 예약", "호빠 추천", "출장 마사지 20대",
              "출장 마사지 후불", "립카페 정보", "건마 후기"]
TERMS = [("총판", "도박 사이트를 대신 홍보하고 이용자를 모집하는 사람"),
         ("환치기", "현금을 불법으로 외국 돈으로 바꿔 주고 수수료를 받는 행위"),
         ("먹튀", "도박 사이트가 당첨금을 주지 않고 잠적하는 일"),
         ("대포통장", "다른 사람 명의로 개설해 범죄에 쓰는 계좌"),
         ("작업대출", "소득 서류를 꾸며 대출을 받게 해 주는 불법 행위"),
         ("떨", "대마를 가리키는 은어"),
         ("던지기", "마약을 약속한 장소에 두고 가는 거래 방식"),
         ("픽스터", "경기 결과를 예측해 판매하는 사람"),
         ("롤링", "베팅 금액에 비례해 돌려주는 포인트 제도"),
         ("충환전", "도박 사이트에서 돈을 넣고 빼는 일"),
         ("조건만남", "성관계를 대가로 금품을 주고받는 만남"),
         ("스미싱", "문자 속 링크로 악성 앱을 설치하게 하는 사기"),
         ("보이스피싱", "전화로 공공기관을 사칭해 돈을 가로채는 사기"),
         ("딥페이크", "사진이나 영상을 합성해 다른 사람인 것처럼 꾸민 콘텐츠")]

STAT_TPL = [
    "{agency} 조사 결과 {region} {subj} {pop}명 중 {cnt}명이 {game}, {game2} 같은 온라인 도박을 경험한 것으로 집계됐다. 처음 접한 평균 연령은 {age}세였고, 이들 가운데 {pc}는 지인의 권유로 시작했다고 답했다.",
    "{yr}년 {agency}이 실시한 실태조사에서 {subj}의 {pc}가 불법 {game} 사이트에 접속한 적이 있다고 응답했다. 이에 따라 {region} 학교를 대상으로 예방교육을 확대하고 상담 창구를 운영한다.",
    "최근 {yrs}년간 {region}에서 적발된 불법 {game} 사이트 운영 사건은 모두 {cnt}건으로, 피해 금액은 {amt}만원에 이른다. {agency}은 단속과 함께 피해자 상담·치료 연계를 강화하겠다고 밝혔다.",
    "{subj}의 도박 문제를 줄이려면 학교와 가정, 지역사회가 함께 대응해야 한다는 지적이 나온다. {agency}은 {game}과 {game2}처럼 접근이 쉬운 온라인 게임형 도박의 위험성을 알리는 교육 자료를 배포했다.",
    "{agency}에 따르면 {yr}년 도박 문제 상담 건수는 전년 대비 {pc} 늘었다. 상담자의 상당수가 {subj}이었으며, 대부분 스마트폰으로 {game}·{game2} 등에 접속한 것으로 파악됐다.",
    "경찰은 {game} 사이트를 만들어 {subj}에게 접근한 일당 {yrs}명을 검거하고 범죄수익 {amt}만원을 환수했다고 밝혔다. 경찰은 비슷한 광고 문자나 게시글을 발견하면 즉시 신고해 달라고 당부했다.",
    "도박 중독은 의지의 문제가 아니라 치료가 필요한 질환이다. 초기에는 도박 시간이 길어지고 돈을 잃은 뒤 더 큰 금액을 걸어 만회하려는 행동이 나타난다. 이런 경우 {agency}에 상담을 신청하면 된다.",
    "법무부는 사행성 게임물 운영자에 대한 처벌을 강화하는 개정안을 입법예고했다. 개정안에는 {game} 등 불법 도박 누리집의 광고를 게시한 자에 대한 과태료 규정도 포함됐다.",
    "{region} 학교 {cnt}곳에서 {yr}년 하반기 도박 예방교육을 실시한다. 교육은 {game}·{game2} 같은 불법 도박의 위험성과 신고 방법, 상담기관 이용 안내로 구성되며 학부모 대상 강연도 함께 열린다.",
    "마약류 범죄가 {subj}으로 확산되면서 {agency}은 SNS를 통한 은밀한 거래를 집중 단속하고 있다. 의심스러운 게시글을 발견하면 누리집이나 전화로 제보해 달라고 안내했다.",
]
DEF_TPL = ["{t} : {d}", "{t}은(는) {d}을(를) 가리키는 말이다.", "'{t}' - {d}을(를) 뜻하는 은어", "{t}이란 {d}을(를) 말한다."]
JOB_TPL = [
    "{job} 직업은 꾸준한 수요가 있어 취업 전망이 밝은 편이다. 관련 자격증을 취득하면 병원, 복지시설, 센터 등에서 일할 수 있고, 교육 과정은 {n}개월 동안 이론과 실습으로 진행된다.",
    "{org} 직업훈련과정 안내: {job} 양성 과정 수강생을 모집합니다. 교육 기간은 {n}개월이며 수료 후 취업 연계를 지원합니다. 문의는 {org} 교육팀으로 해 주세요.",
    "시각장애인 {job} 자격 취득을 위한 교육과정을 운영합니다. 실기와 이론 수업, 현장 실습, 자격시험 대비반으로 구성되어 있으며 수강료는 전액 지원됩니다.",
    "{job}의 하루는 고객 상담과 준비 작업으로 시작한다. 근무 환경과 급여 수준, 자격 요건과 취업 경로를 정리했으니 진로를 고민하는 학생들은 참고하기 바란다.",
    "{region} 관광 일자리 페스타에 {yrs}0개 기업이 참여해 {job}, 호텔 객실, 면세점 판매직 등 {cnt}명을 채용한다. 참가를 원하는 구직자는 사전 등록 후 현장에서 면접을 볼 수 있다.",
    "{job} 교육과정에서는 {tech}, 지압, 림프 마사지 등 여러 기법을 이론과 실습으로 배우며, 수료 후 자격 시험에 응시할 수 있습니다.",
    "{tech} 실습 수업에서는 압력 조절과 자세 교정을 배우고, 수강생은 실기 평가를 통과해야 수료한다. 현장 실습은 {org}과 협약한 시설에서 진행된다.",
    "{job} 자격 취득을 준비하는 분들을 위한 특강: {tech}의 원리와 주의사항, 고객 응대 방법을 다룹니다. 참가 신청은 {org} 누리집에서 받습니다.",
    "{job} 국가자격 취득 과정은 필기와 실기로 나뉜다. 응시 자격과 시험 일정, 합격 기준을 안내하니 응시를 준비하는 분들은 {org} 누리집에서 공고문을 확인하시기 바랍니다.",
]
CONS_TPL = [
    "최근 '무료 {svc}' 이벤트로 소비자를 유인한 뒤 고가의 회원권 결제를 유도하는 피해가 늘고 있다. 계약 전 총액과 환불 조건을 서면으로 확인하고, 사업자가 압박하면 소비자원에 상담을 요청하세요.",
    "{svc} 이용 중 중도 해지가 어렵다는 상담이 {cnt}건 접수됐다. 소비자원은 계약서에 환불 규정이 있는지 확인하고 카드 할부 결제는 신중히 하라고 당부했다.",
    "{svc} 관련 소비자 피해 예방 요령: 광고만 보고 결제하지 말고, 사업자 등록 여부와 이용 약관을 확인하며, 분쟁이 생기면 증빙 자료를 모아 1372 소비자상담센터에 문의하세요.",
]
QUOTE_TPL = [
    "상담 현장에서는 학생들이 서로에게 \"{quote}\"라고 묻는 모습을 쉽게 볼 수 있다. 전문가들은 이런 질문이 또래 압박으로 작용할 수 있다며 올바른 교육이 필요하다고 말했다.",
    "설문에서 한 학생은 \"{quote}\"라는 말을 친구들끼리 장난처럼 주고받는다고 답했다. 연구진은 {subj}의 유해 콘텐츠 노출 실태를 점검하고 대응 방안을 마련했다.",
    "{agency}이 공개한 사례집에는 \"{quote}\"처럼 학교 안에서 오가는 대화가 그대로 실려 있다. 교사들은 이런 표현이 폭력과 혐오로 이어지지 않도록 지도하고 있다.",
]
CONTACT_NEG = [
    "○ (전화) {tel}, (이메일) {mail}",
    "문의처 : {org} 담당자 {tel} / {mail}",
    "접수 방법 : 이메일 {mail} 또는 팩스 {tel2}",
    "담당자 연락처 {tel}  이메일 {mail}",
    "참가 신청 : 홈페이지 {url} 접수, 문의 {tel}",
    "상담 시간 평일 09:00~18:00 (전화 {tel}, 이메일 {mail})",
    "행사 관련 문의는 {org} 사무국(010-{a}-{b}, {mail})으로 연락해 주세요.",
    "주최 : {org} / 주관 : 운영위원회 / 문의 : {tel} / 누리집 : {url}",
]
NORMAL_PARA = [
    "가을 산행에서는 일교차가 크기 때문에 얇은 옷을 겹쳐 입는 것이 좋다. 방수 기능이 있는 겉옷과 보온성이 좋은 중간 옷을 준비하고, 출발 전 기상 예보를 확인한다.",
    "시립도서관은 이번 달부터 평일 운영 시간을 오후 8시까지 연장한다. 이용자는 대출증을 가지고 방문하면 되며 자세한 휴관일은 누리집에서 확인할 수 있다.",
    "올해 지역 축제는 주민 참여 프로그램을 늘려 진행한다. 전통시장 장터와 먹거리 부스, 청소년 공연 무대가 마련되며 행사 기간 동안 임시 주차장이 운영된다.",
    "생활 속 에너지 절약을 위해 사용하지 않는 전자제품의 플러그를 뽑고, 난방 온도를 적정하게 유지하는 것이 도움이 된다. 시민 실천 사례는 공모전으로 접수한다.",
    "문학 작품을 읽을 때는 인물의 심리와 배경을 함께 살펴보면 이야기가 더 깊게 다가온다. 독서 모임에서는 한 장씩 돌아가며 느낀 점을 나누는 방식으로 진행한다.",
    "어린이 교통안전 교육은 횡단보도 건너기, 안전벨트 착용, 자전거 헬멧 쓰기 순서로 진행된다. 신청한 학교에는 강사가 직접 방문해 실습 수업을 한다.",
    "환절기에는 감기와 알레르기 질환이 늘어난다. 손 씻기와 실내 환기를 자주 하고, 충분한 수면과 수분 섭취로 면역력을 유지하는 것이 중요하다.",
    "이번 공모사업은 지역 청년 창업가를 대상으로 사업계획서를 심사해 선정한다. 선정된 팀에는 사무 공간과 멘토링, 시제품 제작비 일부가 지원된다.",
]
SEO_TPL = [
    "{brand}은(는) 오랜 기간 검증을 거쳐 믿고 쓸 {cat} 정보를 안내합니다. 가입 즉시 {benefit}, 24시간 고객센터 운영, 빠른 입출금과 {pct} 보너스를 제공합니다. 지금 바로 이용해 보세요.",
    "{cat} 순위 TOP{yrs}0 — 먹튀 없는 안전한 곳만 엄선했습니다. 신규 회원 {benefit}, 매일 이벤트와 쿠폰 지급. 접속 주소는 {sdom} 입니다.",
    "{brand} 공식 안내. 해외 라이센스 보유 {cat}로 입금 보너스 {pct}, 충전 즉시 {benefit}. 가입 문의는 {contact} 로 주세요. 안전놀이터 이용 후기와 이벤트 정보를 확인하세요.",
    "{cat} 비교 {yr2} | 🔥검색 {brand}🔥 이벤트 · 가입코드 · 첫충 {pct} 혜택을 한 번에 확인하세요. {brand} {cat} 이용 방법과 후기 모음.",
    "{brand} {cat} 이용 안내 — 빠른 입출금과 보안 이체, 쿠폰·이벤트 상시 진행, 언제든 상담 가능한 고객센터. 접속 {sdom}",
]
TITLE_TPL = [
    "{tk} ▶ ✔구글검색 {tb}✔ {slogan}",
    "{tk} :: ✔네이버검색 {tb}✔ {slogan}",
    "{tk} / ☑구글검색 {tb}☑ {slogan} {sdom}",
    "{tk} ｜ 🔥구글검색 {tb}🔥 {slogan}",
]
TITLE_KW = ["고화질 영상", "무삭제", "신작 야동", "국산 야동", "해외 영상", "링크모음", "무료중계", "실시간 중계", "스포츠 무료중계", "토토 분석"]
TITLE_BRAND = ["최신주소", "바로가기", "새주소", "접속방법", "우회주소", "벳툴", "스톰티비", "한강티비"]
TITLE_SLOGAN = ["클릭하면 바로 이동", "지금 접속하세요", "무료로 바로 보기", "최신 주소 안내", "오늘의 추천 경기", "버퍼링 없는 화질"]
CHAT_TPL = [
    "라이브 쇼 채팅 오픈! 지금 접속 http://chat.{sdom} 화상채팅 상시 대기, 가입만 해도 포인트 지급 http://chat.{sdom}",
    "무료채팅 화상채팅 라이브쇼 즉석만남 {sdom} 가입 즉시 무료 포인트 증정 이벤트 진행중",
    "{brand}걸 화상채팅 · 영상통화 · 라이브쇼 상시 대기 http://{sdom} 첫 가입 {pct} 혜택",
]

# 새 풀 이름 → 목록 (make_dataset.split_pools가 이 이름들로 dev/train을 나눈다)
LONG_POOLS = {
    "game": GAME, "jobs": JOBS, "svc": SVC, "quotes": QUOTES, "brands": BRANDS, "cats": CATS, "kw": SYMBOL_KW,
    "cashw": CASHW, "anchor_pos": ANCHOR_POS, "terms": TERMS, "stat_tpl": STAT_TPL, "def_tpl": DEF_TPL,
    "job_tpl": JOB_TPL, "cons_tpl": CONS_TPL, "quote_tpl": QUOTE_TPL, "contact_neg": CONTACT_NEG,
    "normal_para": NORMAL_PARA, "seo_tpl": SEO_TPL, "chat_tpl": CHAT_TPL, "title_tpl": TITLE_TPL,
    "title_kw": TITLE_KW, "title_brand": TITLE_BRAND, "title_slogan": TITLE_SLOGAN,
}


class _Keep(dict):
    """없는 키는 {키} 그대로 두어, 뒤에서 fill()이 마저 채우게 한다."""
    def __missing__(self, key):
        return "{" + key + "}"


def lookalike(rng, s):
    """숫자를 닮은 글자로 바꿔 위장한다 (0→O/o, 1→l/I). 'l9금'·'O6O-5OO-2660' 같은 표기."""
    out = []
    for ch in s:
        if ch == "0" and rng.random() < 0.7:
            out.append(rng.choice("Oo"))
        elif ch == "1" and rng.random() < 0.6:
            out.append(rng.choice("lI"))
        else:
            out.append(ch)
    return "".join(out)


def fill3(rng, text, P, fill):
    """긴 문형의 자리표시자를 채운다. 남는 자리표시자({n}·{pct}·{org}·{a}·{b}·{id} 등)는 기존 fill()이 채운다."""
    tel = lambda: rng.choice(["02", "051", "031", "063", "053", "042"]) + f"-{rng.randint(200, 999)}-{rng.randint(1000, 9999)}"
    m = _Keep(
        subj=rng.choice(SUBJ), region=rng.choice(REGION), agency=rng.choice(AGENCY),
        game=(g := rng.sample(P["game"], 2))[0], game2=g[1],
        yr=rng.choice([2021, 2022, 2023, 2024, 2025]), yr2=rng.choice([2025, 2026]),
        yrs=rng.randint(2, 6), pop=(pop := rng.randint(300, 3000)), cnt=int(pop * rng.uniform(0.02, 0.3)),
        pc=f"{rng.randint(3, 45)}%", age=rng.randint(12, 17), amt=f"{rng.randint(1, 9)},{rng.randint(100, 999)}",
        tech=rng.choice(["스웨디시 마사지", "딥티슈 마사지", "타이 마사지", "지압", "림프 마사지", "스포츠 마사지", "아로마 케어"]),
        job=rng.choice(P["jobs"]), svc=rng.choice(P["svc"]), quote=rng.choice(P["quotes"]),
        tel=tel(), tel2=tel(),
        mail=f"{rng.choice(NEG_IDS)}{rng.choice(['', str(rng.randint(1, 99))])}@{rng.choice(NEG_MAIL_HOST)}",
        url=f"www.{rng.choice(['gasang', 'saebit', 'hanul', 'garam'])}.{rng.choice(['go.kr', 'or.kr', 're.kr'])}",
        brand=rng.choice(P["brands"]), cat=rng.choice(P["cats"]),
        tk=rng.choice(P["title_kw"]), tb=rng.choice(P["title_brand"]), slogan=rng.choice(P["title_slogan"]),
        sdom=f"{rng.choice(['luckyday', 'bigwin', 'vipclub', 'jackpot', 'kingbet'])}{rng.randint(1, 99)}.{rng.choice(SPAM_TLD)}",
        benefit=rng.choice(["꽁머니 3만원 지급", "첫충 20%", "무제한 환전", "입금 즉시 보너스", "무료 스핀 50회"]),
        contact=rng.choice(["텔레그램 @bigwin77", "카톡 vipclub", "상담 @kingbet", "오픈채팅 문의"]),
    )
    return fill(rng, text.format_map(m))


def long_negative(rng, P, fill):
    kind = rng.choice(["stat", "stat", "stat", "defs", "defs", "job", "job", "cons", "quote", "quote",
                       "contact", "contact", "contact", "para"])
    if kind == "stat":
        return fill3(rng, rng.choice(P["stat_tpl"]), P, fill)
    if kind == "defs":
        items = rng.sample(P["terms"], k=min(len(P["terms"]), rng.randint(1, 3)))
        t = rng.choice(P["def_tpl"])
        return " ".join(t.replace("{t}", a).replace("{d}", b) for a, b in items)
    if kind == "job":
        return fill3(rng, rng.choice(P["job_tpl"]), P, fill)
    if kind == "cons":
        return fill3(rng, rng.choice(P["cons_tpl"]), P, fill)
    if kind == "quote":
        return fill3(rng, rng.choice(P["quote_tpl"]), P, fill)
    if kind == "contact":
        return fill3(rng, rng.choice(P["contact_neg"]), P, fill)
    return " ".join(rng.sample(P["normal_para"], k=min(len(P["normal_para"]), rng.randint(1, 2))))


def long_positive(rng, P, fill, short_positive):
    kind = rng.choice(["seo", "seo", "symbol", "symbol", "cash", "phone", "phone", "chat", "anchor", "anchor", "anchor",
                       "tail", "tail", "title", "title", "title"])
    sdom = f"{rng.choice(['luckyday', 'bigwin', 'vipclub', 'jackpot', 'kingbet', 'ace', 'royal'])}{rng.randint(1, 99)}.{rng.choice(SPAM_TLD)}"
    if kind == "seo":
        return fill3(rng, rng.choice(P["seo_tpl"]), P, fill)
    if kind == "symbol":
        a, b = rng.choice(SYMBOLS)
        return " ".join(f"{a}{rng.choice(P['kw'])} 바로가기{b} → https://{sdom} ← {a}{rng.choice(P['kw'])} 추천{b}"
                        for _ in range(rng.randint(2, 4)))
    if kind == "cash":
        w = P["cashw"]
        sep = rng.choice([" ▶ ", " / ", " ♣ ", " | ", " ★ "])
        kws = [f"현금{rng.choice(w)}", f"리얼머니 {rng.choice(w)}", f"돈버는 {rng.choice(w)}", "실시간 환전", f"현금 {rng.choice(w)} 고수익"]
        line = sep.join(rng.sample(kws, 3)) + sep + sdom
        return " ".join([line] * rng.randint(1, 3))
    if kind == "phone":
        t1 = f"060-{rng.choice([500, 700, 300, 900])}-{rng.randint(1000, 9999)}"
        t2 = f"0{rng.choice([505, 504, 507])}-{rng.randint(100, 999)}-{rng.randint(1000, 9999)}"
        age = rng.choice(["l9금", "I9금", "19금", "1 9금", "19+"])
        words = " ".join(rng.sample(["폰팅", "전화방", "전화팅", "폰섹", "즉석만남", "화상통화", "채팅전화", "심야상담"], 3))
        return (f"{age} 《{lookalike(rng, t1)}》 {words} 『www.{sdom}』 {rng.choice(['무료체험', '첫통화 무료', '충전 이벤트'])} "
                f"《{lookalike(rng, t2)}》 {rng.choice(['익명 보장', '비밀 보장', '24시 운영'])}")
    if kind == "title":
        return fill3(rng, rng.choice(P["title_tpl"]), P, fill)
    if kind == "chat":
        return fill3(rng, rng.choice(P["chat_tpl"]), P, fill)
    if kind == "anchor":
        return rng.choice(P["anchor_pos"])
    # 정상 문단 뒤에 스팸이 붙은 글 (정상 글에 광고를 끼워 넣는 수법)
    return rng.choice(P["normal_para"]) + " " + short_positive(rng, P)
