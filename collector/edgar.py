"""
SEC EDGAR (공식·무료) 에서 기업 재무·주식수·내부자 거래를 받는다.

- 티커→CIK: https://www.sec.gov/files/company_tickers.json
- 재무(XBRL companyfacts): https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json
- 제출목록(Form 4 찾기): https://data.sec.gov/submissions/CIK##########.json
- Form 4 본문(XML): https://www.sec.gov/Archives/edgar/data/<cik>/<accession>/<primaryDoc>
SEC 요청 규칙: User-Agent 에 연락처, 초당 10건 이하.
"""
from __future__ import annotations
import re
import time
import logging
from datetime import date, datetime, timedelta
from typing import Optional
import xml.etree.ElementTree as ET

import requests

logger = logging.getLogger("desk.edgar")
UA = {"User-Agent": "stock-desk/1.0 (jch1696@gmail.com)", "Accept-Encoding": "gzip, deflate"}
_last = [0.0]


def _get(url: str, timeout: float = 20.0):
    # 초당 10건 제한 → 0.12초 간격
    gap = 0.12 - (time.time() - _last[0])
    if gap > 0:
        time.sleep(gap)
    r = requests.get(url, headers=UA, timeout=timeout)
    _last[0] = time.time()
    r.raise_for_status()
    return r


_ticker_map: dict = {}


def cik_of(ticker: str) -> Optional[str]:
    global _ticker_map
    if not _ticker_map:
        d = _get("https://www.sec.gov/files/company_tickers.json").json()
        _ticker_map = {v["ticker"].upper(): (str(v["cik_str"]).zfill(10), v["title"]) for v in d.values()}
    t = _ticker_map.get(ticker.upper().replace("-", "."))
    return t[0] if t else None


def name_of(ticker: str) -> str:
    cik_of(ticker)
    t = _ticker_map.get(ticker.upper().replace("-", "."))
    return t[1] if t else ""


# ── 재무 ──
def _series(facts: dict, tag: str, unit_pref=("USD", "USD/shares", "shares")) -> list[dict]:
    """companyfacts 의 us-gaap/dei 태그 → [{end, val, fp, fy, form, start}] (10-Q/10-K 만, 중복 제거, end 순)."""
    for ns in ("us-gaap", "dei"):
        node = ((facts.get("facts") or {}).get(ns) or {}).get(tag)
        if not node:
            continue
        units = node.get("units") or {}
        for u in unit_pref:
            if u in units:
                rows = [r for r in units[u] if r.get("form") in ("10-Q", "10-K", "10-K/A", "10-Q/A", "20-F", "40-F")]
                seen, out = set(), []
                for r in sorted(rows, key=lambda r: (r.get("end", ""), r.get("filed", ""))):
                    key = (r.get("end"), r.get("start"), r.get("fp"))
                    if key in seen:
                        continue
                    seen.add(key); out.append(r)
                return out
    return []


def _quarterly(rows: list[dict]) -> list[dict]:
    """기간(start~end)이 분기(70~100일)인 값만 — 손익계산서 항목용. 10-K 의 연간값은 앞 3분기를 빼서 Q4 로 만든다."""
    def days(r):
        try:
            return (date.fromisoformat(r["end"]) - date.fromisoformat(r["start"])).days
        except Exception:
            return 0
    q = [r for r in rows if r.get("start") and 70 <= days(r) <= 100]
    annual = [r for r in rows if r.get("start") and 350 <= days(r) <= 380]
    by_end = {r["end"]: r for r in q}
    for a in annual:
        if a["end"] in by_end:
            continue
        # 같은 회계연도 앞 3분기 합계를 빼서 Q4 추정
        end = date.fromisoformat(a["end"])
        prev = [r for r in q if date.fromisoformat(a["start"]) <= date.fromisoformat(r["end"]) < end]
        if len(prev) == 3:
            by_end[a["end"]] = {"end": a["end"], "start": prev[-1]["end"], "val": a["val"] - sum(p["val"] for p in prev),
                                "fp": "Q4", "fy": a.get("fy"), "form": a.get("form")}
    return sorted(by_end.values(), key=lambda r: r["end"])


def _merge_quarterly(series_list: list[list[dict]]) -> list[dict]:
    """여러 태그의 분기 계열을 end 기준으로 합친다(뒤에 오는 태그가 우선). 이상치(이웃 분기의 1/5 미만·5배 초과)는 버림."""
    by: dict = {}
    for ser in series_list:
        for r in ser or []:
            by[r["end"]] = r
    rows = [by[k] for k in sorted(by)]
    return rows


