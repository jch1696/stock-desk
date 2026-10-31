"""
stock-desk 수집기 — 보유/관심 종목의 시세(Yahoo)·재무/주식수/내부자(EDGAR)를 받아 Supabase 에 올린다.

  python collector/collect.py                 # 전 종목
  python collector/collect.py --symbols NVDA,AAPL
  python collector/collect.py --no-insider    # Form 4 생략(빠름)

환경변수(또는 .env): SUPABASE_URL, SUPABASE_KEY(anon), SUPABASE_EMAIL, SUPABASE_PASSWORD
→ 사용자 계정으로 로그인해 desk_* 표에 upsert (RLS: authenticated 쓰기 허용).
"""
from __future__ import annotations
import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import edgar  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("desk")


def load_env():
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.strip().split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


class SB:
    def __init__(self):
        self.url = os.environ["SUPABASE_URL"].rstrip("/")
        self.key = os.environ["SUPABASE_KEY"]
        r = requests.post(f"{self.url}/auth/v1/token?grant_type=password",
                          headers={"apikey": self.key, "Content-Type": "application/json"},
                          json={"email": os.environ["SUPABASE_EMAIL"], "password": os.environ["SUPABASE_PASSWORD"]}, timeout=15)
        r.raise_for_status()
        self.token = r.json()["access_token"]

    def h(self, extra=None):
        d = {"apikey": self.key, "Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        d.update(extra or {}); return d

    def get(self, path):
        r = requests.get(f"{self.url}/rest/v1/{path}", headers=self.h(), timeout=20); r.raise_for_status(); return r.json()

    def upsert(self, table, rows, on_conflict):
        r = requests.post(f"{self.url}/rest/v1/{table}?on_conflict={on_conflict}", headers=self.h({"Prefer": "resolution=merge-duplicates,return=minimal"}),
                          json=rows, timeout=30)
        if r.status_code not in (200, 201, 204):
            log.warning(f"{table} upsert {r.status_code}: {r.text[:200]}")
        return r.status_code in (200, 201, 204)


def yahoo(symbol: str) -> dict:
    r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=1y&interval=1d",
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    # 분할 이벤트(5년) — EDGAR 주식수 이력은 분할 미조정이라 보정에 쓴다
    splits = []
    try:
        r2 = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=5y&interval=1mo&events=splits",
                          headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        ev = (r2.json()["chart"]["result"][0].get("events") or {}).get("splits") or {}
        for v in ev.values():
            d = datetime.fromtimestamp(int(v["date"]), timezone.utc).strftime("%Y-%m-%d")
            ratio = float(v["numerator"]) / float(v["denominator"])
            if ratio > 0:
                splits.append({"date": d, "ratio": ratio})
    except Exception:
        pass
    q = res["indicators"]["quote"][0]
    closes = [{"d": datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d"), "c": round(float(c), 2)}
              for t, c in zip(res["timestamp"], q["close"]) if c is not None]
    meta = res.get("meta", {})
    return {"price": float(meta.get("regularMarketPrice") or closes[-1]["c"]), "closes": closes,
            "name": meta.get("longName") or meta.get("shortName") or "", "splits": sorted(splits, key=lambda x: x["date"])}


def collect(symbol: str, with_insider: bool = True) -> dict:
    sym = symbol.upper().strip()
    y = yahoo(sym)
    cik = edgar.cik_of(sym)
    out = {"symbol": sym, "price": y["price"], "closes": y["closes"], "name": y["name"] or edgar.name_of(sym), "cik": cik}
    if cik:
        try:
            out["fund"] = edgar.fundamentals(cik, y["price"], splits=y.get("splits") or [])
            if y.get("splits"):
                out["fund"]["splits"] = y["splits"]
            out["name"] = out["name"] or out["fund"].get("name", "")
        except Exception as e:
            log.warning(f"{sym} 재무 실패: {e}")
        if with_insider:
            try:
                out["insider"] = edgar.insider(cik)
            except Exception as e:
                log.warning(f"{sym} 내부자 실패: {e}")
    else:
        log.warning(f"{sym}: EDGAR CIK 없음(ETF·해외 ADR 등) — 시세만")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="")
    ap.add_argument("--no-insider", action="store_true")
    ap.add_argument("--dry", action="store_true", help="업로드 없이 출력만")
    a = ap.parse_args()
    load_env()
    sb = None if a.dry else SB()
    if a.symbols:
        syms = [s for s in a.symbols.split(",") if s.strip()]
    else:
        rows = sb.get("desk_positions?select=symbol") + sb.get("desk_watch?select=symbol")
        syms = sorted({r["symbol"].upper() for r in rows})
    log.info(f"{len(syms)} 종목: {', '.join(syms)}")
    for i, s in enumerate(syms, 1):
        t0 = time.time()
        try:
            d = collect(s, with_insider=not a.no_insider)
        except Exception as e:
            log.error(f"{s} 실패: {e}"); continue
        f = d.get("fund") or {}
        ins = d.get("insider") or {}
        log.info(f"[{i}/{len(syms)}] {s} ${d['price']:.2f} PER {f.get('per') and round(f['per'],1)} PBR {f.get('pbr') and round(f['pbr'],2)} "
                 f"PEG {f.get('peg') and round(f['peg'],2)} ROE {f.get('roe') and round(f['roe']*100,1)}% 주식수1y {f.get('shares_chg_1y') and round(f['shares_chg_1y']*100,1)}% "
                 f"내부자매수6m {ins.get('buy_6m',{}).get('n')}건 ({time.time()-t0:.1f}s)")
        if a.dry:
            print(json.dumps({k: v for k, v in d.items() if k != "closes"}, ensure_ascii=False, default=str)[:1500])
            continue
        now = datetime.now(timezone.utc).isoformat()
        sb.upsert("desk_fundamentals", [{"symbol": s, "asof": now, "name": d["name"], "cik": d["cik"], "price": d["price"], "data": f}], "symbol")
        sb.upsert("desk_prices", [{"symbol": s, "asof": now, "closes": d["closes"]}], "symbol")
        if ins:
            sb.upsert("desk_insider", [{"symbol": s, "asof": now, "data": ins}], "symbol")


if __name__ == "__main__":
    main()
