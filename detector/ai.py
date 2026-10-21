"""규칙 경계 검증과 수집된 DOM 잔여 회수. 실모델은 필요한 단계에서만 연다."""
import hashlib
import html
import json
import math
import re
import time
from collections import Counter
from contextlib import ExitStack
from html.parser import HTMLParser
from urllib.parse import urlparse

from . import adscore
from .textnorm import normalize
from .laya_runtime import LayaCleanupError, LayaRunner
from .inspection import project, split_row, matches as matches_inspection


# Gemma 넘김 최소화 — Gemma는 1건에 5~10초라 마감까지 남는 몇 분에 수십 건만 볼 수 있다.
# counselpsy(1,089페이지): 대기 2,500건 중 327건만 처리, 96%가 20자 미만 링크 문구('강남 달토')라
# Gemma가 판단할 내용 없이 정상으로 뒤집었다. 세부 점수로 정말 판단할 수 없는 것만 골라
# 남은 시간 ÷ 건당 시간(main.GEMMA_SEC_PER_ITEM)만큼만 보낸다.
GEMMA_MIN_CHARS = 20         # 이보다 짧은 문구(링크 글자·메뉴)는 Gemma도 근거가 없어 Laya 판정을 그대로 쓴다
GEMMA_MIN_AMBIGUITY = .6     # 판단 곤란 점수(0~1)가 이 이상인 것만 Gemma 후보


def ambiguity(text, score, p, conflict=False, text_score=None):
    """(판단 곤란 점수, 제외 이유). Laya 확률이 0.5에 가까울수록, 규칙 점수가 경계 구간(LOW~HIGH) 가운데일수록,
    규칙과 Laya가 서로 반대쪽으로 강하게 기울수록 높다. 점수가 None이면 Gemma로 보내지 않는다
    ('short' 짧은 문구, 'clear' 판단 가능, 'context' 교육·보도 페이지의 짧은 문구라 맥락대로 정상).
    예: 규칙 점수가 경계 가운데면 Laya 0.34~0.66, 경계 끝(LOW·HIGH)이면 Laya 0.5 언저리만 후보.
    conflict(Laya는 광고 확신, 페이지는 교육·보도 글): text_score(맥락 감점을 뺀 문구 자체 점수)로 나눈다 —
    문구에 광고 단서가 충분하면(HIGH 이상) 끼어든 스팸이라 Laya를 따르고('clear'), 짧으면 '카지노' 메뉴·시설명 같은
    맥락 속 단어라 정상('context'), 그 사이만 Gemma 후보. (hanyanglaw·counselpsy 10-22: 이 경로의 스팸 32건을 Gemma가 정상으로 뒤집음)"""
    short = len(" ".join(str(text).split())) < GEMMA_MIN_CHARS
    if conflict:
        if text_score is not None and text_score >= adscore.HIGH:
            return None, "clear"
        if short:
            return None, "context"
        return max(GEMMA_MIN_AMBIGUITY, ambiguity(text, score, p)[0] or 0), None
    if short:
        return None, "short"
    r = min(1, max(0, (score - adscore.LOW) / (adscore.HIGH - adscore.LOW)))
    a = .6 * (1 - abs(2 * p - 1)) + .2 * (1 - abs(2 * r - 1)) + .2 * max(0, -(2 * p - 1) * (2 * r - 1))
    return (round(a, 3), None) if a >= GEMMA_MIN_AMBIGUITY else (None, "clear")


# URL만 있는 문구 ('https://www.wooribugo4.com/page/funeral/view.php?…', 'pf.kakao.com/_lGzxkn')
BARE_URL = re.compile(r"(?:https?://|www\.)?[a-z0-9가-힣-]+(?:\.[a-z0-9가-힣-]+)*\.(?:[a-z]{2,}|xn--[a-z0-9-]+|[가-힣]{2,})(?::\d+)?(?:[/?#]\S*)?", re.I)
RULE_CLUE_MIN = 1  # 잔여 선별 단서 최소 점수: '바로가기'(0.3)·'중계'(0.5)·'주소'(0.5) 같은 약한 말만 있으면 단서로 치지 않는다


def bare_url(text):
    """문구가 URL 하나뿐인가. Laya는 URL만 보고는 판단할 근거가 없다 — djbokji(10-22) 게시글 속 부고·청첩장·카카오 채널
    링크 15건을 0.78~0.94로 광고 판정했고, hanyanglaw 스팸 URL('www.bbs7757.com')은 0.07로 정상 판정했다.
    URL만 있는 문구는 규칙(도메인 속 키워드·메신저 ID 등)으로만 판정한다."""
    t = " ".join(str(text).split()).strip("<>()[]【】'\" ")
    return bool(t) and " " not in t and BARE_URL.fullmatch(t) is not None