def _ttm(qs: list[dict]) -> Optional[float]:
    if len(qs) < 4:
        return None
    return float(sum(r["val"] for r in qs[-4:]))


def _consecutive_tail(qs: list[dict]) -> list[dict]:
    """끝에서부터 분기가 연속(end 간격 80~100일)인 구간만 — 신규 상장은 공시된 분기가 띄엄띄엄이라 그냥 더하면 안 됨."""
    tail = qs[-1:]
    for i in range(len(qs) - 2, -1, -1):
        gap = (date.fromisoformat(tail[0]["end"]) - date.fromisoformat(qs[i]["end"])).days
        if 80 <= gap <= 100:
            tail.insert(0, qs[i])
        else:
            break
    return tail


def _ttm_or_annualized(qs: list[dict]) -> tuple[Optional[float], str]:
    """연속된 최근 4분기 합(TTM). 4분기가 안 되면(신규 상장 등) 연속 구간 평균×4(연환산)과 표시용 꼬리표."""
    if not qs:
        return None, ""
    tail = _consecutive_tail(qs)
    if len(tail) >= 4:
        return float(sum(r["val"] for r in tail[-4:])), "TTM"
    return float(sum(r["val"] for r in tail)) * 4.0 / len(tail), f"연환산({len(tail)}분기)"


def _instant(rows: list[dict]) -> list[dict]:
    """시점값(재무상태표·주식수): start 없는 것만. 같은 end 가 여러 보고서에 반복되면 마지막 것 하나만."""
    by = {}
    for r in sorted([r for r in rows if not r.get("start")], key=lambda r: (r["end"], r.get("filed", ""))):
        by[r["end"]] = r
    return [by[k] for k in sorted(by)]


def _split_adjust(rows: list[dict], splits: list[dict]) -> list[dict]:
    """분할(예: 10:1) 전 날짜의 주식수에 비율을 곱해 지금 기준으로 맞춘다."""
    if not splits:
        return rows
    out = []
    for r in rows:
        f = 1.0
        for sp in splits:
            if r["end"] < sp["date"]:
                f *= sp["ratio"]
        out.append(dict(r, val=r["val"] * f))
    return out


