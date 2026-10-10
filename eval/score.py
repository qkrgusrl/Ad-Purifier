"""모의 채점기: result.json 을 testsite/ground_truth.json 과 비교.

요강 붙임4 '평가 참고사항' 기준
- url·location·technique 3개 모두 일치해야 정답, 중복 제출은 1건으로 통합
- location 은 표기 방식이 아니라 '같은 요소를 가리키는지'로 판정 (실제 브라우저에서 해석)
- 선택자가 2개 이상 요소를 가리키면 오답
점수 환산은 공식 산식이 비공개라 '45×재현율 + 35×정밀도' 로 가정 (검출 시간 20점 제외).

사용: python eval/score.py result.json
"""
import asyncio
import json
import re
import sys
from pathlib import Path

from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from detector.main import norm_url  # noqa: E402

TECHNIQUES = {"HOMOGLYPH", "JAMO", "TRANSPARENT", "OFFSCREEN"}
REQUIRED = {"id": str, "url": str, "is_violation": bool, "location": str, "evidence_text": str, "technique": str}
FINGERPRINT_JS = """el => { const p = []; for (let e = el; e && e.parentElement; e = e.parentElement) {
  p.unshift(e.tagName + ':' + Array.from(e.parentElement.children).indexOf(e)); } return p.join('/'); }"""


def check_schema(path):
    raw = Path(path).read_bytes()
    problems = []
    if raw.startswith(b"\xef\xbb\xbf"):
        problems.append("UTF-8 BOM 존재")
    doc = json.loads(raw.decode("utf-8-sig"))
    if set(doc) != {"meta", "findings"}:
        problems.append(f"최상위 키 {sorted(doc)}")
    for k, t in {"topic": str, "entry_url": str, "started_at": str, "finished_at": str, "elapsed_sec": (int, float)}.items():
        if not isinstance(doc.get("meta", {}).get(k), t):
            problems.append(f"meta.{k} 누락/타입 오류")
    valid, ids = [], set()
    for f in doc.get("findings", []):
        bad = [k for k, t in REQUIRED.items() if not isinstance(f.get(k), t)]
        if f.get("technique") not in TECHNIQUES:
            bad.append("technique 값")
        if f.get("id") in ids:
            bad.append("id 중복")
        ids.add(f.get("id"))
        if bad:
            problems.append(f"{f.get('id')}: {bad} → 채점 제외")
        else:
            valid.append(f)
    return doc, valid, problems


async def resolve(ctx, cache, url, location):
    """(정규화 URL, 요소 지문) 또는 실패 사유 문자열."""
    if url not in cache:
        page = await ctx.new_page()
        await page.goto(url, wait_until="networkidle")
        cache[url] = page
    frame = cache[url].main_frame
    parts = [p.strip() for p in location.split(">>>")]
    chain = []
    for iframe_sel in parts[:-1]:
        # 요강 규칙상 src는 절대주소로 적으므로, 속성 문자열이 아닌 해석된 주소(el.src)로 비교
        m = re.fullmatch(r'iframe\[src="(.*)"\]', iframe_sel)
        if m:
            els = [e for e in await frame.query_selector_all("iframe")
                   if norm_url(await e.evaluate("e => e.src")) == norm_url(m.group(1))]
        else:
            els = await frame.query_selector_all(iframe_sel)
        if len(els) != 1:
            return f"iframe 선택자 {len(els)}개 일치: {iframe_sel}"
        chain.append(await els[0].evaluate(FINGERPRINT_JS))
        frame = await els[0].content_frame()
    try:
        els = await frame.query_selector_all(parts[-1])
    except Exception as e:
        return f"선택자 오류: {e}"
    if len(els) != 1:
        return f"요소 {len(els)}개 일치"
    chain.append(await els[0].evaluate(FINGERPRINT_JS))
    return " >>> ".join(chain)


async def main(result_path):
    gt = json.loads((Path(__file__).resolve().parents[1] / "testsite" / "ground_truth.json").read_text(encoding="utf-8"))
    base = gt["base"]
    doc, findings, problems = check_schema(result_path)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        ctx = await browser.new_context(viewport={"width": 1280, "height": 900})
        cache = {}
        gold = {}
        for a in gt["answers"]:
            url = base + a["url"]
            fp = await resolve(ctx, cache, url, a["location"].replace("{BASE}", base))
            assert not fp.startswith(("요소", "iframe", "선택자")), f"정답셋 선택자 오류 {a}: {fp}"
            gold[(norm_url(url), fp, a["technique"])] = a
        pred, rejected = {}, []
        for f in findings:
            fp = await resolve(ctx, cache, f["url"], f["location"])
            if fp.startswith(("요소", "iframe", "선택자")):
                rejected.append((f, fp))
                continue
            pred.setdefault((norm_url(f["url"]), fp, f["technique"]), f)
        await browser.close()

    tp = set(pred) & set(gold)
    n_pred = len(pred) + len(rejected)
    precision = len(tp) / n_pred if n_pred else 0.0
    recall = len(tp) / len(gold)
    print(f"스키마 문제: {problems or '없음'}")
    print(f"제출 {len(doc['findings'])}건 (중복 통합 후 {n_pred}) / 정답 {len(gold)}건")
    print(f"정탐 TP={len(tp)}  오탐 FP={n_pred - len(tp)}  미탐 FN={len(gold) - len(tp)}")
    print(f"정밀도 {precision:.3f}  재현율 {recall:.3f}  → 가정 점수 {45 * recall + 35 * precision:.1f} / 80 "
          f"(+ 검출시간 20점, elapsed {doc['meta']['elapsed_sec']}s)")
    for k in sorted(set(pred) - set(gold)):
        f = pred[k]
        print(f"  [FP] {f['technique']:11} {f['url']} | {f['location']} | {f['evidence_text'][:40]}")
    for f, why in rejected:
        print(f"  [FP-선택자] {f['technique']:11} {f['url']} | {f['location']} ({why})")
    for k in sorted(set(gold) - set(pred)):
        a = gold[k]
        print(f"  [FN] {a['technique']:11} {a['url']} | {a['location']}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "result.json"))
