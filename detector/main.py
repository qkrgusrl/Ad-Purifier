"""불법광고 탐지기 (프로토타입).

사용: python -m detector.main <진입URL> [--out 폴더] [--max-pages 200] [--model 경로] [--no-ml]
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

from playwright.async_api import async_playwright

from . import adscore
from .textnorm import homoglyph_signals, jamo_signal, normalize

TOOL_VERSION = "0.1.0-proto"
TOPIC = "불법광고"  # 요강 예시는 "TOPIC" 문자열 — 주최 측 확인 필요
KST = timezone(timedelta(hours=9))
EXTRACT_JS = (Path(__file__).parent / "extract.js").read_text(encoding="utf-8")
SKIP_EXT = (".pdf", ".hwp", ".hwpx", ".doc", ".docx", ".xls", ".xlsx", ".zip", ".jpg", ".jpeg", ".png", ".gif", ".mp4")


def norm_url(url):
    """채점 규칙과 같은 기준: http/https·끝 슬래시·# 무시, 쿼리 순서 무시."""
    p = urlparse(url)
    query = urlencode(sorted(parse_qsl(p.query, keep_blank_values=True)))
    return urlunparse(("", p.netloc.lower(), p.path.rstrip("/") or "/", "", query, ""))


def strip_fragment(url):
    return urlunparse(urlparse(url)._replace(fragment=""))


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


async def scan_page(ctx, url, timeout_ms):
    page = await ctx.new_page()
    try:
        try:
            await page.goto(url, wait_until="networkidle", timeout=timeout_ms)
        except Exception:
            await page.wait_for_timeout(500)  # networkidle 실패 시 현재 DOM으로 진행
        frames = []
        for fr in page.frames:
            try:
                data = await fr.evaluate(EXTRACT_JS)
                data["frame_url"] = fr.url
                frames.append((await frame_prefix(fr), data))
            except Exception:
                continue
        return strip_fragment(page.url), frames
    finally:
        await page.close()


async def crawl(entry, max_pages, concurrency, timeout_ms):
    host = urlparse(entry).netloc
    seen, queue, results = {norm_url(entry)}, asyncio.Queue(), []
    frame_urls = set()  # iframe으로 삽입된 문서: 상위 페이지 기준으로 보고하므로 단독 페이지로 크롤링하지 않음
    queue.put_nowait(entry)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(viewport={"width": 1280, "height": 900}, ignore_https_errors=True)

        async def worker():
            while True:
                url = await queue.get()
                try:
                    final_url, frames = await scan_page(ctx, url, timeout_ms)
                    results.append((final_url, frames))
                    frame_urls.update(norm_url(d["frame_url"]) for pre, d in frames if pre)
                    for _, data in frames:
                        for link in data["links"]:
                            p = urlparse(link)
                            if p.scheme not in ("http", "https") or p.netloc != host:
                                continue
                            if p.path.lower().endswith(SKIP_EXT):
                                continue
                            key = norm_url(link)
                            if key in frame_urls:
                                continue
                            if key not in seen and len(seen) < max_pages:
                                seen.add(key)
                                queue.put_nowait(strip_fragment(link))
                except Exception as e:  # 한 페이지 실패가 전체를 멈추지 않게
                    results.append((url, []))
                    print(f"[warn] {url}: {e}")
                finally:
                    queue.task_done()

        workers = [asyncio.create_task(worker()) for _ in range(concurrency)]
        await queue.join()
        for w in workers:
            w.cancel()
        await browser.close()
    # 순서상 iframe 문서가 먼저 큐에 들어갔던 경우까지 정리
    return [(u, f) for u, f in results if norm_url(u) not in frame_urls]


def candidates_from(pages):
    """페이지 스캔 결과 → (url, location, technique) 후보."""
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
    return list(cands.values())


def judge(cands, model_path, use_ml):
    """룰 점수로 1차 판정, 경계 구간만 Laya로 2차 판정."""
    for c in cands:
        c["normalized"] = normalize(c["evidence_text"])
        c["score"], c["hits"] = adscore.score(c["normalized"])
        c["verdict"] = adscore.verdict(c["score"])
        c["decided_by"] = "rules"
    border = [c for c in cands if c["verdict"] == "borderline"]
    if border and use_ml:
        from laya.agent import Agent

        from training.make_dataset import QUESTION
        agent = Agent(model_path, device="cpu")
        texts = sorted({c["normalized"] for c in border})
        out = agent.predict_batch(texts, QUESTION, batch_size=16)
        prob = {t: o["answers"]["is_illegal_ad"]["noul"] for t, o in zip(texts, out)}
        for c in border:
            c["ml_prob"] = round(prob[c["normalized"]], 4)
            c["verdict"] = "ad" if c["ml_prob"] >= 0.5 else "clean"
            c["decided_by"] = "laya"
    return cands


def main():
    # Windows 콘솔(cp949)에서 한글·특수문자 출력 시 UnicodeEncodeError 방지
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("entry_url")
    ap.add_argument("--out", default=".")
    ap.add_argument("--max-pages", type=int, default=200)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--timeout", type=int, default=15000)
    ap.add_argument("--model", default=str(Path(__file__).resolve().parents[1] / "models" / "laya-illegal-ad"))
    ap.add_argument("--no-ml", action="store_true", help="경계 구간을 Laya 없이 정상으로 처리")
    args = ap.parse_args()

    started = datetime.now(KST)
    t0 = time.perf_counter()
    pages = asyncio.run(crawl(args.entry_url, args.max_pages, args.concurrency, args.timeout))
    t_crawl = time.perf_counter() - t0
    cands = judge(candidates_from(pages), args.model, not args.no_ml)
    finished = datetime.now(KST)
    elapsed = round(time.perf_counter() - t0, 2)

    hits = sorted((c for c in cands if c["verdict"] == "ad"), key=lambda c: (c["url"], c["location"], c["technique"]))
    meta = {"topic": TOPIC, "entry_url": args.entry_url, "started_at": started.isoformat(timespec="seconds"),
            "finished_at": finished.isoformat(timespec="seconds"), "elapsed_sec": elapsed, "tool_version": TOOL_VERSION}
    findings = [{"id": f"f_{i:03d}", "url": c["url"], "is_violation": True, "location": c["location"],
                 "evidence_text": c["evidence_text"], "technique": c["technique"]} for i, c in enumerate(hits, 1)]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "result.json").write_text(json.dumps({"meta": meta, "findings": findings}, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
    (out / "report.json").write_text(json.dumps({"meta": meta, "pages": len(pages), "crawl_sec": round(t_crawl, 2),
                                                 "candidates": cands}, ensure_ascii=False, indent=2), encoding="utf-8")
    by = {}
    for c in cands:
        by.setdefault((c["decided_by"], c["verdict"]), 0)
        by[(c["decided_by"], c["verdict"])] += 1
    print(f"pages={len(pages)} candidates={len(cands)} findings={len(findings)} elapsed={elapsed}s (crawl {t_crawl:.1f}s)")
    print("판정 분포:", {f"{k[0]}/{k[1]}": v for k, v in sorted(by.items())})


if __name__ == "__main__":
    main()
