"""
소식 묶음 — 공시 목록에 (1) 8-K 본문 요약(한글 번역)과 (2) 뉴스 제목(Yahoo Finance RSS, 한글 번역)을 붙인다.

- 요약: 8-K 원문에서 표지(회사 주소·체크박스)를 건너뛰고 'Item x.xx' 본문 앞부분만 뽑아 번역한다. 기계 번역이라 어색할 수 있다.
- 번역: 구글 번역 공개 주소(키 없음). 실패하면 영어 원문을 그대로 둔다.
- 한 번 만든 요약·번역은 이전 저장분(prev)에서 다시 쓴다(공시 원문·번역을 매일 다시 받지 않음).
"""
from __future__ import annotations
import html
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

import filings as filings_mod
from edgar import _get

log = logging.getLogger("desk")
UA = {"User-Agent": "Mozilla/5.0"}
MAX_SUM = 10          # 종목당 요약할 8-K 수(최신부터)
NEWS_DAYS = 14
NEWS_MAX = 12
_tr_cache: dict[str, str] = {}


def tr(text: str) -> str:
    """영어 → 한국어. 실패하면 빈 문자열."""
    text = (text or "").strip()
    if not text:
        return ""
    if text in _tr_cache:
        return _tr_cache[text]
    out = ""
    try:
        r = requests.get("https://translate.googleapis.com/translate_a/single",
                         params={"client": "gtx", "sl": "en", "tl": "ko", "dt": "t", "q": text}, headers=UA, timeout=15)
        if r.status_code == 200:
            out = "".join(seg[0] for seg in r.json()[0] if seg and seg[0]).strip()
        time.sleep(0.15)
    except Exception as e:
        log.warning(f"번역 실패: {e}")
    _tr_cache[text] = out
    return out


def _plain(doc: str) -> str:
    doc = re.sub(r"(?is)<(script|style|ix:header)[^>]*>.*?</\1>", " ", doc)
    doc = re.sub(r"(?s)<[^>]+>", " ", doc)
    return re.sub(r"\s+", " ", html.unescape(doc).replace("\xa0", " ")).strip()


def body_8k(doc_html: str, limit: int = 520) -> str:
    """8-K 원문에서 항목 본문 앞부분(영어)만. 표지·첨부 목록·서명·면책 문구는 뺀다."""
    t = _plain(doc_html)
    ms = list(re.finditer(r"\bItem\s+(\d\.\d\d)\.?", t))
    if not ms:
        return ""
    parts = []
    for i, m in enumerate(ms):
        if m.group(1) == "9.01":
            continue
        end = ms[i + 1].start() if i + 1 < len(ms) else len(t)
        seg = t[m.end():end]
        for stop in ("SIGNATURE", "Forward-Looking Statements", "Forward Looking Statements", "Cautionary Note", "Cautionary Statement"):
            k = seg.find(stop)
            if k > 60:
                seg = seg[:k]
        seg = seg.strip(" .:-–—")
        if len(seg) > 40:
            parts.append(seg)
    s = " ".join(parts)
    if len(s) > limit:
        cut = s[:limit]
        k = max(cut.rfind(". "), cut.rfind("; "))
        s = cut[:k + 1] if k > limit * 0.5 else cut.rstrip() + "…"
    return s


def add_summaries(rows: list[dict], prev: dict[str, dict]) -> None:
    n = 0
    for r in rows:
        old = prev.get(r["url"])
        if old and "sum" in old:
            r["sum"] = old["sum"]
            continue
        if not (r["form"].upper().startswith("8-K") and r["imp"]) or n >= MAX_SUM or not r["url"].lower().endswith((".htm", ".html")):
            continue
        n += 1
        try:
            en = body_8k(_get(r["url"]).text)
            r["sum"] = (tr(en) or en) if en else ""
        except Exception as e:
            log.warning(f"요약 실패 {r['url']}: {e}")


def news(symbol: str, prev: dict[str, dict]) -> list[dict]:
    r = requests.get("https://feeds.finance.yahoo.com/rss/2.0/headline",
                     params={"s": symbol, "region": "US", "lang": "en-US"}, headers=UA, timeout=20)
    r.raise_for_status()
    cutoff = datetime.now(timezone.utc) - timedelta(days=NEWS_DAYS)
    out, seen = [], set()
    for it in ET.fromstring(r.content).iter("item"):
        title = (it.findtext("title") or "").strip()
        url = (it.findtext("link") or "").split("?")[0].strip()
        try:
            dt = parsedate_to_datetime(it.findtext("pubDate") or "")
        except Exception:
            continue
        if not title or not url.startswith("https://") or dt < cutoff or title in seen:
            continue
        seen.add(title)
        old = prev.get(url)
        ko = (old or {}).get("title") if old and old.get("en") == title else tr(title)
        out.append({"date": dt.strftime("%Y-%m-%d"), "form": "뉴스", "kind": "뉴스", "title": ko or title, "en": title,
                    "imp": 0, "url": url, "items": ""})
    out.sort(key=lambda x: x["date"], reverse=True)
    return out[:NEWS_MAX]


def feed(symbol: str, cik: str | None, prev_rows: list[dict] | None) -> list[dict]:
    """한 종목의 소식 목록 = 공시(요약 포함) + 뉴스 제목."""
    prev = {r.get("url"): r for r in (prev_rows or []) if r.get("url")}
    rows = filings_mod.filings(cik) if cik else []
    add_summaries(rows, prev)
    try:
        rows += news(symbol, prev)
    except Exception as e:
        log.warning(f"{symbol} 뉴스 실패: {e}")
        rows += [r for r in (prev_rows or []) if r.get("kind") == "뉴스"]     # 실패하면 이전 뉴스 유지
    return rows
