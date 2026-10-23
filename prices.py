"""
시세·수급 (네이버 금융 모바일 API, 전일 종가 기준 — 실시간 아님)

  get_quote(code)   : 현재가·등락·시총·PER/PBR·52주 범위·거래정지 여부
  get_daily(code, days) : 일봉 [{date, close, open, high, low, volume}]
  get_flows(code)   : 최근 10거래일 개인/기관/외국인 순매수(주)

주의: 네이버의 비공식 엔드포인트라 형식이 바뀔 수 있음. 실패하면 빈 값으로 두고 리포트는 공시 데이터만으로 계속 생성.
"""
from __future__ import annotations

import time
from datetime import date, timedelta

import requests

H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Referer": "https://m.stock.naver.com/", "Accept": "application/json"}
BASE = "https://m.stock.naver.com/api/stock"
CHART = "https://api.stock.naver.com/chart/domestic/item"
_last = 0.0


def _get(url, **params):
    global _last
    wait = 0.25 - (time.time() - _last)
    if wait > 0:
        time.sleep(wait)
    for attempt in range(3):
        try:
            r = requests.get(url, headers=H, params=params or None, timeout=15)
            _last = time.time()
            if r.status_code == 200:
                return r.json()
            if r.status_code in (404, 410):
                return None
        except (requests.RequestException, ValueError):
            pass
        time.sleep(1.5 * (attempt + 1))
    return None


def _n(v):
    if v is None:
        return None
    s = str(v).replace(",", "").replace("+", "").replace("%", "").replace("배", "").replace("원", "").strip()
    try:
        return float(s) if "." in s else int(s)
    except ValueError:
        return None


def _won_text(s: str | None) -> int | None:
    """'1,252조 5,652억' / '4조 5,326억' / '326억' → 원 단위 정수"""
    if not s:
        return None
    total, cur = 0, ""
    for ch in s.replace(",", "").replace(" ", ""):
        if ch.isdigit():
            cur += ch
        elif ch in "조억만":
            if cur:
                total += int(cur) * {"조": 10**12, "억": 10**8, "만": 10**4}[ch]
                cur = ""
    if cur:
        total += int(cur)
    return total or None


def get_quote(code: str) -> dict:
    b = _get(f"{BASE}/{code}/basic") or {}
    i = _get(f"{BASE}/{code}/integration") or {}
    tot = {x.get("code"): x.get("value") for x in i.get("totalInfos", [])}
    halt = (b.get("tradeStopType") or {}).get("code")
    q = {
        "code": code, "name": b.get("stockName"),
        "market": (b.get("stockExchangeType") or {}).get("nameKor"),
        "close": _n(b.get("closePrice")), "change": _n(b.get("compareToPreviousClosePrice")),
        "change_pct": _n(b.get("fluctuationsRatio")),
        "traded_at": b.get("localTradedAt"),
        "halted": bool(halt and halt != "1"),
        "halt_text": (b.get("tradeStopType") or {}).get("text") if halt and halt != "1" else None,
        "market_cap": _won_text(tot.get("marketValue")),
        "high52": _n(tot.get("highPriceOf52Weeks")), "low52": _n(tot.get("lowPriceOf52Weeks")),
        "per": _n(tot.get("per")), "pbr": _n(tot.get("pbr")), "eps": _n(tot.get("eps")), "bps": _n(tot.get("bps")),
        "div_yield": _n(tot.get("dividendYieldRatio")), "foreign_rate": _n(tot.get("foreignRate")),
        "volume": _n(tot.get("accumulatedTradingVolume")), "value": _won_text(tot.get("accumulatedTradingValue")),
    }
    if q["close"] and q["pbr"] and q["pbr"] < 0:
        q["pbr"] = None
    return q


def get_daily(code: str, days: int = 400) -> list[dict]:
    end = date.today()
    start = end - timedelta(days=days)
    rows = _get(f"{CHART}/{code}/day", startDateTime=f"{start:%Y%m%d}0000", endDateTime=f"{end:%Y%m%d}2359") or []
    out = []
    for r in rows:
        c = r.get("closePrice")
        if not c:
            continue
        out.append({"date": r.get("localDate"), "close": float(c), "open": float(r.get("openPrice") or 0),
                    "high": float(r.get("highPrice") or 0), "low": float(r.get("lowPrice") or 0),
                    "volume": int(r.get("accumulatedTradingVolume") or 0), "foreign": r.get("foreignRetentionRate")})
    return out


def get_flows(code: str) -> list[dict]:
    rows = _get(f"{BASE}/{code}/trend") or []
    out = []
    for r in rows:
        out.append({"date": r.get("bizdate"), "close": _n(r.get("closePrice")), "change": _n(r.get("compareToPreviousClosePrice")),
                    "individual": _n(r.get("individualPureBuyQuant")), "institution": _n(r.get("organPureBuyQuant")),
                    "foreign": _n(r.get("foreignerPureBuyQuant")), "foreign_rate": _n(r.get("foreignerHoldRatio")),
                    "volume": _n(r.get("accumulatedTradingVolume"))})
    return sorted(out, key=lambda x: x["date"] or "")


def close_on(daily: list[dict], yyyymmdd: str) -> float | None:
    """해당 일자(또는 그 이전 가장 가까운 거래일) 종가"""
    best = None
    for r in daily:
        if r["date"] and r["date"] <= yyyymmdd:
            best = r["close"]
        else:
            break
    return best


def summary_flows(flows: list[dict], n: int = 10) -> dict:
    rows = flows[-n:]
    s = {"days": len(rows)}
    for k in ("individual", "institution", "foreign"):
        vals = [r[k] for r in rows if r.get(k) is not None]
        s[k] = sum(vals) if vals else None
        s[k + "_streak"] = _streak([r[k] for r in rows if r.get(k) is not None])
    return s


def _streak(vals: list) -> int:
    """마지막 값 부호 기준 연속 일수 (+: 순매수, -: 순매도)"""
    if not vals:
        return 0
    sign = 1 if vals[-1] > 0 else -1 if vals[-1] < 0 else 0
    n = 0
    for v in reversed(vals):
        if (v > 0 and sign > 0) or (v < 0 and sign < 0):
            n += 1
        else:
            break
    return n * sign


if __name__ == "__main__":
    import sys, json
    c = sys.argv[1] if len(sys.argv) > 1 else "005930"
    print(json.dumps(get_quote(c), ensure_ascii=False, indent=1))
    d = get_daily(c, 30); print("daily", len(d), d[-1] if d else None)
    f = get_flows(c); print("flows", summary_flows(f))
