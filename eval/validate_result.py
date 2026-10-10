"""제출 전 result.json / result_extra.json 형식 검증 (요강 붙임4 기준, 브라우저 불필요).

- 오류(ERROR): 요강상 채점 제외·0점 사유 — 반드시 고쳐야 함
- 경고(WARN) : 요강 표기 규칙과 다르지만 '같은 요소를 가리키면 정답 인정' 등으로 채점에는 영향이 적은 것

사용: python eval/validate_result.py result.json
      python eval/validate_result.py result_extra.json --extra
선택자가 실제로 요소 1개를 가리키는지는 eval/score.py(브라우저)로 확인한다.
"""
import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse

TECHNIQUES = {"HOMOGLYPH", "JAMO", "TRANSPARENT", "OFFSCREEN"}
META_REQUIRED = {"topic": str, "entry_url": str, "started_at": str, "finished_at": str}
FINDING_REQUIRED = {"id": str, "url": str, "is_violation": bool, "location": str, "evidence_text": str, "technique": str}
EXTRA_REQUIRED = {"id": str, "url": str, "location": str, "evidence_text": str, "technique": str, "extra_finding": str}


def norm_url(url):
    """요강 비교 규칙: http/https·끝 슬래시·# 무시, 쿼리 순서 무시."""
    p = urlparse(url)
    query = urlencode(sorted(parse_qsl(p.query, keep_blank_values=True)))
    return (p.netloc.lower(), p.path.rstrip("/") or "/", query)