def fundamentals(cik: str, price: Optional[float], splits: Optional[list] = None) -> dict:
    """PER·PBR·ROE·PEG·주식수 변화·자사주 매입 등. 모두 EDGAR 값 + 현재가로 계산."""
    facts = _get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json").json()
    out: dict = {"name": facts.get("entityName", "")}
    eps_q = _quarterly(_series(facts, "EarningsPerShareDiluted", ("USD/shares",)) or _series(facts, "EarningsPerShareBasic", ("USD/shares",)))
    ni_q = _quarterly(_series(facts, "NetIncomeLoss", ("USD",)))
    # 매출 태그는 회사마다·시기마다 다르다(MSFT: 2018 까지 Revenues, 이후 RevenueFromContractWithCustomer…) → 태그별 분기 계열을
    # 모두 모아 같은 분기(end)는 더 최신 보고 태그 값을 쓰고, 날짜순으로 합친다
    rev_q = _merge_quarterly([_quarterly(_series(facts, t, ("USD",))) for t in
                              ("SalesRevenueNet", "Revenues", "RevenueFromContractWithCustomerIncludingAssessedTax",
                               "RevenueFromContractWithCustomerExcludingAssessedTax")])
    buyback_q = _quarterly(_series(facts, "PaymentsForRepurchaseOfCommonStock", ("USD",)))
    equity = _instant(_series(facts, "StockholdersEquity", ("USD",)) or _series(facts, "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest", ("USD",)))
    # 주식수: 표지(dei) → 재무상태표 발행주식 → 분기 희석/기본 가중평균 순으로, '최신값이 0보다 큰' 첫 계열을 쓴다
    #   (HOOD 는 CommonStockSharesOutstanding 이 0 으로 보고됨, TEM·신규상장은 표지 주식수가 없음)
    shares = []
    #   표지 주식수가 없으면 '전체' 가중평균(희석)을 재무상태표 발행주식(클래스별로 쪼개져 있을 수 있음, HOOD)보다 먼저 쓴다
    for tag, kind in (("EntityCommonStockSharesOutstanding", "i"), ("WeightedAverageNumberOfDilutedSharesOutstanding", "q"),
                      ("WeightedAverageNumberOfSharesOutstandingBasic", "q"), ("CommonStockSharesOutstanding", "i"), ("CommonStockSharesIssued", "i")):
        rows = _series(facts, tag, ("shares",))
        if not rows:
            continue
        if kind == "i":
            cand = [r for r in _instant(rows) if float(r["val"]) > 0]
        else:
            by = {}
            for r in _quarterly(rows):
                if float(r["val"]) > 0:
                    by[r["end"]] = {"end": r["end"], "val": r["val"]}
            cand = [by[k] for k in sorted(by)]
        if cand:
            shares = _split_adjust(cand, splits or [])
            out["shares_tag"] = tag
            break
    out["eps_q"] = [{"end": r["end"], "val": round(float(r["val"]), 3)} for r in eps_q[-12:]]
    out["rev_q"] = [{"end": r["end"], "val": float(r["val"])} for r in rev_q[-12:]]
    out["ni_q"] = [{"end": r["end"], "val": float(r["val"])} for r in ni_q[-12:]]
    out["shares_hist"] = [{"end": r["end"], "val": float(r["val"])} for r in shares[-16:]]
    out["equity_hist"] = [{"end": r["end"], "val": float(r["val"])} for r in equity[-8:]]
    ni_ttm, basis = _ttm_or_annualized(ni_q); rev_ttm, _ = _ttm_or_annualized(rev_q); bb_ttm = _ttm(buyback_q)
    out["basis"] = basis
    sh_now = shares[-1]["val"] if shares else None
    _eq_now = equity[-1]["val"] if equity else None
    if sh_now and price and _eq_now and _eq_now > 0 and sh_now < 2e7 and (price * sh_now) / _eq_now < 0.5:
        shares = [dict(r, val=r["val"] * 1000) for r in shares]          # TEM 처럼 천 주 단위로 올라온 경우
        sh_now = shares[-1]["val"]
        out["shares_scaled"] = 1000
    # EPS 는 XBRL 값이 분할 미조정(NVDA 10:1 등)이라 비교가 안 됨 → 순이익 ÷ 현재 주식수로 통일(분할 영향 없음)
    eps_ttm = (ni_ttm / sh_now) if (ni_ttm is not None and sh_now) else None
    out["eps_ttm"] = round(eps_ttm, 3) if eps_ttm is not None else None
    out["eps_q"] = [{"end": r["end"], "val": round(float(r["val"]) / sh_now, 3)} for r in ni_q[-12:]] if sh_now else out["eps_q"]
    eps_q = [{"end": r["end"], "val": float(r["val"]) / sh_now, "start": r.get("start")} for r in ni_q] if sh_now else eps_q
    out["ni_ttm"] = ni_ttm; out["rev_ttm"] = rev_ttm; out["buyback_ttm"] = bb_ttm
    out["buyback_3y"] = float(sum(r["val"] for r in buyback_q[-12:])) if len(buyback_q) >= 4 else None
    # 성장률(EPS ttm 전년 대비, 3년 CAGR)
    def ttm_at(qs, back):
        seg = qs[:len(qs) - back] if back else qs
        tail = _consecutive_tail(seg) if seg else []
        return _ttm(tail) if len(tail) >= 4 else None
    e0, e1, e3 = eps_ttm, ttm_at(eps_q, 4), ttm_at(eps_q, 12)
    out["eps_growth_1y"] = (e0 / e1 - 1) if (e0 is not None and e1 and e1 > 0 and e0 > 0) else None
    out["eps_cagr_3y"] = ((e0 / e3) ** (1 / 3) - 1) if (e0 is not None and e3 and e3 > 0 and e0 > 0) else None
    r0, r1 = rev_ttm, ttm_at(rev_q, 4)
    out["rev_growth_1y"] = (r0 / r1 - 1) if (r0 and r1) else None
    # 수익성
    eq_now = equity[-1]["val"] if equity else None
    eq_prev = equity[-5]["val"] if len(equity) >= 5 else (equity[0]["val"] if equity else None)
    out["roe"] = (ni_ttm / ((eq_now + eq_prev) / 2)) if (ni_ttm is not None and eq_now and eq_prev and (eq_now + eq_prev) > 0) else None
    out["net_margin"] = (ni_ttm / rev_ttm) if (ni_ttm is not None and rev_ttm) else None
    out["shares_out"] = sh_now
    def sh_at(months):
        if not shares:
            return None
        cutoff = (date.fromisoformat(shares[-1]["end"]) - timedelta(days=int(30.4 * months))).isoformat()
        cands = [r for r in shares if r["end"] <= cutoff]
        return cands[-1]["val"] if cands else None
    s1, s3 = sh_at(12), sh_at(36)
    out["shares_chg_1y"] = (sh_now / s1 - 1) if (sh_now and s1) else None
    out["shares_chg_3y"] = (sh_now / s3 - 1) if (sh_now and s3) else None
    # 가격 배수
    if price and price > 0:
        out["per"] = (price / eps_ttm) if (eps_ttm and eps_ttm > 0) else None
        out["pbr"] = (price * sh_now / eq_now) if (sh_now and eq_now and eq_now > 0) else None
        g = out["eps_cagr_3y"] if out["eps_cagr_3y"] is not None else out["eps_growth_1y"]
        out["peg"] = (out["per"] / (g * 100)) if (out.get("per") and g and g > 0) else None
        out["peg_basis"] = "3y CAGR" if out["eps_cagr_3y"] is not None else ("1y" if out["eps_growth_1y"] is not None else "")
        out["mcap"] = price * sh_now if sh_now else None
        out["buyback_yield"] = (bb_ttm / out["mcap"]) if (bb_ttm and out.get("mcap")) else None
    out["last_report"] = eps_q[-1]["end"] if eps_q else (shares[-1]["end"] if shares else None)
    return out


