"""
중요 공시 목록 — SEC EDGAR submissions 의 최근 제출 목록에서 투자 판단에 쓰는 것만 골라 한글 꼬리표를 붙인다.

대상: 8-K(항목 번호로 분류)·10-Q/10-K·제출 지연(NT)·대량보유(13D/13G)·증자/사채 신고(S-1/S-3/424B)·상장폐지(25/15).
결과 1건 = {"date", "form", "kind", "title", "imp"(1 중요/0 참고), "url", "items"}.
"""
from __future__ import annotations
from datetime import date, timedelta

from edgar import _get   # SEC 요청(간격·헤더)은 기존 것을 그대로 쓴다

# 8-K 항목 → (꼬리표, 설명, 중요)
ITEMS_8K = {
    "1.01": ("계약", "중요 계약 체결", 1), "1.02": ("계약", "중요 계약 해지", 1), "1.03": ("부도", "파산·법정관리", 1),
    "1.05": ("사고", "사이버 보안 사고", 1),
    "2.01": ("인수매각", "자산 인수·매각 완료", 1), "2.02": ("실적", "실적 발표", 1), "2.03": ("차입", "대규모 차입·채무 발생", 1),
    "2.04": ("차입", "채무 조기상환 사유 발생", 1), "2.05": ("구조조정", "구조조정·사업 철수 비용", 1), "2.06": ("손상", "자산 손상(상각)", 1),
    "3.01": ("상장", "상장 유지 요건 미달 통지", 1), "3.02": ("증자", "미등록 주식 발행(사모)", 1), "3.03": ("주주권", "주주 권리 변경", 1),
    "4.01": ("회계", "감사인 교체", 1), "4.02": ("회계", "과거 재무제표 신뢰 불가(정정)", 1),
    "5.01": ("지배", "경영권 변동", 1), "5.02": ("임원", "임원·이사 선임·사임·보수", 1), "5.03": ("정관", "정관·회계연도 변경", 0),
    "5.07": ("주총", "주주총회 표결 결과", 0), "7.01": ("기타", "공정공시(자료 공개)", 0), "8.01": ("기타", "기타 사항", 0),
    "9.01": ("기타", "첨부 자료", 0),
}
# 그 밖의 양식 → (꼬리표, 설명, 중요). 앞부분 일치로 찾는다(긴 것부터)
FORMS = [
    ("NT 10-K", ("지연", "연간보고서 제출 지연", 1)), ("NT 10-Q", ("지연", "분기보고서 제출 지연", 1)),
    ("10-K", ("보고서", "연간보고서(10-K)", 1)), ("10-Q", ("보고서", "분기보고서(10-Q)", 0)),
    ("SCHEDULE 13D", ("대량보유", "5% 이상 대량보유(경영참여 가능)", 1)), ("SC 13D", ("대량보유", "5% 이상 대량보유(경영참여 가능)", 1)),
    ("SCHEDULE 13G", ("대량보유", "5% 이상 대량보유(단순투자)", 0)), ("SC 13G", ("대량보유", "5% 이상 대량보유(단순투자)", 0)),
    ("S-1", ("증자", "증권 발행 신고(S-1)", 1)), ("S-3", ("증자", "증권 발행 일괄신고(S-3)", 1)),
    ("424B", ("증자", "증권 발행 투자설명서(증자·사채)", 1)),
    ("SC TO", ("공개매수", "공개매수", 1)), ("SC 14D9", ("공개매수", "공개매수 의견", 1)),
    ("DEFM14", ("합병", "합병 주주총회 소집", 1)), ("25", ("상장", "상장폐지 신고", 1)), ("15-", ("상장", "등록 종료(공시 의무 해제)", 1)),
]


def _classify(form: str, items: str) -> tuple[str, str, int] | None:
    f = (form or "").upper().strip()
    if f.startswith("8-K"):
        codes = [c.strip() for c in (items or "").split(",") if c.strip()]
        hits = [ITEMS_8K[c] for c in codes if c in ITEMS_8K]
        if not hits:
            return ("기타", "수시공시(8-K)", 0)
        major = [h for h in hits if h[2]] or hits
        title = " · ".join(dict.fromkeys(h[1] for h in major))          # 중요 항목만 이어 붙임(중복 제거)
        return (major[0][0], title + (" (정정)" if f.endswith("/A") else ""), 1 if major[0][2] else 0)
    for prefix, v in FORMS:
        if f == prefix or f.startswith(prefix):
            if prefix in ("25", "15-") and not (f == "25" or f.startswith("25-") or f.startswith("15-")):
                continue
            return (v[0], v[1] + (" (정정)" if f.endswith("/A") else ""), v[2])
    return None


def filings(cik: str, months: int = 6, limit: int = 60, scan: int = 400) -> list[dict]:
    sub = _get(f"https://data.sec.gov/submissions/CIK{cik}.json").json()
    rec = sub.get("filings", {}).get("recent", {})
    cutoff = (date.today() - timedelta(days=int(30.4 * months))).isoformat()
    cik_int = str(int(cik))
    out: list[dict] = []
    # recent(최근 1000건)로 6개월이 안 채워지는 회사(은행: 채권 설명서가 하루 수십 건)는 이전 묶음도 2개까지 더 읽는다
    pages = [rec]
    for extra in (sub.get("filings", {}).get("files") or [])[:2]:
        dates = pages[-1].get("filingDate") or []
        if not dates or dates[-1] < cutoff:
            break
        try:
            pages.append(_get(f"https://data.sec.gov/submissions/{extra['name']}").json())
        except Exception:
            break
    for page in pages:
        cols = [page.get(k, []) for k in ("form", "filingDate", "accessionNumber", "primaryDocument", "items")]
        for i in range(len(cols[0])):
            form, dt, acc, doc = cols[0][i], cols[1][i], cols[2][i], (cols[3][i] if i < len(cols[3]) else "")
            items = cols[4][i] if i < len(cols[4]) else ""
            if dt < cutoff:
                break                                # 최신순
            c = _classify(form, items)
            if not c:
                continue
            acc_nodash = acc.replace("-", "")
            url = (f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/{doc}" if doc
                   else f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/")
            out.append({"date": dt, "form": form, "kind": c[0], "title": c[1], "imp": c[2], "url": url, "items": items})
    # 금융사 잡음 제거: 은행은 구조화 채권 설명서(424B)를 하루에도 여러 건 내고, 운용사는 '남의 회사' 대량보유 보고(13G/13D)를
    # 수십 건 낸다(자기 회사 소식이 아님). 창 안에 같은 종류가 많으면 통째로 뺀다.
    n424 = sum(1 for r in out if r["form"].upper().startswith("424B"))
    n13 = sum(1 for r in out if "13G" in r["form"].upper() or "13D" in r["form"].upper())
    if n424 > 8:
        out = [r for r in out if not r["form"].upper().startswith("424B") and not r["form"].upper().startswith("FWP")]
    if n13 > 10:
        out = [r for r in out if "13G" not in r["form"].upper() and "13D" not in r["form"].upper()]
    return out[:limit]