def parse_iso(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def check_location(loc):
    """요강 location 표기 규칙 위반 목록 (경고)."""
    warns = []
    parts = loc.split(">>>")
    if len(parts) > 1 and re.search(r"(?<! )>>>|>>>(?! )|  >>>|>>>  ", loc):
        warns.append("'>>>' 앞뒤 공백 1칸 아님")
    for frame_sel in parts[:-1]:
        m = re.fullmatch(r'\s*iframe\[src="([^"]*)"\]\s*', frame_sel)
        if not m:
            warns.append(f"iframe 구간이 iframe[src=\"주소\"] 형식 아님: {frame_sel.strip()}")
        elif urlparse(m.group(1)).scheme not in ("http", "https"):
            warns.append(f"iframe src가 절대 주소 아님: {m.group(1)}")
    for seg in parts:
        # 속성 선택자 안([...])은 제외하고 결합자 '>' 앞뒤 공백 확인
        outside = re.sub(r"\[[^\]]*\]", "[]", seg)
        if re.search(r"\S>|>\S", outside.replace(">>>", "")):
            warns.append("'>' 앞뒤 공백 1칸 아님")
            break
    if re.search(r"\[[^\]]*='[^']*'\]", loc):
        warns.append("속성값을 작은따옴표로 표기 (큰따옴표 원칙)")
    if not loc.strip():
        warns.append("빈 location")
    return warns


def validate(path, extra=False):
    errors, warns = [], []
    raw = Path(path).read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        errors.append("UTF-8 BOM이 있음 (BOM 없는 UTF-8이어야 함)")
    try:
        doc = json.loads(raw.decode("utf-8-sig"))
    except UnicodeDecodeError:
        return ["UTF-8로 읽을 수 없음 → 파일 전체 파싱 불가(0점)"], warns, 0
    except json.JSONDecodeError as e:
        return [f"JSON 파싱 불가 → 파일 전체 0점: {e}"], warns, 0
    if not isinstance(doc, dict):
        return ["최상위가 객체가 아님"], warns, 0

    keys = set(doc)
    if keys != {"meta", "findings"}:
        errors.append(f"최상위 키는 meta, findings 두 개여야 함 (현재: {sorted(keys)})")
    meta = doc.get("meta")
    if not isinstance(meta, dict):
        errors.append("meta 누락 또는 객체 아님")
        meta = {}
    for k, t in META_REQUIRED.items():
        if not isinstance(meta.get(k), t) or not meta.get(k):
            errors.append(f"meta.{k} 누락 또는 문자열 아님")
    es = meta.get("elapsed_sec")
    if isinstance(es, bool) or not isinstance(es, (int, float)):
        errors.append("meta.elapsed_sec 누락 또는 숫자 아님")
    elif es < 0:
        errors.append("meta.elapsed_sec 음수")
    if "tool_version" in meta and not isinstance(meta["tool_version"], str):
        errors.append("meta.tool_version은 문자열이어야 함")
    for k in set(meta) - set(META_REQUIRED) - {"elapsed_sec", "tool_version"}:
        warns.append(f"meta에 요강에 없는 키: {k}")
    st, fi = parse_iso(meta.get("started_at")), parse_iso(meta.get("finished_at"))
    for name, v in (("started_at", st), ("finished_at", fi)):
        if meta.get(name) and v is None:
            errors.append(f"meta.{name}이 ISO 8601 형식 아님: {meta.get(name)}")
        elif v is not None and v.tzinfo is None:
            warns.append(f"meta.{name}에 시간대(+09:00 등) 없음")
    if st and fi and st.tzinfo and fi.tzinfo:
        if fi < st:
            errors.append("finished_at이 started_at보다 이름")
        elif isinstance(es, (int, float)) and abs((fi - st).total_seconds() - es) > 2:
            warns.append(f"elapsed_sec({es})와 finished_at-started_at({(fi - st).total_seconds():.0f}초) 차이가 큼")
    if meta.get("entry_url") and urlparse(meta["entry_url"]).scheme not in ("http", "https"):
        warns.append("meta.entry_url이 http(s) 주소 아님")

    findings = doc.get("findings")
    if not isinstance(findings, list):
        errors.append("findings 누락 또는 배열 아님 (0건이어도 [] 필수)")
        findings = []
    required = EXTRA_REQUIRED if extra else FINDING_REQUIRED
    ids, combos, valid = set(), {}, 0
    for i, f in enumerate(findings):
        tag = f"findings[{i}]" + (f" ({f.get('id')})" if isinstance(f, dict) and f.get("id") else "")
        if not isinstance(f, dict):
            errors.append(f"{tag}: 객체 아님 → 채점 제외")
            continue
        n_errors = len(errors)
        bad = [k for k, t in required.items() if not isinstance(f.get(k), t)]
        if bad:
            errors.append(f"{tag}: 필수 필드 누락/타입 오류 {bad} → 이 항목 채점 제외")
        tech = f.get("technique")
        if extra:
            if tech != "ETC":
                errors.append(f"{tag}: result_extra.json의 technique은 \"ETC\" 고정")
        elif isinstance(tech, str) and tech not in TECHNIQUES:
            errors.append(f"{tag}: technique 값 {tech!r} (HOMOGLYPH/JAMO/TRANSPARENT/OFFSCREEN 중 하나, 1개만)")
        if not extra and f.get("is_violation") is not True and isinstance(f.get("is_violation"), bool):
            warns.append(f"{tag}: is_violation이 false — 위반 아닌 항목은 제출하지 않는 편이 안전")
        if f.get("id") in ids:
            errors.append(f"{tag}: id 중복")
        ids.add(f.get("id"))
        if isinstance(f.get("id"), str) and not re.fullmatch(r"f_\d+", f["id"]):
            warns.append(f"{tag}: id가 예시 형식(f_001)과 다름")
        url = f.get("url")
        if isinstance(url, str):
            p = urlparse(url)
            if p.scheme not in ("http", "https") or not p.netloc:
                errors.append(f"{tag}: url이 전체 URL 아님: {url}")
            elif meta.get("entry_url") and p.netloc.lower() != urlparse(meta["entry_url"]).netloc.lower():
                warns.append(f"{tag}: 진입 URL과 다른 호스트: {p.netloc}")
        if isinstance(f.get("evidence_text"), str) and not f["evidence_text"].strip():
            warns.append(f"{tag}: evidence_text가 비어 있음 (본심사 참고자료라 반드시 기재)")
        if isinstance(f.get("location"), str):
            for w in check_location(f["location"]):
                warns.append(f"{tag}: location {w}")
        for k in set(f) - set(required) - ({"is_violation"} if extra else set()):
            warns.append(f"{tag}: 요강에 없는 필드 {k}")
        if isinstance(url, str) and isinstance(f.get("location"), str) and isinstance(tech, str):
            key = (norm_url(url), f["location"], tech)
            if key in combos:
                warns.append(f"{tag}: {combos[key]}와 url·location·technique 동일 → 1건으로 통합 집계")
            combos.setdefault(key, tag)
        if len(errors) == n_errors:  # 이 항목에서 오류가 없을 때만 채점 대상
            valid += 1
    return errors, warns, valid


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--extra", action="store_true", help="result_extra.json 규칙(technique=ETC, extra_finding 필수)")
    args = ap.parse_args()
    errors, warns, valid = validate(args.path, args.extra)
    for e in errors:
        print("ERROR", e)
    for w in warns:
        print("WARN ", w)
    print(f"→ 오류 {len(errors)}건, 경고 {len(warns)}건, 채점 대상 findings {valid}건")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