def digest(item):
    return hashlib.sha256(json.dumps({k: item.get(k, "") for k in ("html", "context")},
                                    ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def valid_binding(row, item, owner):
    """모델 문구가 실제 요청 소유 단위의 원문 범위에 결합되는지 확인한다."""
    field, start, end = row.get("source_field"), row.get("start_char"), row.get("end_char")
    return (row.get("owner_id") == owner and row.get("source_sha256") == digest(item)
            and isinstance(row.get("evidence_id"), str) and field in ("html", "context")
            and type(start) is int and type(end) is int
            and 0 <= start < end <= len(item.get(field, ""))
            and item[field][start:end] == row.get("evidence"))


_ROOT_ATTR = re.compile(r'''\s+([^\s"'<>/=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'))?''')


class _OwnerText(HTMLParser):
    """하위 요소의 소유 관계를 추측하지 않고 단일 요소의 본문만 읽는다."""
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.source = source
        self.tag = None
        self.closed = False
        self.parts = []
        self.opening = ""
        self.body_start = self.body_end = 0

    def _char_offset(self):
        line, column = self.getpos()
        return sum(len(part) + 1 for part in self.source.split("\n")[:line - 1]) + column

    def handle_starttag(self, tag, attrs):
        if tag == "br" and not attrs and self.tag is not None and not self.closed:
            return
        if self.tag is not None or tag in ("script", "style", "textarea", "template", "noscript"):
            raise ValueError("본문 소유가 불명확한 HTML")
        self.tag = tag
        self.text_attrs = [name for name, value in attrs
                           if name in ("alt", "title", "aria-label") and value and value.strip()]
        self.has_text_attrs = bool(self.text_attrs)
        self.opening = self.get_starttag_text()
        self.body_start = self._char_offset() + len(self.opening)
        if tag == "meta":
            self.closed = True

    def handle_startendtag(self, tag, attrs):
        if tag == "br" and not attrs and self.tag is not None and not self.closed:
            return
        self.handle_starttag(tag, attrs)
        if tag != "meta":
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.tag != tag or self.closed:
            raise ValueError("단일 요소가 아닌 HTML")
        self.closed = True
        self.body_end = self._char_offset()

    def handle_data(self, data):
        if self.tag is None or self.closed:
            if data.strip():
                raise ValueError("소유 요소 밖의 본문")
        else:
            self.parts.append(data)

    def handle_comment(self, data):
        raise ValueError("주석 포함 근거는 재검토")

    def handle_decl(self, decl):
        raise ValueError("선언 포함 근거는 재검토")

    def handle_pi(self, data):
        raise ValueError("처리 지시 포함 근거는 재검토")

    def attribute_span(self, name):
        # outerHTML의 인용된 속성값만 지원하며, 다른 속성값 내부를 재검색하지 않는다.
        position, matches = len(self.tag) + 1, []
        while match := _ROOT_ATTR.match(self.opening, position):
            if match[1].lower() == name:
                group = 2 if match[2] is not None else 3
                if match[group] is None:
                    return None
                base = self.body_start - len(self.opening)
                matches.append((base + match.start(group), base + match.end(group), match[group]))
            position = match.end()
        if self.opening[position:].strip() not in (">", "/>") or len(matches) != 1:
            return None
        return matches[0]


def matching_recovery_text(binding, row):
    """검증된 binding의 실제 범위를 현재 본문/속성 소유 단위에 결합한다."""
    if 'inspection' in row:
        return matches_inspection(binding, row)
    evidence = binding["evidence"]
    expected = normalize(row["evidence_text"] if "evidence_text" in row else row["text"])
    bare_match = normalize(html.unescape(evidence)) == expected
    source = row.get("source_html", "")
    if binding.get("source_field") != "html":
        return False
    parser = _OwnerText(source)
    try:
        parser.feed(source)
        parser.close()
    except ValueError:
        return False
    if parser.tag != row.get("tag", "").lower():
        return False
    attr = row.get("attr")
    if parser.tag == "meta":
        span = parser.attribute_span("content")
        return (not attr and not parser.has_text_attrs and source == parser.opening and evidence == source
                and binding["start_char"] == 0 and binding["end_char"] == len(source)
                and span is not None and bool(normalize(html.unescape(span[2])))
                and normalize(html.unescape(span[2])) == expected)
    if attr:
        if attr not in ("alt", "title", "aria-label") or not (parser.closed or source == parser.opening):
            return False
        span = parser.attribute_span(attr)
        if span is None:
            return False
        value = normalize(html.unescape(span[2]))
        if (parser.tag == "img" and source == parser.opening and evidence == source
                and binding["start_char"] == 0 and binding["end_char"] == len(source)):
            # img 루트 단독 원문 전체는 그 요소의 유일한 비어 있지 않은 텍스트 속성에만 결합한다.
            return parser.text_attrs == [attr] and bool(value) and value == expected
        return (bare_match and span[0] <= binding["start_char"] < binding["end_char"] <= span[1]
                and value == expected)
    if not parser.closed:
        return False
    if evidence == source:
        return not parser.has_text_attrs and normalize("".join(parser.parts)) == expected
    # 같은 문구라도 선택 offset이 루트 직접 본문에 속해야 한다.
    return (bare_match
            and parser.body_start <= binding["start_char"] < binding["end_char"] <= parser.body_end)


def analyze(cands, pages, model_path, use_ml, deadline=None, cancel=None, laya_confidence_threshold=.8, cancel_error=None, detect_posted=True, telemetry=None, laya_deadline=None, residual_needs_rule_clue=False,
            gemma_sec_per_item=None):
    from .ai_telemetry import AIProgress, context_conflict
    from .main import ScanCancelled, candidates_from, judge, source_fields
    from local_llm.prompts import DECISION_POLICY
    ScanCancelled = cancel_error or ScanCancelled
    if not .5 <= laya_confidence_threshold <= 1:
        raise ValueError("laya_confidence_threshold는 0.5~1 범위여야 합니다")
    stats = {"mode": "policy" if use_ml else "diagnostic", "laya_confidence_threshold": laya_confidence_threshold,
             "policy": "laya_low_confidence_or_context_conflict",
             "gemma_decision_policy": DECISION_POLICY,
             "laya_inputs": 0, "laya_preflight_rejected": 0,
             "length_skipped": [], "length_skipped_count": 0, "length_skipped_laya": 0, "length_skipped_gemma": 0,
             "gemma_verify": 0, "gemma_extract": 0, "residual_screened_clean": 0, "residual_no_rule_clue": 0, "residual_bare_url": 0, "border_bare_url": 0,
             "gemma_requests": dict.fromkeys(("attempted", "succeeded", "transport_failed", "output_failed", "preflight_failed", "reused"), 0),
             "source_rows": 0, "unique_count": 0, "cache_reuse": 0,
             "laya_sec": 0.0, "gemma_verify_sec": 0.0, "gemma_extract_sec": 0.0,
             "unresolved_sources": [], "failures": [], "source_results": [],
             "scope": "수집된 DOM 텍스트 소유 단위; 전체 HTTP 원문 및 이미지 검사는 포함하지 않음"}

    meter = AIProgress(telemetry, allow_context_conflict=True)
    stats["telemetry"] = meter.snapshot
    parent_groups = []

    def combine_units():
        for parent, units in parent_groups:
            if len(units) == 1:
                parent.update({k: v for k, v in units[0].items()
                               if k not in ('source_id', 'evidence_text', 'text', 'raw', 'normalized')})
                continue
            parent['inspection_units'] = [{k: v for k, v in unit.items()
                                           if k not in ('source_html', 'source_context', 'source_near_text')}
                                          for unit in units]
            ads = [u for u in units if u.get('verdict') == 'ad' and u.get('processing_status') == 'completed']
            clean = all(u.get('verdict') == 'clean' and u.get('processing_status') == 'completed' for u in units)
            parent.pop('ml_prob', None)  # 여러 조각의 확률을 원문 전체의 확률로 합성하지 않는다.
            parent.pop('laya_confidence', None)
            parent['laya_confidence_scope'] = 'inspection_units'
            if ads or clean:
                selected = ads[0] if ads else next((u for u in units if u.get('decided_by') == 'gemma'), units[0])
                parent.update(verdict='ad' if ads else 'clean', decided_by=selected.get('decided_by', 'laya'),
                              processing_status='completed',
                              reason=f"분할 {len(units)}개: 광고 {len(ads)}개; " + ('전부 정상' if clean else '선택 근거는 inspection_units에 보존'))
                if ads:
                    for unit in units:
                        if unit.get('processing_status') != 'completed':
                            unresolved(unit, '광고 근거 외 나머지 조각 미판정: ' + unit.get('reason', '재검토'))
            else:
                review(parent, f"분할 {len(units)}개 중 미판정 조각 있음; 전체 정상 확정 금지")

    def finish():
        combine_units()
        laya, gemma = meter.snapshot["laya"], meter.snapshot["gemma"]
        complete = (use_ml and check() and laya["completed"] + laya["failed"] == laya["total"]
                    and gemma["settled"] == gemma["eligible_verify"] + gemma["eligible_extract"])
        stats["telemetry"] = meter.progress(
            "complete" if complete else "incomplete" if use_ml else "diagnostic")
        for stage in ("laya", "gemma"):
            stats[f"length_skipped_{stage}"] = sum(x["stage"] == stage for x in stats["length_skipped"])
        stats["length_skipped_count"] = len(stats["length_skipped"])
        stats['review_types'] = dict(Counter(
            row.get('review_code', 'other_review') for row in
            [c for c in cands if c.get('verdict') == 'uncertain'] + stats['unresolved_sources']))
        return stats

    def check():
        if cancel is not None and cancel.is_set():
            raise ScanCancelled()
        return deadline is None or time.perf_counter() < deadline

    def review(c, reason, status="review", code=None):
        c.update(verdict="uncertain", processing_status=status, reason=reason)
        if code is not None:
            c['review_code'] = code

    def laya_fallback(c, why):
        """Gemma로 보내지 않거나 Gemma 시간이 끝난 항목은 보류 대신 Laya 판정으로 확정 (gemma_sec_per_item를 줄 때만).
        문맥 충돌 항목(교육·보도 페이지에서 Laya만 광고 확신)은 Gemma 확인 없이 광고로 확정하지 않고 맥락대로 정상."""
        p = c.get("ml_prob")
        if gemma_sec_per_item is None or p is None:
            return False
        c.pop("review_code", None)
        if c.get("context_conflict"):
            c.update(verdict="clean", decided_by="rules", processing_status="completed",
                     reason=f"맥락 정상 ({why}: 교육·보도 페이지에서 Laya만 광고 확신)")
        else:
            c.update(verdict="ad" if p >= .5 else "clean", decided_by="laya", processing_status="completed",
                     reason=f"Laya 판정 ({why})")
        return True

    def unresolved(row, reason, status="review", code=None):
        stats["unresolved_sources"].append({**source_fields(row), "selector": row.get("selector"),
                                            "url": row.get("url"), "text": row.get('evidence_text', row.get('text', '')),
                                            "reason": str(reason), "processing_status": status,
                                            **({'review_code': code} if code else {})})

    length_seen = set()

    def length_skip(stage, row, reason, tokens=None, limit=None):
        """입력 길이 때문에 모델이 판정하지 못한 원문을 대시보드용으로 따로 모은다(단계별 중복 제외)."""
        marker = (stage, row.get("source_id"))
        if marker in length_seen:
            return
        length_seen.add(marker)
        stats["length_skipped"].append({
            "source_id": row.get("source_id"), "stage": stage, "url": row.get("url"), "selector": row.get("selector"),
            "chars": len(row.get("evidence_text") or row.get("text") or ""),
            "input_tokens": tokens, "token_limit": limit, "reason": str(reason)})

    def input_for(row):
        item = {"html": row.get("source_html", ""), "context": row.get("source_context", "")}
        if 'inspection' in row:
            item['target_spans'] = row['inspection']['target_spans']
        return item

    # 이 호출의 model/QUESTION은 고정. Laya 입력만 공유하고 Gemma 원문 결합은 owner별 유지.
    def key(row):
        return normalize(row.get("evidence_text", row.get("text", "")))

    def rule_clue(row, url):
        """규칙 사전의 가산 단서(키워드·연락처·도메인 등)가 하나라도 있는가.
        단서가 하나도 없거나 약한 말뿐인 문구('패밀리 사이트'·기사 제목·주소·'… 바로가기')를 Laya가 광고로 확신하는 오탐이 많고
        (jnbburi 190건 전부), 애매한 것은 Gemma 추출로 넘어가 시간을 잡아먹어(hanyanglaw 2,414건) 잔여 선별에서 뺀다."""
        return sum(w for _, w in adscore.explain(key(row), urlparse(url).netloc)[1] if w > 0) >= RULE_CLUE_MIN

    check()
    used = {c.get("source_id") for c in cands}
    residual, seen = [], set()
    for url, frames in pages:
        for prefix, data in frames:
            for row in data.get("source_blocks", []):
                sid = row.get("source_id")
                if sid and sid not in used and sid not in seen:
                    seen.add(sid)
                    if residual_needs_rule_clue and not rule_clue(row, url):
                        stats["residual_no_rule_clue"] += 1
                        continue
                    if residual_needs_rule_clue and bare_url(key(row)):  # 규칙 확정 미만인 URL만 있는 문구는 정상
                        stats["residual_bare_url"] += 1
                        continue
                    residual.append({**row, "url": url, "prefix": prefix})
    border = []
    for c in cands:
        if c["verdict"] == "borderline" and bare_url(c.get("evidence_text", "")):
            stats["border_bare_url"] += 1
            c.update(verdict="clean", processing_status="completed",
                     reason="URL만 있는 문구: 규칙 점수로만 판정(확정 점수 미만이라 정상)")
        elif c["verdict"] == "borderline":
            border.append(c)
            review(c, "AI 판정 대기")
        else:
            c.update(processing_status="completed", reason="기존 규칙 확정")

    predictions = {}
    rejections = {}
    rows_by_key = {}
    for row in border + residual:
        rows_by_key.setdefault(key(row), []).append(row)
    stats["source_rows"] = sum(len(owners) for owners in rows_by_key.values())
    stats["unique_count"] = len(rows_by_key)
    stats["cache_reuse"] = stats["source_rows"] - stats["unique_count"]
    laya_counts = meter.snapshot["laya"]
    laya_counts["total"] = len(rows_by_key)
    meter.progress("laya")
    if not use_ml:
        for c in border:
            review(c, "명시적 진단 모드: AI 판정 생략")
        for row in residual:
            unresolved(row, "명시적 진단 모드: 잔여 선별 생략")
        return finish()

    laya_started = time.perf_counter()
    try:
        if rows_by_key and check():
            from training.make_dataset import QUESTION
            # Laya는 laya_deadline(크롤링+Laya 21분)까지, 남은 시간은 Gemma 몫
            laya_until = min((d for d in (deadline, laya_deadline) if d is not None), default=None)
            with LayaRunner(model_path, deadline=laya_until, cancel=cancel) as runner:
                # 같은 child/tokenizer로 검사 계획만 만든다. 모델/수집 문구/규칙은 바꾸지 않는다.
                projected, texts = {}, set()
                for row in border + residual:
                    try:
                        projection = project(row)
                        projected[id(row)] = projection
                        texts.add(projection['text'])
                    except (ValueError, TypeError) as exc:
                        row['_inspection_error'] = str(exc)
                planned = {}
                ordered = sorted(texts)
                for offset in range(0, len(ordered), 64):
                    if not check():
                        break
                    batch = ordered[offset:offset + 64]
                    # 기존 외부 테스트 seam에는 plan API가 없을 수 있다. 실제 runner는 항상 제공한다.
                    planner = getattr(runner, 'plan_batch', None)
                    plans = (planner(batch, QUESTION, max_chars=1024) if planner else
                             [{'segments': [{'start_char': 0, 'end_char': len(t)}]} for t in batch])
                    if len(plans) != len(batch):
                        raise ValueError('Laya 검사 계획 개수 불일치')
                    planned.update(zip(batch, plans))
                inspection_stats = dict(parents=0, units=0, projected=0, split_parents=0, unmapped=0)

                def expand(row, candidate=False):
                    inspection_stats['parents'] += 1
                    projection = projected.get(id(row))
                    try:
                        if projection is None:
                            raise ValueError(row.get('_inspection_error', '원문 투영 없음'))
                        plan = planned.get(projection['text'], {})
                        if plan.get('error'):
                            raise ValueError(plan['error'])
                        units = split_row(row, projection, plan.get('segments'))
                    except (ValueError, TypeError, KeyError) as exc:
                        row['_inspection_error'] = str(exc)
                        inspection_stats['unmapped'] += 1
                        inspection_stats['units'] += 1
                        return [row]
                    inspection_stats['projected'] += 1
                    inspection_stats['split_parents'] += int(len(units) > 1)
                    inspection_stats['units'] += len(units)
                    if candidate:
                        parent_groups.append((row, units))
                    return units

                border = [unit for row in border for unit in expand(row, True)]
                residual = [unit for row in residual for unit in expand(row)]
                stats['inspection'] = inspection_stats
                rows_by_key = {}
                for row in border + residual:
                    rows_by_key.setdefault(key(row), []).append(row)
                stats['source_rows'] = sum(map(len, rows_by_key.values()))
                stats['unique_count'] = len(rows_by_key)
                stats['cache_reuse'] = stats['source_rows'] - stats['unique_count']
                laya_counts['total'] = len(rows_by_key)
                meter.progress('laya')
                # 긴 조각 하나 때문에 같은 batch의 짧은 문구까지 padding하지 않도록 묶는다.
                # 모델/질문/batch_size/확률 검증은 같고, 결과는 원문 key로 다시 연결한다.
                keys = sorted(rows_by_key, key=lambda k: (max(
                    (r.get('inspection', {}).get('planned_input_tokens') or len(k))
                    for r in rows_by_key[k]), k))
                for offset in range(0, len(keys), 64):
                    if not check():
                        break
                    chunk = keys[offset:offset + 64]
                    stats["laya_inputs"] += len(chunk)
                    laya_counts["attempted"] += len(chunk)
                    laya_counts["in_flight"] += len(chunk)
                    settled_before = laya_counts["completed"] + laya_counts["failed"]
                    meter.progress()
                    try:
                        outputs = runner.predict_batch(chunk, QUESTION, batch_size=16)
                        if not check():
                            break
                        for index, k in enumerate(chunk):
                            try:
                                out = outputs[index]
                                preflight = out.get("preflight") or {}
                                if (out.get("status") == "preflight_rejected"
                                        and preflight.get("reason") == "input_too_long"):
                                    rejection = {"input_tokens": preflight.get("input_tokens"),
                                                 "token_limit": preflight.get("token_limit"),
                                                 "model_invoked": False}
                                    rejection["reason"] = (f"Laya 입력 길이 초과 ({rejection['input_tokens']} > "
                                                           f"{rejection['token_limit']} 토큰): 모델 추론 생략")
                                    rejections[k] = rejection
                                    stats["laya_preflight_rejected"] += 1
                                    laya_counts["preflight_rejected"] += 1
                                    laya_counts["failed"] += 1
                                    stats["failures"].extend({"source_id": owner.get("source_id"),
                                                             "stage": "laya_preflight", **rejection}
                                                            for owner in rows_by_key[k])
                                    continue
                                p = float(out["answers"]["is_illegal_ad"]["noul"])
                                usage = out.get("usage") or {}
                                if (out.get("truncated") or usage.get("truncated") or usage.get("state_tokens_dropped", 0)
                                        or usage.get("truncated_questions")
                                        or out.get("status", "ok") not in ("ok", "completed")):
                                    raise ValueError("잘린 또는 미완료 Laya 출력")
                                if not math.isfinite(p) or not 0 <= p <= 1:
                                    raise ValueError("잘못된 Laya 확률")
                                predictions[k] = p
                                laya_counts["completed"] += 1
                            except (IndexError, KeyError, TypeError, ValueError):
                                laya_counts["failed"] += 1
                                stats["failures"].extend({"source_id": owner.get("source_id"), "stage": "laya", "reason": "출력 누락/잘림/오류"}
                                                         for owner in rows_by_key[k])
                    except LayaCleanupError:
                        raise
                    except Exception as exc:
                        check()
                        settled = laya_counts["completed"] + laya_counts["failed"] - settled_before
                        laya_counts["failed"] += len(chunk) - settled
                        stats["failures"].extend({"source_id": owner.get("source_id"), "stage": "laya", "reason": str(exc)}
                                                 for k in chunk for owner in rows_by_key[k])
                    finally:
                        laya_counts["in_flight"] -= len(chunk)
                        meter.progress()
    except LayaCleanupError as exc:
        # __enter__ 실패 때도 PID/정리 단계가 포함된 전용 타입으로 Gemma 시작을 차단한다.
        stats["failures"].extend({"source_id": r.get("source_id"), "stage": "laya_cleanup", "reason": str(exc)}
                                 for owners in rows_by_key.values() for r in owners)
        for c in border:
            review(c, str(exc), "error")
        for row in residual:
            unresolved(row, str(exc), "error")
        combine_units()
        stats['telemetry'] = meter.progress("incomplete")
        return stats
    except Exception as exc:
        check()
        stats["failures"].extend({"source_id": r.get("source_id"), "stage": "laya", "reason": str(exc)} for owners in rows_by_key.values() for r in owners)
    finally:
        stats["laya_sec"] = round(time.perf_counter() - laya_started, 3)
    check()

    pending = []
    def route(c):
        if not c.get("source_html"):
            review(c, "원본 HTML 없음: 재합성하지 않음")
            stats["failures"].append({"source_id": c.get("source_id"), "stage": "source", "reason": c["reason"]})
            return
        p = predictions.get(key(c))
        if p is None:
            rejection = rejections.get(key(c))
            if rejection:
                c["laya_preflight"] = dict(rejection)
                length_skip("laya", c, rejection["reason"], rejection["input_tokens"], rejection["token_limit"])
            review(c, rejection["reason"] if rejection else
                   "Laya 출력 누락/잘림/오류: 유효 확률 없이 Gemma 대체 판정 금지",
                   code='laya_input_length' if rejection else 'laya_output_unavailable')
            return
        c["ml_prob"] = round(p, 4)
        c["laya_confidence"] = max(p, 1 - p)
        c.pop('review_code', None)
        if max(p, 1 - p) >= laya_confidence_threshold:
            if context_conflict(c, p, laya_confidence_threshold):
                if c.get('_inspection_error'):
                    review(c, '공통 검사 범위 결합 불가: ' + c['_inspection_error'], code='source_binding_unavailable')
                    return
                pending.append(c)
                meter.snapshot['gemma']['eligible_verify'] += 1
                meter.progress('gemma')
                return
            c.update(verdict="ad" if p >= .5 else "clean", decided_by="laya",
                     processing_status="completed", reason="Laya 정상 출력의 confidence가 운영 임계값 이상")
        else:
            if c.get('_inspection_error'):
                review(c, '공통 검사 범위 결합 불가: ' + c['_inspection_error'])
                return
            pending.append(c)
            meter.snapshot["gemma"]["eligible_verify"] += 1
            meter.progress("gemma")
    for c in border:
        route(c)

    def recover_row(row, extraction=None):
        data = {"frame_url": row["url"], "hidden": [row] if row.get("signals") else [],
                "texts": [] if row.get("signals") else [row], "head": [], "links": []}
        new = candidates_from([(row["url"], [(row["prefix"], data)])],
                              detect_posted=detect_posted, recover_posted=True)
        if not new and not detect_posted:
            result = {"source_id": row["source_id"], "processing_status": "excluded",
                      "reason": "게시형 탐지 제외 설정"}
            if extraction is not None:
                result["extraction"] = extraction
            stats["source_results"].append(result)
            return
        if not new:
            unresolved(row, "광고 근거 회수됨; 원문에서 기법을 확정할 수 없음")
            if extraction is not None:
                stats["source_results"].append({"source_id": row["source_id"], "extraction": extraction})
            return
        judge(new, model_path, False)
        for c in new:
            if extraction is not None:
                c["extraction"] = extraction
            if (extraction is not None and c["verdict"] != "borderline"
                    and not c.get("recovered_without_rule_hit")):
                c.update(processing_status="completed", reason="회수 후 기존 규칙 확정")
            elif key(c) != key(row):
                review(c, "회수 문구와 선별 문구가 다름: Laya 확률 재사용 및 Gemma 판정 금지")
            else:
                route(c)
        cands.extend(new)

    extract_rows = []
    for row in residual:
        p = predictions.get(key(row))
        if not row.get("source_html"):
            unresolved(row, "원본 HTML 없음: 재합성하지 않음")
        elif p is None:
            rejection = rejections.get(key(row))
            unresolved(row, rejection["reason"] if rejection else
                       "유효 Laya 확률 없음: Gemma 대체 추출 금지")
            if rejection:
                stats["unresolved_sources"][-1]["laya_preflight"] = dict(rejection)
                length_skip("laya", row, rejection["reason"], rejection["input_tokens"], rejection["token_limit"])
        elif max(p, 1 - p) >= laya_confidence_threshold:
            if p < .5:
                stats["residual_screened_clean"] += 1
                stats["source_results"].append({"source_id": row["source_id"], "verdict": "clean", "decided_by": "laya",
                                                "ml_prob": round(p, 4), "laya_confidence": max(p, 1-p),
                                                **({'inspection': row['inspection']} if 'inspection' in row else {})})
            else:
                stats["source_results"].append({**source_fields(row), "verdict": "ad", "decided_by": "laya",
                                                "ml_prob": round(p, 4), "laya_confidence": max(p, 1 - p)})
                recover_row(row)
        else:
            if row.get('_inspection_error'):
                unresolved(row, '공통 검사 범위 결합 불가: ' + row['_inspection_error'])
                continue
            extract_rows.append(row)
            meter.snapshot["gemma"]["eligible_extract"] += 1
            meter.progress("gemma")

    stats["gemma_skipped"] = {"short": 0, "clear": 0, "context": 0, "budget": 0}
    CONTEXT_SIGNALS = ("page-context", "body-context")

    def select_for_gemma(items, rows):
        """Gemma 대기열(검증 items·추출 rows)을 판단 곤란 점수로 줄 세워 남은 시간 ÷ GEMMA_SEC_PER_ITEM 건만 남기고
        나머지는 Laya 판정으로 확정한다. gemma_sec_per_item가 없으면 그대로(기존 동작)."""
        if gemma_sec_per_item is None:
            return items, rows
        ranked = []
        for c in items:
            p = c["ml_prob"]
            detail = c.get("score_detail") or []
            text_score = sum(w for n, w in detail if n not in CONTEXT_SIGNALS) if detail else None
            conflict = context_conflict(c, p, laya_confidence_threshold)
            a, why = ambiguity(c.get("evidence_text", ""), c.get("score", 0), p, conflict, text_score)
            if conflict and why is None:
                c["context_conflict"] = True  # Gemma 시간이 모자라면 laya_fallback이 맥락대로 정상 처리
            ranked.append((a, why, "verify", c))
        for row in rows:
            p = predictions[key(row)]
            score = adscore.explain(key(row), urlparse(row["url"]).netloc)[0]
            a, why = ambiguity(row.get("evidence_text", row.get("text", "")), score, p)
            ranked.append((a, why, "extract", row))
        budget = None if deadline is None else max(0, int((deadline - time.perf_counter()) // gemma_sec_per_item))
        chosen = sorted((x for x in ranked if x[0] is not None), key=lambda x: -x[0])
        keep = {id(x[3]) for x in (chosen if budget is None else chosen[:budget])}
        kept_items, kept_rows = [], []
        for a, why, kind, obj in sorted(ranked, key=lambda x: -(x[0] or 0)):
            if id(obj) in keep:
                obj["gemma_ambiguity"] = a
                (kept_items if kind == "verify" else kept_rows).append(obj)
                continue
            why = why or "budget"
            stats["gemma_skipped"][why] += 1
            label = {"short": "짧은 문구라 Gemma 대상 아님", "clear": "규칙·Laya로 판단 가능",
                     "context": "교육·보도 페이지의 짧은 문구", "budget": "Gemma 시간 한도"}[why]
            if kind == "verify" and why == "context":
                obj.pop("review_code", None)
                obj.update(verdict="clean", decided_by="rules", processing_status="completed",
                           reason=f"맥락 정상 ({label}: Laya 광고 확신이지만 문구 자체 단서가 약함)")
                obj["gemma_skip"] = why
            elif kind == "verify":
                laya_fallback(obj, label)
                obj["gemma_skip"] = why
            else:  # 잔여 원문(규칙 확정 미만)은 Gemma가 광고 근거를 확인하지 않으면 정상으로 둔다
                stats["residual_screened_clean"] += 1
                stats["source_results"].append({"source_id": obj["source_id"], "verdict": "clean", "decided_by": "laya",
                                                "ml_prob": round(predictions[key(obj)], 4), "gemma_skip": why})
        meter.snapshot["gemma"]["eligible_verify"] -= len(items) - len(kept_items)
        meter.snapshot["gemma"]["eligible_extract"] -= len(rows) - len(kept_rows)
        meter.progress("gemma")
        return kept_items, kept_rows

    kept, extract_rows = select_for_gemma(list(pending), extract_rows)
    pending[:] = kept

    with ExitStack() as stack:
        client = None
        client_error = None
        inner_proofs = {}

        def count_requests(response):
            # extract wrapper의 합계와 block leaf를 중복 집계하지 않는다.
            if "block_calls" in response:
                for call in response["block_calls"]:
                    count_requests(call)
            elif "calls" in response:
                for call in response["calls"]:
                    count_requests(call)
                stats["gemma_requests"]["preflight_failed"] += sum(
                    f.get("preflight_failed", 0) for f in response.get("failures", []))
            elif "results" in response:
                for row in response["results"]:
                    count_requests(row.get("call", {}))
            else:
                counts = stats["gemma_requests"]
                attempted = response.get("request_count", 0)
                counts["attempted"] += attempted
                counts["reused"] += int(bool(response.get("cache_hit")))
                counts["preflight_failed"] += response.get("preflight_failed", 0)
                if response.get('request_attempts'):
                    for attempt in response['request_attempts']:
                        kind = ('succeeded' if attempt['status'] == 'ok' else
                                'output_failed' if attempt['completion_received'] else 'transport_failed')
                        counts[kind] += 1
                elif attempted:
                    kind = ("succeeded" if response.get("status") == "ok" else
                            "output_failed" if response.get("completion_received") or "raw_response" in response else
                            "transport_failed")
                    counts[kind] += attempted

        def reusable_proof(c):
            extraction = c.get("extraction", {})
            owner = extraction.get("block_id")
            call = inner_proofs.get(owner, {})
            parsed = call.get("parsed", {})
            item = {"candidate_id": owner, **input_for(c)}
            if (call.get("status") != "ok" or call.get("validation", {}).get("errors")
                    or call.get("input") != item or parsed.get("verdict") != "ad"
                    or not valid_binding(parsed, item, owner)
                    or any(parsed.get(k) != extraction.get(k) for k in
                           ("evidence", "evidence_id", "owner_id", "source_field", "start_char", "end_char", "source_sha256"))
                    or not matching_recovery_text(parsed, c)):
                return None
            return {"task": "verify", "version": "v15", "status": "ok", "results": [
                {**parsed, "candidate_id": owner, "status": "ok", "needs_review": False, "call": call}]}

        def get_client():
            nonlocal client, client_error
            if not check():
                raise TimeoutError("공유 점검 시간 예산 초과")
            if client_error:
                raise RuntimeError(client_error)
            if client is None:
                try:
                    from local_llm.client import GemmaClient
                    # Budget의 None은 30분 기본값이므로 무제한을 명시한다. 보고서에는 저장하지 않는다.
                    client = stack.enter_context(GemmaClient(deadline=math.inf if deadline is None else deadline, cancel=cancel, on_event=meter.event))
                except Exception as exc:
                    client_error = str(exc)
                    raise
            return client

        def register(owner, row, route):
            p = predictions[key(row)]
            if route == 'verify' and context_conflict(row, p, laya_confidence_threshold):
                route = 'verify_context_conflict'
            meter.register(owner, row, route, p, laya_confidence_threshold,
                           digest(input_for(row)), hashlib.sha256(key(row).encode()).hexdigest())

        def item_end(owner, status, preflight_failed=0):
            if owner not in meter.owners:
                return
            import uuid
            meter.event({"event": "item_end", "request_id": uuid.uuid4().hex,
                         "candidate_id": owner, "task": "verify", "status": status,
                         "request_count": 0, "completion_received": False,
                         "preflight_failed": preflight_failed})

        def verify_pending(start, end):
            verify_started = time.perf_counter()
            for index in range(start, end):
                c = pending[index]
                if not check():
                    if not laya_fallback(c, "Gemma 시간 한도"):
                        review(c, "공유 점검 시간 예산 초과", "timeout")
                    continue
                owner = f"{c.get('source_id', 'source')}:{index}"
                register(owner, c, "verify")
                if c.get("attr", "").startswith("::"):
                    item_end(owner, "preflight_failed", 1)
                    review(c, "CSS 가상 문구는 원본 HTML 텍스트에 결합할 수 없음")
                    continue
                if not c.get("source_html"):
                    item_end(owner, "preflight_failed", 1)
                    review(c, "원본 HTML 없음: 재합성하지 않음")
                    continue
                item = {"candidate_id": f"{c.get('source_id', 'source')}:{index}", **input_for(c)}
                try:
                    response = reusable_proof(c)
                    if response is not None:
                        item["candidate_id"] = c["extraction"]["block_id"]
                        meter.owners[item["candidate_id"]] = meter.owners.pop(owner)
                        meter.owners[item["candidate_id"]]["owner_id"] = item["candidate_id"]
                        item_end(item["candidate_id"], "reused")
                        stats["gemma_requests"]["reused"] += 1
                    else:
                        gemma = get_client()
                        stats["gemma_verify"] += 1
                        response = gemma.verify(item)
                        count_requests(response)
                    if not check():
                        raise TimeoutError("Gemma 검증 중 공유 시간 예산 초과")
                    c["gemma"] = response
                    rows = response.get("results", [])
                    row = rows[0] if len(rows) == 1 else {}
                    if (row.get('status') == 'ok' and row.get('verdict') == 'uncertain'
                            and row.get('candidate_id') == item['candidate_id']
                            and valid_binding(row, item, item['candidate_id'])
                            and matching_recovery_text(row, c)):
                        review(c, 'Gemma 정상 응답: 판단 불확실(uncertain)', code='gemma_uncertain')
                    elif (response.get("status") != "ok" or row.get("status") != "ok" or row.get("needs_review")
                            or row.get("candidate_id") != item["candidate_id"]
                            or row.get("verdict") not in ("ad", "clean")
                            or not valid_binding(row, item, item["candidate_id"])):
                        call = row.get("call") or {}
                        if "input_overflow" in str(call.get("error", "")):
                            ctx = getattr(getattr(client, "server", None), "ctx_size", None)
                            length_skip("gemma", c, call["error"], call.get("preflight_prompt_tokens"),
                                        ctx - call.get("max_tokens", 0) - 64 if ctx else None)
                        review(c, "Gemma 입력 길이 초과: 판정 보류" if "input_overflow" in str(call.get("error", ""))
                               else "Gemma 오류/출력 누락/원문 결합 실패",
                               code='gemma_input_length' if 'input_overflow' in str(call.get('error', ''))
                               else 'gemma_response_error' if row.get('status') != 'ok' else 'gemma_binding_error')
                    elif row["verdict"] == "ad" and row.get("source_field") != "html":
                        review(c, "광고 근거가 source_html에 없음: 주변 문맥만으로 현재 위치를 확정할 수 없음")
                    elif row["verdict"] == "ad" and not matching_recovery_text(row, c):
                        review(c, "선택 광고 근거가 현재 문구의 직접 본문에 결합되지 않음: 소유 관계 재검토")
                    elif row["verdict"] == "clean" and row.get("source_field") != "html":
                        review(c, "정상 근거가 대상 source_html에 없음: 주변 문맥만으로 정상 확정 불가")
                    elif row["verdict"] == "clean" and not matching_recovery_text(row, c):
                        review(c, "선택 정상 근거가 현재 문구의 직접 본문에 결합되지 않음: 소유 관계 재검토")
                    else:
                        c.update(verdict=row["verdict"], decided_by="gemma", processing_status="completed", reason="Gemma v15 원문 결합 검증 완료")
                except Exception as exc:
                    check()
                    item_end(item["candidate_id"], "preflight_failed", 1)
                    if not (isinstance(exc, TimeoutError) and laya_fallback(c, "Gemma 시간 한도")):
                        review(c, f"{type(exc).__name__}: {exc}", "timeout" if isinstance(exc, TimeoutError) else "error")
            stats["gemma_verify_sec"] = round(stats["gemma_verify_sec"] + time.perf_counter() - verify_started, 3)

        initial_pending = len(pending)
        verify_pending(0, initial_pending)

        if extract_rows:
            extract_started = time.perf_counter()
            blocks = [{"block_id": r["source_id"], **input_for(r)} for r in extract_rows]
            by_id = {r["source_id"]: r for r in extract_rows}
            for row in extract_rows:
                register(row["source_id"], row, "extract")
            try:
                gemma = get_client()
                stats["gemma_extract"] += len(blocks)
                response = gemma.extract({"blocks": blocks})
                count_requests(response)
                if not check():
                    raise TimeoutError("Gemma 추출 중 공유 시간 예산 초과")
                stats["extraction"] = response
                failed = {sid: f.get("error", "extract 실패") for f in response.get("failures", []) for sid in f.get("block_ids", [])}
                recovered = {}
                for item in response.get("candidates", []):
                    sid = item.get("block_id")
                    if sid not in by_id:
                        continue
                    if item.get("source_field") != "html":
                        failed[sid] = "회수 근거가 source_html에 없음: 주변 문맥을 현재 위치의 광고로 할당하지 않음"
                    elif not valid_binding(item, input_for(by_id[sid]), sid):
                        failed[sid] = "회수 근거의 원문 결합 실패"
                    elif not matching_recovery_text(item, by_id[sid]):
                        failed[sid] = "선택 근거와 선별 문구가 다름: 다른 문구의 Laya 캐시를 재사용하지 않고 재검토"
                    else:
                        # 동일 문구·소유 단위만 회수; model selector/technique는 사용하지 않는다.
                        recovered.setdefault(sid, item)
                cleaned = {}
                for call in response.get("calls", []):
                    for bc in call.get("block_calls", []):
                        sid = bc.get("input", {}).get("candidate_id", bc.get("block_id"))
                        parsed = bc.get("parsed", {})
                        if sid in by_id:
                            inner_proofs[sid] = bc
                        if sid in by_id and bc.get("status") == "ok" and parsed.get("verdict") == "clean":
                            if (bc.get("input") == {"candidate_id": sid, **input_for(by_id[sid])}
                                    and parsed.get("source_field") == "html"
                                    and not bc.get("validation", {}).get("errors")
                                    and valid_binding(parsed, input_for(by_id[sid]), sid)
                                    and matching_recovery_text(parsed, by_id[sid])):
                                cleaned[sid] = parsed
                for sid, row in by_id.items():
                    if sid in failed:
                        proof = inner_proofs.get(sid, {})
                        parsed = proof.get('parsed', {})
                        uncertain = (failed[sid] == 'uncertain' and proof.get('status') == 'ok'
                                     and parsed.get('verdict') == 'uncertain'
                                     and valid_binding(parsed, input_for(row), sid)
                                     and matching_recovery_text(parsed, row))
                        unresolved(row, 'Gemma 정상 응답: 판단 불확실(uncertain)' if uncertain else failed[sid],
                                   code='gemma_uncertain' if uncertain else 'gemma_extraction_error')
                        if "input_overflow" in str(failed[sid]):
                            length_skip("gemma", row, failed[sid])
                    elif sid in recovered:
                        recover_row(row, recovered[sid])
                    elif sid in cleaned:
                        stats["source_results"].append({"source_id": sid, "verdict": "clean", "decided_by": "gemma", "binding": cleaned[sid],
                                                        **({'inspection': row['inspection']} if 'inspection' in row else {})})
                    else:
                        unresolved(row, "extract 출력 누락 또는 원문 결합 실패")
            except Exception as exc:
                check()
                for row in extract_rows:
                    item_end(row["source_id"], "preflight_failed", 1)
                    unresolved(row, f"{type(exc).__name__}: {exc}", "timeout" if isinstance(exc, TimeoutError) else "error")
            finally:
                stats["gemma_extract_sec"] = round(time.perf_counter() - extract_started, 3)

        # 추출에서 회수돼 새로 들어온 검증 후보도 같은 기준·남은 시간으로 고른다
        tail, _ = select_for_gemma(pending[initial_pending:], [])
        pending[initial_pending:] = tail
        verify_pending(initial_pending, len(pending))
    check()
    stats["failures"].extend({"source_id": c.get("source_id"), "stage": "verify", "reason": c["reason"]}
                             for c in pending if c["verdict"] == "uncertain")
    return finish()