# ── 내부자 거래 (Form 4) ──
def insider(cik: str, months: int = 12, max_filings: int = 60) -> dict:
    sub = _get(f"https://data.sec.gov/submissions/CIK{cik}.json").json()
    rec = sub.get("filings", {}).get("recent", {})
    forms, accs, docs, dates = rec.get("form", []), rec.get("accessionNumber", []), rec.get("primaryDocument", []), rec.get("filingDate", [])
    cutoff = (date.today() - timedelta(days=int(30.4 * months))).isoformat()
    picks = [(a, d, f) for f, a, d, dt in zip(forms, accs, docs, dates) if f in ("4", "4/A") and dt >= cutoff][:max_filings]
    txs: list[dict] = []
    cik_int = str(int(cik))
    for acc, doc, _ in picks:
        acc_nodash = acc.replace("-", "")
        raw_doc = doc.split("/")[-1]                      # 'xslF345X05/wk-form4_1.xml' → 원본 XML 파일명
        url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/{raw_doc}"
        try:
            xml_text = _get(url).text
            # primaryDocument 가 .xml 이 아니면(드묾) 폴더 안의 xml 을 찾지 않고 건너뜀
            if "<ownershipDocument" not in xml_text:
                continue
            txs.extend(_parse_form4(xml_text))
        except Exception as e:
            logger.debug(f"form4 {acc} 실패: {e}")
    txs.sort(key=lambda t: t["date"], reverse=True)
    def agg(days, code):
        c = (date.today() - timedelta(days=days)).isoformat()
        sel = [t for t in txs if t["date"] >= c and t["code"] == code]
        return {"n": len(sel), "shares": sum(t["shares"] for t in sel), "value": sum(t["value"] for t in sel)}
    return {"buy_3m": agg(91, "P"), "buy_6m": agg(182, "P"), "buy_12m": agg(365, "P"),
            "sell_3m": agg(91, "S"), "sell_6m": agg(182, "S"), "sell_12m": agg(365, "S"),
            "recent": txs[:40], "filings": len(picks)}


def _txt(el, path):
    n = el.find(path)
    return (n.text or "").strip() if n is not None and n.text else ""


def _parse_form4(xml_text: str) -> list[dict]:
    root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    owner = _txt(root, ".//reportingOwner/reportingOwnerId/rptOwnerName")
    rel = root.find(".//reportingOwner/reportingOwnerRelationship")
    title = ""
    if rel is not None:
        if _txt(rel, "isDirector") in ("1", "true"):
            title = "이사"
        if _txt(rel, "isOfficer") in ("1", "true"):
            title = _txt(rel, "officerTitle") or "임원"
        if _txt(rel, "isTenPercentOwner") in ("1", "true"):
            title = (title + " · " if title else "") + "10%주주"
    out = []
    for tx in root.findall(".//nonDerivativeTable/nonDerivativeTransaction"):
        code = _txt(tx, "transactionCoding/transactionCode")
        if code not in ("P", "S"):          # P=장내 매수, S=장내 매도 (A=부여, M=옵션행사, F=세금 납부용 처분 등은 제외)
            continue
        try:
            shares = float(_txt(tx, "transactionAmounts/transactionShares/value") or 0)
            price = float(_txt(tx, "transactionAmounts/transactionPricePerShare/value") or 0)
        except ValueError:
            continue
        out.append({"date": _txt(tx, "transactionDate/value"), "owner": owner, "title": title, "code": code,
                    "shares": shares, "price": price, "value": round(shares * price, 2),
                    "after": _txt(tx, "postTransactionAmounts/sharesOwnedFollowingTransaction/value")})
    return out
