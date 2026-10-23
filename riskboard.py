"""
종목별 '공시 리스크 보드'

  python riskboard.py 이오플로우                 # 한 종목
  python riskboard.py --watchlist watchlist.txt   # 관심종목 전부
  python riskboard.py --watchlist watchlist.txt --years 3 --out reports/stocks

무엇을 보여주나 (전부 DART 공시·정기보고서 기반 사실, 매수/매도 의견 없음)
  1. 주의 신호 요약  — 아래 수치를 정해진 기준으로 표시 (기준값을 같이 보여줌)
  2. 감사의견 이력   — 최근 3개 사업연도 (정기보고서 정형 API)
  3. 메자닌(CB/BW/EB) — 기간 내 발행 건별 조건, 리픽싱 끝까지 갔을 때 최대 주식수, 회사의 자기사채 취득분
  4. 유상증자        — 건별 신주·희석률·발행가·배정대상자, 기간 누적 신주 / 현재 주식수
  5. 최대주주        — 지분율(최대주주+특수관계인), 변동 이력, 주식담보 현황
  6. 자기주식 / 타법인지분 / 기타 중요 이벤트 타임라인
  7. 실적 (최근 사업보고서 3개년)

데이터 우선순위
  DART 정형 API(cvbdIsDecsn 등) 값이 있으면 그것을 쓰고, 없으면(자율공시·종속회사 공시 등) 우리 파서 값을 쓴다.
  정정공시는 같은 사건(같은 사채 회차 / 같은 이사회결의일)끼리 묶어 최신 정정분만 반영한다.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
    load_dotenv("dart.env")   # 윈도우 탐색기에서 보이는 이름
except ImportError:
    pass

from dart_client import DartClient, DartError, Disclosure, summarize_financials, REPRT_ANNUAL
from analyzer import classify
from parsers import PARSERS, parse_structured, structured_lines, headline, to_int, to_float
from pipeline import load_watchlist
from html_report import write_html, build_index
import quarterly
import industry
import prices

REPRTS_RECENT_FIRST = [("11014", "3분기"), ("11012", "반기"), ("11013", "1분기"), ("11011", "사업")]

# 주의 신호 기준 (표시 규칙일 뿐 투자 판단 기준이 아님 — 리포트에 값과 함께 노출)
TH = {
    "mezz_dilution_warn": 10.0,   # 리픽싱 최대 기준 잠재 주식 / 현재 주식수 (%)
    "mezz_dilution_high": 20.0,
    "rights_cum_high": 30.0,      # 기간 내 유상증자 누적 신주 / 현재 주식수 (%)
    "pledge_high": 50.0,          # 최대주주 보유분 중 담보 비율 (%)
    "major_low": 15.0,            # 최대주주+특수관계인 지분 (%)
    "cb_near_floor": 10.0,        # 현재가가 리픽싱 하한가 대비 +10% 이내면 '하한 근접'
    "pledge_drop": 30.0,          # 담보계약 공시일 종가 대비 현재가 하락률 (%) — 반대매매 위험권 표시 기준
    "rights_discount": 20.0,      # 현재가가 유상증자 발행가 대비 −20% 이하 (청약 전) → 실권 가능성
}


def _num(v) -> int | None:
    return to_int(v) if isinstance(v, str) else (v if isinstance(v, int) else None)


def _flt(v) -> float | None:
    return to_float(v) if isinstance(v, str) else (float(v) if isinstance(v, (int, float)) else None)


def _won(n) -> str:
    if not isinstance(n, int):
        return "-"
    if abs(n) >= 10**12:
        return f"{n / 10**12:,.2f}조"
    if abs(n) >= 10**8:
        return f"{n / 10**8:,.0f}억"
    return f"{n:,}"


def _isodate(v) -> str | None:
    if not v:
        return None
    m = re.search(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})", v)
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else None


def _c(n) -> str:
    return f"{n:,}" if isinstance(n, (int, float)) and n else "-"


def _d(s: str) -> str:
    return f"{s[:4]}-{s[4:6]}-{s[6:]}" if s and len(s) == 8 else (s or "")


@dataclass
class Event:
    disc: Disclosure
    category: str
    importance: int
    parsed: dict | None = None
    api: dict | None = None          # DART 정형 API 레코드 (있으면)
    corrections: int = 0             # 같은 사건에 대한 정정 건수
    withdrawn: bool = False


@dataclass
class Board:
    corp: dict
    company: dict = field(default_factory=dict)
    bgn: str = ""
    end: str = ""
    shares: int | None = None
    shares_basis: str = ""
    audit: list[dict] = field(default_factory=list)
    holders: list[dict] = field(default_factory=list)
    holders_basis: str = ""
    holder_changes: list[dict] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    fin: list[dict] = field(default_factory=list)
    flags: list[tuple[str, str, str]] = field(default_factory=list)   # (레벨, 제목, 근거)
    errors: list[str] = field(default_factory=list)
    quarters: list[dict] = field(default_factory=list)
    peer: dict = field(default_factory=dict)        # {"snap":..., "cmp":[...], "ind":..}
    quote: dict = field(default_factory=dict)
    daily: list[dict] = field(default_factory=list)
    flows: list[dict] = field(default_factory=list)


# ====================================================================== 수집
class Collector:
    def __init__(self, client: DartClient):
        self.c = client

    def _api(self, ep: str, **params) -> list[dict]:
        try:
            return self.c._get_json(f"{ep}.json", **params).get("list", []) or []
        except DartError as e:
            return []

    def _periodic(self, ep: str, corp_code: str) -> tuple[list[dict], str]:
        """가장 최근 정기보고서 기준 값. (목록, '2026 반기' 같은 기준 표시)"""
        y = date.today().year
        for yr in (y, y - 1):
            for code, nm in REPRTS_RECENT_FIRST:
                rows = self._api(ep, corp_code=corp_code, bsns_year=str(yr), reprt_code=code)
                if rows:
                    return rows, f"{yr} {nm}보고서"
        return [], ""

    def collect(self, query: str, years: int = 2) -> Board:
        corp = self.c.find_corp(query)
        cc = corp["corp_code"]
        b = Board(corp=corp, end=f"{date.today():%Y%m%d}",
                  bgn=f"{date.today() - timedelta(days=365 * years):%Y%m%d}")
        try:
            b.company = self.c._get_json("company.json", corp_code=cc)
        except DartError:
            pass

        # 주식 총수
        rows, basis = self._periodic("stockTotqySttus", cc)
        tot = next((r for r in rows if r.get("se") in ("합계", "보통주")), None)
        com = next((r for r in rows if r.get("se") == "보통주"), None)
        b.shares = _num((com or tot or {}).get("istc_totqy"))
        b.shares_basis = basis

        # 감사의견 (최근 사업보고서 1건에 당기·전기·전전기 포함)
        y = date.today().year
        for yr in (y - 1, y - 2):
            rows = self._api("accnutAdtorNmNdAdtOpinion", corp_code=cc, bsns_year=str(yr), reprt_code=REPRT_ANNUAL)
            if rows:
                seen = set()
                for r in rows:
                    k = re.sub(r"\s+", "", r.get("bsns_year", ""))
                    if k in seen:
                        continue
                    seen.add(k)
                    b.audit.append({"기수": k, "감사인": r.get("adtor"), "의견": r.get("adt_opinion"),
                                    "강조사항": r.get("emphs_matter"), "핵심감사사항": r.get("core_adt_matter"),
                                    "특기사항": r.get("adt_reprt_spcmnt_matter")})
                break

        # 최대주주
        b.holders, b.holders_basis = self._periodic("hyslrSttus", cc)
        rows, _ = self._periodic("hyslrChgSttus", cc)
        b.holder_changes = rows

        # 공시 목록 + 정형 API
        discs = self.c.list_disclosures(cc, b.bgn, b.end)
        api_map: dict[str, dict] = {}
        for ep in ("cvbdIsDecsn", "bdwtIsDecsn", "exbdIsDecsn", "piicDecsn", "pifricDecsn",
                   "tsstkAqDecsn", "tsstkDpDecsn", "tsstkAqTrctrCnsDecsn", "tsstkAqTrctrCcDecsn",
                   "otcprStkInvscrInhDecsn", "otcprStkInvscrTrfDecsn"):
            for r in self._api(ep, corp_code=cc, bgn_de=b.bgn, end_de=b.end):
                api_map[r.get("rcept_no", "")] = {"_ep": ep, **r}

        for d in discs:
            k = classify(d)
            if k.importance < 3 and k.category not in ("자기주식", "지분변동"):
                continue
            ev = Event(d, k.category, k.importance, api=api_map.get(d.rcept_no))
            if k.category in PARSERS:          # 전용 파서가 있는 유형만 원문 수집 (정기보고서 등 대용량 원문 제외)
                try:
                    ev.parsed = parse_structured(k.category, self.c.fetch_document_text(d.rcept_no))
                except DartError as e:
                    b.errors.append(f"{d.report_nm} 원문 실패: {e}")
            ev.withdrawn = bool(ev.parsed and ev.parsed.get("철회"))
            b.events.append(ev)
        b.events = dedup(b.events)

        # 실적 (연간 요약 + 분기 시계열)
        for yr in (y - 1, y - 2):
            try:
                rows = self.c.financials(cc, yr, REPRT_ANNUAL)
            except DartError:
                rows = []
            if rows:
                b.fin = summarize_financials(rows)
                break
        try:
            b.quarters = quarterly.series(self.c, cc, years=2)
        except Exception as e:  # noqa: BLE001
            b.errors.append(f"분기 실적 수집 실패: {e}")

        # 업종 내 비교 (업종 지도가 있을 때만)
        try:
            m = industry.load_map()
            if m.get("corps"):
                snap = industry.peer_snapshot(self.c, m, cc)
                if snap:
                    b.peer = {"snap": snap, "cmp": industry.compare(snap, cc),
                              "ind": snap["ind"], "n": len(snap["peers"]), "year": snap["year"],
                              "fresh": industry.map_is_fresh(m)}
        except Exception as e:  # noqa: BLE001
            b.errors.append(f"업종 비교 실패: {e}")

        # 시세·수급 (네이버, 전일 종가) — 실패해도 공시 보드는 그대로
        if corp.get("stock_code"):
            try:
                b.quote = prices.get_quote(corp["stock_code"])
                b.daily = prices.get_daily(corp["stock_code"], days=365 * years + 30)
                b.flows = prices.get_flows(corp["stock_code"])
            except Exception as e:  # noqa: BLE001
                b.errors.append(f"시세 수집 실패: {e}")

        b.flags = make_flags(b)
        return b


# ====================================================================== 정정 묶기
def _event_key(ev: Event) -> tuple:
    p = ev.parsed or {}
    a = ev.api or {}
    if ev.category == "메자닌발행":
        kind = "BW" if "신주인수권" in ev.disc.report_nm else "EB" if "교환" in ev.disc.report_nm else "CB"
        rnd = a.get("bd_tm") or p.get("회차")
        if rnd:
            return ("메자닌발행", kind, str(rnd).strip())
    if ev.category == "메자닌취득":
        if p.get("회차"):
            return ("메자닌취득", str(p["회차"]), p.get("취득일") or ev.disc.rcept_dt)
    if ev.category == "유상증자":
        if p.get("이사회결의일"):
            return ("유상증자", p["이사회결의일"], p.get("증자방식") or "")
    base = re.sub(r"\[[^\]]*정정\]|\[첨부[^\]]*\]", "", ev.disc.report_nm).strip()
    if base != ev.disc.report_nm:          # 정정공시인데 키를 못 만든 경우: 같은 제목의 원공시와 묶음
        return ("제목", ev.category, base)
    return ("원공시", ev.disc.rcept_no)


def dedup(events: list[Event]) -> list[Event]:
    groups: dict[tuple, list[Event]] = defaultdict(list)
    for ev in events:
        groups[_event_key(ev)].append(ev)
    out = []
    for k, evs in groups.items():
        if k[0] == "원공시":
            out += evs
            continue
        evs.sort(key=lambda e: e.disc.rcept_no)
        latest = evs[-1]
        latest.corrections = len(evs) - 1
        latest.withdrawn = any(e.withdrawn for e in evs)
        out.append(latest)
    out.sort(key=lambda e: e.disc.rcept_no, reverse=True)
    return out


# ====================================================================== 지표 계산
def _results(b: Board) -> list[dict]:
    out = []
    for ev in b.events:
        if ev.category == "발행결과" and ev.parsed:
            out.append({**ev.parsed, "_date": ev.disc.rcept_dt, "_title": ev.disc.report_nm, "_url": ev.disc.url})
    return out


def _match_result(results: list[dict], *, board_date: str | None = None, shares: int | None = None,
                  rnd=None, kind: str = "") -> dict | None:
    for r in results:
        t = r["_title"] + " " + (r.get("발행방법") or "") + " " + (r.get("증권종류") or "")
        if kind == "rights" and not re.search(r"유상|보통주|주식", t):
            continue
        if kind == "mezz" and not re.search(r"CB|BW|EB|사채", t):
            continue
        if board_date and r.get("이사회결의일") == board_date:
            return r
        if rnd and r.get("회차") and str(r["회차"]) == str(rnd):
            return r
        if kind == "rights" and shares and r.get("실제주식수") == shares:
            return r
    return None


def _paid(r: dict | None) -> str:
    if not r:
        return "-"
    rate = r.get("납입률")
    mark = "✅" if rate is None or rate >= 99.9 else "⚠"
    return f"{mark} {r.get('납입일') or _d(r['_date'])}" + (f" ({rate}%)" if rate is not None and rate < 99.9 else "")


def withdrawals(b: Board) -> list[dict]:
    return [{**ev.parsed, "_date": ev.disc.rcept_dt, "_url": ev.disc.url}
            for ev in b.events if ev.category == "철회" and ev.parsed]


def adjustments(b: Board) -> dict:
    """회차별 가장 최근 전환가액 조정 (공시일 내림차순 events 이므로 첫 등장이 최신)."""
    out = {}
    for ev in b.events:
        if ev.category == "전환가액조정" and ev.parsed:
            for r in ev.parsed.get("조정", []):
                out.setdefault(r["회차"], {**r, "_date": ev.disc.rcept_dt, "_url": ev.disc.url})
    return out


def mezz_rows(b: Board) -> list[dict]:
    rows = []
    for ev in b.events:
        if ev.category != "메자닌발행" or ev.withdrawn:
            continue
        a, p = ev.api or {}, ev.parsed or {}
        kind = "BW" if "신주인수권" in ev.disc.report_nm else "EB" if "교환" in ev.disc.report_nm else "CB"
        face = _num(a.get("bd_fta")) or p.get("권면총액")
        price = _num(a.get("cv_prc") or a.get("ex_prc")) or p.get("전환가액")
        floor = _num(a.get("act_mktprcfl_cvprc_lwtrsprc") or a.get("act_mktprcfl_exprc_lwtrsprc")) or p.get("최저조정가액")
        shares = _num(a.get("cvisstk_cnt") or a.get("nstk_isstk_cnt")) or p.get("발행주식수") or (face // price if face and price else None)
        rows.append({
            "date": ev.disc.rcept_dt, "kind": kind, "회차": a.get("bd_tm") or p.get("회차"),
            "권면": face, "전환가": price, "최저가": floor, "주식수": shares,
            "최대주식수": (face // floor) if face and floor else shares,
            "사모": (a.get("bdis_mthn") or p.get("발행방법") or ""),
            "대상자": ", ".join(r["대상자"] for r in (p.get("발행대상자") or [])[:2]),
            "출처": "DART정형" if a else "파서", "정정": ev.corrections, "url": ev.disc.url,
            "납입": _paid(_match_result(_results(b), board_date=p.get("이사회결의일") or _isodate(a.get("bddd")),
                                        rnd=a.get("bd_tm") or p.get("회차"), kind="mezz")),
        })
    adj = adjustments(b)
    wd = [w for w in withdrawals(b) if w.get("대상") and "사채" in w["대상"]]
    for r in rows:
        try:
            k = int(str(r["회차"]).strip()) if r["회차"] else None
        except ValueError:
            k = None
        if k in adj:
            r["현재"] = adj[k]
        if any(w.get("회차") and k and w["회차"] == k for w in wd):
            r["납입"] = "⚠철회"
    rows = [r for r in rows if r["납입"] != "⚠철회"] + [r for r in rows if r["납입"] == "⚠철회"]
    return rows


def buyback_rows(b: Board) -> list[dict]:
    rows = []
    for ev in b.events:
        if ev.category != "메자닌취득":
            continue
        p = ev.parsed or {}
        rows.append({"date": ev.disc.rcept_dt, "구분": p.get("구분") or ("매도" if "매도" in ev.disc.report_nm else "취득"),
                     "회차": p.get("회차"), "권면": p.get("취득대상_권면금액"), "금액": p.get("취득금액"),
                     "처리": p.get("처리방안"), "사유": p.get("취득목적"), "url": ev.disc.url, "제목": ev.disc.report_nm})
    return rows


def overhang(b: Board) -> list[dict]:
    """
    회차별 '남아 있는' 잠재 주식 추정.
      잔액 = (최신 전환가액조정 공시의 미전환 권면 | 없으면 발행 권면) − 그 시점 이후 회사가 취득한 권면 + 재매각 권면
      주식 = 잔액 ÷ (최저조정가 | 최신 조정 전환가 | 전환가)   ← 리픽싱 하한 기준이라 '최대' 쪽 추정
    """
    issued = {}
    for r in mezz_rows(b):
        if r["납입"] == "⚠철회" or not r["회차"]:
            continue
        try:
            issued[int(str(r["회차"]).strip())] = r
        except ValueError:
            pass
    adj = adjustments(b)
    bbs = buyback_rows(b)
    out = []
    for k in sorted(set(issued) | set(adj)):
        iss, a = issued.get(k), adj.get(k)
        if a and a.get("미전환권면"):
            base, base_dt, src = a["미전환권면"], a["_date"], f"조정공시 {_d(a['_date'])}"
        elif iss and iss["권면"]:
            base, base_dt, src = iss["권면"], iss["date"], f"발행공시 {_d(iss['date'])}"
        else:
            continue
        bought = sum((x["권면"] or 0) for x in bbs if x["회차"] and str(x["회차"]) == str(k) and x["date"] > base_dt and x["구분"] == "취득")
        resold = sum((x["권면"] or 0) for x in bbs if x["회차"] and str(x["회차"]) == str(k) and x["date"] > base_dt and x["구분"] == "매도")
        remain = max(base - bought + resold, 0)
        price = (iss or {}).get("최저가") or (a or {}).get("조정후") or (iss or {}).get("전환가")
        out.append({"회차": k, "기준": src, "기준잔액": base, "이후취득": bought, "이후매도": resold, "잔액": remain,
                    "가격": price, "주식": remain // price if price else None, "기간이전": iss is None})
    return out


def rights_rows(b: Board) -> list[dict]:
    rows = []
    for ev in b.events:
        if ev.category != "유상증자" or ev.withdrawn:
            continue
        a, p = ev.api or {}, ev.parsed or {}
        new = (_num(a.get("nstk_ostk_cnt")) or 0) + (_num(a.get("nstk_estk_cnt")) or 0) or p.get("신주_합계")
        before = _num(a.get("bfic_tisstk_ostk")) or p.get("증자전_발행주식총수")
        rows.append({
            "date": ev.disc.rcept_dt, "방식": a.get("ic_mthn") or p.get("증자방식"), "신주": new, "증자전": before,
            "희석률": round(new / before * 100, 1) if new and before else None,
            "발행가": p.get("확정발행가_보통주") or p.get("예정발행가_보통주"),
            "기준주가대비": p.get("기준주가대비"),
            "대상자": ", ".join(f"{r['대상자']}" for r in (p.get("배정대상자") or [])[:2]),
            "출처": "DART정형" if a else "파서", "정정": ev.corrections, "url": ev.disc.url,
            "종속회사": "종속회사" in ev.disc.report_nm,
            "납입": _paid(_match_result(_results(b), board_date=p.get("이사회결의일"), shares=new, kind="rights")),
            "결의일": p.get("이사회결의일"),
        })
    for w in withdrawals(b):
        if w.get("대상") != "유상증자":
            continue
        for r in rows:
            if (w.get("신주") and r["신주"] == w["신주"]) or (w.get("최초결의일") and r["결의일"] == w["최초결의일"]):
                r["납입"] = f"⚠철회 {_d(w['_date'])}"
    return rows


def holder_summary(b: Board) -> dict:
    rows = [r for r in b.holders if (r.get("nm") or "").strip() not in ("계", "합계", "")]
    if not rows:
        return {}
    main = next((r for r in rows if (r.get("relate") or "").strip() == "최대주주"), rows[0])
    common = [r for r in rows if not re.search(r"우선|종류", r.get("stock_knd") or "")]
    total = sum((_flt(r.get("trmend_posesn_stock_qota_rt")) or 0) for r in (common or rows))
    return {"최대주주": main.get("nm"), "최대주주_지분": _flt(main.get("trmend_posesn_stock_qota_rt")),
            "특수관계인포함": round(total, 2), "인원": len(rows), "기준": b.holders_basis}


def last_holder_change(b: Board) -> str | None:
    """가장 최근 '최대주주변경' 공시일 (YYYYMMDD)."""
    ds = [ev.disc.rcept_dt for ev in b.events
          if ev.category == "지배구조" and re.search(r"최대주주\s*변경$|^최대주주변경|최대주주\s*변경\s*$", ev.disc.report_nm.replace("[기재정정]", "").strip())]
    return max(ds) if ds else None


def latest_pledge(b: Board) -> dict | None:
    for ev in b.events:
        if ev.category == "최대주주담보" and ev.parsed and ev.parsed.get("현재_지분율") is not None:
            pl = {**ev.parsed, "date": ev.disc.rcept_dt, "url": ev.disc.url}
            chg = last_holder_change(b)
            pl["stale"] = bool(chg and chg > ev.disc.rcept_dt)
            pl["stale_since"] = chg
            return pl
    return None


def pending_split(b: Board) -> dict | None:
    """최근 주식병합/분할 결정 중 효력일이 기간 내(또는 미래)인 것. 가격 비교 불가 구간 판단용."""
    for ev in b.events:
        if ev.category == "주식병합분할" and ev.parsed and ev.parsed.get("비율"):
            p = ev.parsed
            eff = (p.get("효력일") or "").replace("-", "")
            today = f"{date.today():%Y%m%d}"
            relist = (p.get("신주상장예정일") or "").replace("-", "")
            if not eff or relist >= today or eff >= today or (today[:6] == eff[:6]):
                return {**p, "date": ev.disc.rcept_dt, "url": ev.disc.url}
            return None
    return None


def cross_signals(b: Board) -> list[dict]:
    """공시 × 주가 교차 신호 (사실 표시). 각 항목: {lvl, title, why}"""
    out = []
    px = (b.quote or {}).get("close")
    if not px or not b.daily:
        return out
    sp = pending_split(b)
    if sp:
        r = sp["비율"]
        out.append({"lvl": "🟠", "title": f"주식{sp['구분']} {f'{r:g}:1' if r >= 1 else f'1:{1 / r:g}'} 진행 중",
                    "why": f"효력일 {sp.get('효력일')}, 신주 상장 {sp.get('신주상장예정일') or '-'}. 그 전 종가·주식수·전환가는 {sp['구분']} 전 기준이라 "
                           f"현재가와 직접 비교 불가 — 아래 CB·담보 가격 비교 신호는 생략 ([공시]({sp['url']}))"})
        return out
    if (b.quote or {}).get("halted"):
        out.append({"lvl": "🔴", "title": "현재 매매거래정지 상태", "why": f"네이버 기준 {b.quote.get('halt_text')} — 거래정지 공시 참고. 가격 비교 신호는 생략"})
        return out
    # 1) CB: 현재가 vs 전환가·리픽싱 하한 (회차별 집계 → 한 줄)
    near, below, above = [], [], []
    for r in overhang(b):
        iss = next((m for m in mezz_rows(b) if str(m["회차"]).strip() == str(r["회차"])), None)
        floor = (iss or {}).get("최저가")
        cv = r.get("가격")
        if not cv:
            continue
        if px <= cv and floor and px <= floor * (1 + TH["cb_near_floor"] / 100):
            near.append(f"{r['회차']}회(하한 {floor:,})")
        elif px <= cv:
            below.append(f"{r['회차']}회(전환가 {cv:,})")
        else:
            above.append(f"{r['회차']}회(전환가 {cv:,})")
    if near:
        out.append({"lvl": "🟠", "title": f"CB {len(near)}개 회차: 현재가가 리픽싱 하한 근접·이하",
                    "why": f"현재가 {px:,.0f}원. {', '.join(near)} — 전환가 추가 하향 불가 구간. 전환 유인 낮아 조기상환 청구(풋옵션) 가능성 ↑ (사실 표시, 기준 하한가×{1 + TH['cb_near_floor'] / 100:.1f})"})
    if below:
        out.append({"lvl": "🟡", "title": f"CB {len(below)}개 회차: 현재가 < 전환가",
                    "why": f"현재가 {px:,.0f}원. {', '.join(below)} — 전환 유인 낮음, 리픽싱 조건 있으면 다음 조정일 하향 가능"})
    if above:
        out.append({"lvl": "🟡", "title": f"CB {len(above)}개 회차: 현재가 > 전환가 (전환 가능 구간)",
                    "why": f"현재가 {px:,.0f}원. {', '.join(above)} — 전환 청구 시 신주 출회 가능"})
    # 2) 담보계약 공시일 대비 주가 하락
    pl = latest_pledge(b)
    if pl and not pl.get("stale"):
        base = prices.close_on(b.daily, pl["date"])
        if base and px < base:
            drop = (px / base - 1) * 100
            if drop <= -TH["pledge_drop"]:
                out.append({"lvl": "🟠", "title": f"최대주주 담보계약 이후 주가 {drop:.0f}%",
                            "why": f"담보 공시일({_d(pl['date'])}) 종가 {base:,.0f}원 → 현재 {px:,.0f}원. 담보 비율 {pl.get('담보비율')}%. 담보가치 하락 시 추가담보·반대매매 가능성 (사실 표시, 담보유지비율은 공시에 없음)"})
    # 3) 진행 중 유상증자 발행가 vs 현재가
    for r in rights_rows(b):
        if r["종속회사"] or r["납입"].startswith("⚠철회") or r["납입"].startswith("✅"):
            continue
        fp = r.get("발행가")
        if fp and px < fp * (1 - TH["rights_discount"] / 100):
            out.append({"lvl": "🟡", "title": f"진행 중 유상증자 발행가 {fp:,}원 > 현재가 {px:,.0f}원",
                        "why": f"현재가가 발행가보다 {(1 - px / fp) * 100:.0f}% 낮음 — 미납입 상태라 실권·정정 가능성 (결의 {r.get('결의일') or _d(r['date'])})"})
    return out


def make_flags(b: Board) -> list[tuple[str, str, str]]:
    F = []
    # 감사의견
    bad = [a for a in b.audit if a.get("의견") and a["의견"].strip() not in ("적정", "적정의견", "-")]
    if bad:
        F.append(("🔴", "감사의견 비적정",
                  "; ".join(f"{a['기수'].replace(chr(10), ' ')} {a['의견']}({a['감사인']})" for a in bad)))
    gc = [a for a in b.audit if a.get("강조사항") and "계속기업" in a["강조사항"]]
    gc_ev = [ev for ev in b.events if ev.category == "감사/외부감사" and ev.parsed and ev.parsed.get("계속기업_불확실성")]
    if gc or gc_ev:
        F.append(("🔴", "계속기업 관련 중요한 불확실성",
                  "감사보고서에 계속기업 불확실성 문단" + (f" ({_d(gc_ev[0].disc.rcept_dt)} 공시)" if gc_ev else "")))
    # 메자닌
    oh = overhang(b)
    if oh and b.shares:
        pot = sum(r["주식"] or 0 for r in oh)
        pct = pot / b.shares * 100
        lvl = "🟠" if pct >= TH["mezz_dilution_high"] else "🟡" if pct >= TH["mezz_dilution_warn"] else ""
        if lvl:
            F.append((lvl, f"CB·BW 잔여 잠재 주식 약 {pct:.1f}%",
                      f"{len(oh)}개 회차 남은 권면을 리픽싱 하한가로 전부 전환 시 약 {pot:,}주 (현재 {b.shares:,}주 대비). "
                      f"최신 전환가액조정 공시 잔액 − 이후 회사 취득분 기준 추정, 일반 전환청구분 미반영 / 기준 {TH['mezz_dilution_warn']:.0f}%·{TH['mezz_dilution_high']:.0f}%"))
    # 유상증자
    rr = [r for r in rights_rows(b) if not r["종속회사"] and not r["납입"].startswith("⚠철회")]
    if rr and b.shares:
        cum = sum(r["신주"] or 0 for r in rr)
        pct = cum / b.shares * 100
        if pct >= TH["rights_cum_high"]:
            F.append(("🟠", f"유상증자 누적 신주 {pct:.1f}%", f"기간 내 {len(rr)}건, 신주 합계 {cum:,}주 / 현재 {b.shares:,}주 (기준 {TH['rights_cum_high']:.0f}%)"))
        elif len(rr) >= 3:
            F.append(("🟡", f"유상증자 {len(rr)}건", f"신주 합계 {cum:,}주 = 현재 주식수의 {pct:.1f}%"))
    # 최대주주
    hs = holder_summary(b)
    if hs and hs.get("특수관계인포함") is not None and hs["특수관계인포함"] < TH["major_low"]:
        F.append(("🟡", f"최대주주 측 지분 {hs['특수관계인포함']}%", f"특수관계인 포함 {hs['기준']} 기준 (기준 {TH['major_low']:.0f}% 미만)"))
    pl = latest_pledge(b)
    if pl and not pl.get("stale") and pl.get("담보비율") is not None and pl["담보비율"] >= TH["pledge_high"]:
        F.append(("🟠", f"최대주주 보유분 {pl['담보비율']}% 담보 제공",
                  f"담보권 전부 실행 시 지분율 {pl.get('현재_지분율')}% → {pl.get('실행시_지분율')}% ({_d(pl['date'])} 공시)"))
    chg = [ev for ev in b.events if ev.category in ("지배구조",) and "최대주주" in ev.disc.report_nm and "변경" in ev.disc.report_nm]
    if len(chg) >= 2:
        F.append(("🟠", f"최대주주 변경 공시 {len(chg)}건", ", ".join(_d(e.disc.rcept_dt) for e in chg[:4])))
    elif chg:
        F.append(("🟡", "최대주주 변경", _d(chg[0].disc.rcept_dt)))
    # 기타
    for cat, lvl, label in (("불성실공시", "🟠", "불성실공시"), ("부실", "🔴", "회생·파산·부도 관련"), ("횡령배임", "🔴", "횡령·배임")):
        evs = [ev for ev in b.events if ev.category == cat]
        if evs:
            F.append((lvl, f"{label} {len(evs)}건", ", ".join(f"{_d(e.disc.rcept_dt)} {e.disc.report_nm}" for e in evs[:3])))
    halts = [ev for ev in b.events if ev.category == "거래정지" and not re.search(r"병합|분할|액면|전자등록|중요내용공시|정지해제", ev.disc.report_nm)]
    if halts:
        F.append(("🟡", f"매매거래정지 {len(halts)}건", ", ".join(f"{_d(e.disc.rcept_dt)} {e.disc.report_nm}" for e in halts[:3])))
    for msg in quarterly.signals(b.quarters):
        lvl = "🔴" if "자본잠식" in msg else "🟠" if ("연속" in msg or "부채비율" in msg) else "🟡"
        F.append((lvl, msg.split(" (")[0] if "(" in msg else msg, msg + " — 분기 실적 섹션 참고"))
    for r in (b.peer.get("cmp") or []):
        if r["지표"] in ("부채비율", "영업이익률") and r["표본"] >= 8 and r["상위%"] >= 90:
            tail = f"하위 {max(100 - r['상위%'], 1)}%" if r["순위"] < r["표본"] else "업종 최하위"
            F.append(("🟡", f"{r['지표']} 업종 {tail}" if r["순위"] < r["표본"] else f"{r['지표']} {tail}",
                      f"{r['값']:,}{'%' if r['지표'] != '매출액' else ''} vs 업종 중앙값 {r['중앙값']:,} ({r['순위']}/{r['표본']}위, {b.peer['year']}년 사업보고서)"))
    for x in cross_signals(b):
        F.append((x["lvl"], x["title"], x["why"]))
    order = {"🔴": 0, "🟠": 1, "🟡": 2}
    F.sort(key=lambda f: order.get(f[0], 9))
    return F


# ====================================================================== 렌더링
def render(b: Board) -> str:
    c, co = b.corp, b.company
    L = [f"# {c['corp_name']} ({c['stock_code'] or '-'}) 공시 리스크 보드",
         f"기간 {_d(b.bgn)} ~ {_d(b.end)} · 생성 {date.today()} · 대표 {co.get('ceo_nm', '-')} · 결산월 {co.get('acc_mt', '-')}"
         + (f" · 발행주식 {b.shares:,}주 ({b.shares_basis})" if b.shares else ""), "",
         "> DART 공시·정기보고서를 자동 정리한 **사실 자료**입니다. 매수·매도 의견이 아니며, 아래 '주의 신호'는 정해진 표시 기준(값과 함께 표기)에 따른 것입니다. "
         "모든 항목은 원문 링크로 확인할 수 있습니다.", ""]

    q = b.quote or {}
    if q.get("close"):
        chg = f"{q['change_pct']:+.2f}%" if q.get("change_pct") is not None else ""
        bits = [f"**{q['close']:,.0f}원** {chg} ({(q.get('traded_at') or '')[:10]} 종가)"]
        if q.get("market_cap"):
            bits.append(f"시총 {_won(q['market_cap'])}")
        if q.get("per"):
            bits.append(f"PER {q['per']}")
        if q.get("pbr"):
            bits.append(f"PBR {q['pbr']}")
        if q.get("high52") and q.get("low52"):
            bits.append(f"52주 {q['low52']:,}~{q['high52']:,}")
        if q.get("halted"):
            bits.append("🔴 **거래정지**")
        L += [" · ".join(bits), ""]

    L += ["## 주의 신호", ""]
    if b.flags:
        L += ["| | 항목 | 근거 |", "|---|---|---|"]
        L += [f"| {lvl} | **{t}** | {why} |" for lvl, t, why in b.flags]
    else:
        L.append("_표시 기준에 해당하는 항목 없음._")
    L.append("")

    # 감사의견
    L += ["## 감사의견", ""]
    if b.audit:
        L += ["| 사업연도 | 감사인 | 의견 | 강조사항 | 핵심감사사항 |", "|---|---|---|---|---|"]
        for a in b.audit:
            op = a["의견"] or "-"
            op = f"**{op}** ⚠" if op not in ("적정", "적정의견", "-") else op
            L.append(f"| {a['기수'].replace(chr(10), ' ')} | {a['감사인'] or '-'} | {op} | {(a['강조사항'] or '-')[:80]} | {(a['핵심감사사항'] or '-')[:60]} |")
    else:
        L.append("_정기보고서 감사의견 데이터 없음._")
    L.append("")

    # 메자닌
    mz, bb = mezz_rows(b), buyback_rows(b)
    L += ["## 전환사채·신주인수권부사채", ""]
    if mz:
        L += ["| 공시일 | 종류 | 회차 | 권면 | 전환가 → 최저조정가 | 전환 주식수 | 리픽싱 최대 | 발행 | 인수자 | 납입 | 출처 |",
              "|---|---|---|---:|---|---:|---:|---|---|---|---|"]
        for r in mz:
            pct = f" ({r['최대주식수'] / b.shares * 100:.1f}%)" if b.shares and r["최대주식수"] else ""
            L.append(f"| [{_d(r['date'])}]({r['url']}){' ⚠정정' + str(r['정정']) if r['정정'] else ''} | {r['kind']} | {r['회차'] or '-'} | {_won(r['권면'])} | "
                     f"{_c(r.get('전환가'))} → {_c(r.get('최저가'))} | {_c(r.get('주식수'))} | "
                     f"{_c(r.get('최대주식수'))}{pct} | {r['사모']} | {r['대상자'] or '-'} | {r['납입']} | {r['출처']} |")
        if b.shares:
            tot = sum(r["최대주식수"] or 0 for r in mz if r["납입"] != "⚠철회")
            L.append("")
            L.append(f"발행 기준 합계: 리픽싱 하한 기준 최대 {tot:,}주 = 현재 발행주식의 {tot / b.shares * 100:.1f}% _(발행 시점 값, 이후 변동 미반영)_")
    else:
        L.append("_기간 내 발행 없음._")
    adj = adjustments(b)
    if adj:
        L += ["", "**리픽싱(전환가액 조정) 실제 발생 — 회차별 최신 조정 공시 기준**", "",
              "| 조정 공시일 | 회차 | 전환가 | 조정 후 전환가능 주식 | 미전환 권면 | 현재 발행주식 대비 |", "|---|---|---|---:|---:|---:|"]
        for k, r in sorted(adj.items()):
            sh = r.get("조정후_주식수")
            pct = f"{sh / b.shares * 100:.1f}%" if sh and b.shares else "-"
            L.append(f"| [{_d(r['_date'])}]({r['_url']}) | {k} | {_c(r['조정전'])} → **{_c(r['조정후'])}** | {_c(sh)} | {_won(r.get('미전환권면'))} | {pct} |")
        L.append("_조정 공시일 시점의 미전환 잔액 기준. 이후 전환·상환은 반영 안 됨._")
    if bb:
        L += ["", "**회사의 자기 사채 취득·매도**", "", "| 공시일 | 구분 | 회차 | 대상 권면 | 금액 | 처리 | 사유 |", "|---|---|---|---:|---:|---|---|"]
        for r in bb:
            L.append(f"| [{_d(r['date'])}]({r['url']}) | {r['구분']} | {r['회차'] or '-'} | {_won(r['권면'])} | {_won(r['금액'])} | {r['처리'] or '-'} | {(r['사유'] or '-')[:40]} |")
    oh = overhang(b)
    if oh:
        L += ["", "**남은 잠재 주식 추정 (회차별)**", "",
              "| 회차 | 기준 잔액 | 이후 회사 취득 | 이후 재매각 | 추정 잔액 | 적용 가격 | 잠재 주식 | 현재 발행주식 대비 |",
              "|---|---|---:|---:|---:|---:|---:|---:|"]
        for r in oh:
            pct = f"{r['주식'] / b.shares * 100:.1f}%" if r["주식"] and b.shares else "-"
            L.append(f"| {r['회차']}{' (기간 이전 발행)' if r['기간이전'] else ''} | {_won(r['기준잔액'])} ({r['기준']}) | {_won(r['이후취득']) if r['이후취득'] else '-'} | "
                     f"{_won(r['이후매도']) if r['이후매도'] else '-'} | {_won(r['잔액'])} | {_c(r['가격'])} | {_c(r['주식'])} | {pct} |")
        if b.shares:
            pot = sum(r["주식"] or 0 for r in oh)
            L.append("")
            L.append(f"추정 합계 **{pot:,}주 = 현재 발행주식의 {pot / b.shares * 100:.1f}%** "
                     "_(일반 투자자의 전환청구로 이미 주식이 된 분은 반영 못 함 → 실제 잔여는 이보다 작을 수 있음)_")
    L.append("")

    # 유상증자
    rr = rights_rows(b)
    L += ["## 유상증자", ""]
    if rr:
        L += ["| 공시일 | 방식 | 신주 | 증자전 | 희석률 | 발행가 | 기준주가 대비 | 배정대상자 | 납입 | 출처 |", "|---|---|---:|---:|---:|---:|---:|---|---|---|"]
        for r in rr:
            when = f"결의 {r['결의일']}" if r.get("결의일") else _d(r["date"])
            L.append(f"| [{when}]({r['url']}){' ⚠정정' + str(r['정정']) if r['정정'] else ''}{' (종속회사)' if r['종속회사'] else ''} | {r['방식'] or '-'} | "
                     f"{_c(r.get('신주'))} | {_c(r.get('증자전'))} | {r['희석률'] if r['희석률'] is not None else '-'}% | "
                     f"{_c(r.get('발행가'))} | {('%+.1f%%' % r['기준주가대비']) if r['기준주가대비'] is not None else '-'} | {r['대상자'] or '-'} | {r['납입']} | {r['출처']} |")
        own = [r for r in rr if not r["종속회사"] and not r["납입"].startswith("⚠철회")]
        if own and b.shares:
            cum = sum(r["신주"] or 0 for r in own)
            L += ["", f"기간 누적 신주 **{cum:,}주 = 현재 발행주식의 {cum / b.shares * 100:.1f}%** (철회·종속회사 증자 제외, 결정 기준)"]
    else:
        L.append("_기간 내 유상증자 없음._")
    L.append("")

    # 최대주주
    hs, pl = holder_summary(b), latest_pledge(b)
    L += ["## 최대주주", ""]
    chg = last_holder_change(b)
    basis_dt = (b.holders[0].get("stlm_dt") or "").replace("-", "") if b.holders else ""
    if hs and chg and basis_dt and chg > basis_dt:
        L.append(f"- ⚠ 아래 지분율은 {hs['기준']}(기준일 {_d(basis_dt)}) 값. **이후 {_d(chg)} 최대주주 변경 공시** 있음 — 관련 공시 참고")
    if hs:
        L.append(f"- 최대주주 **{hs['최대주주']}** {hs['최대주주_지분']}% · 특수관계인 포함 **{hs['특수관계인포함']}%** ({hs['인원']}명, {hs['기준']})")
    if pl and pl.get("stale"):
        L.append(f"- 주식담보: [{_d(pl['date'])} 공시]({pl['url']}) 기준 담보제공자 {pl.get('담보제공자') or '-'} "
                 f"(보유분의 {pl.get('담보비율')}%) — **이후 {_d(pl['stale_since'])} 최대주주 변경**으로 현 최대주주 기준이 아님")
    elif pl:
        L.append(f"- 주식담보: {pl.get('담보제공자') or '최대주주'} 보유 {_c(pl.get('현재_주식수'))}주 중 **{pl.get('담보비율')}%** 담보, "
                 f"전부 실행 시 지분율 {pl.get('현재_지분율')}% → **{pl.get('실행시_지분율')}%** "
                 f"(채무 {_won(pl.get('채무금액_총액'))}, [{_d(pl['date'])} 공시]({pl['url']}))")
    if b.holder_changes:
        L += ["", "| 변동일 | 최대주주 | 주식수 | 지분율 | 사유 |", "|---|---|---:|---:|---|"]
        for r in b.holder_changes[:8]:
            q = _num(r.get("posesn_stock_co"))
            warn = " ⚠원문 기재값(발행주식수 초과)" if q and b.shares and q > b.shares else ""
            L.append(f"| {r.get('change_on', '-')} | {r.get('mxmm_shrholdr_nm', '-')} | {r.get('posesn_stock_co', '-')}{warn} | {r.get('qota_rt', '-')} | {(r.get('change_cause') or '-')[:40]} |")
    gov = [ev for ev in b.events if ev.category in ("지배구조", "최대주주양수도", "최대주주양수도해제")]
    if gov:
        L += ["", "관련 공시:"]
        L += [f"- [{_d(ev.disc.rcept_dt)} {ev.disc.report_nm}]({ev.disc.url}) — {headline(ev.parsed) or ''}" for ev in gov[:8]]
    if not (hs or pl or b.holder_changes or gov):
        L.append("_데이터 없음._")
    L.append("")

    # 타임라인
    L += ["## 주요 공시 타임라인 (★3 이상, 정정은 최신분으로 묶음)", ""]
    tl = [ev for ev in b.events if ev.importance >= 3]
    if tl:
        L += ["| 공시일 | 유형 | 공시 | 핵심 수치 |", "|---|---|---|---|"]
        for ev in tl[:40]:
            tag = (" ⚠철회" if ev.withdrawn else "") + (f" (정정 {ev.corrections}회)" if ev.corrections else "")
            L.append(f"| {_d(ev.disc.rcept_dt)} | {ev.category} {'★' * ev.importance} | [{ev.disc.report_nm}]({ev.disc.url}){tag} | {headline(ev.parsed) or ''} |")
        if len(tl) > 40:
            L.append(f"| … | | 외 {len(tl) - 40}건 | |")
    else:
        L.append("_없음._")
    L.append("")

    # 실적
    L += ["## 분기 실적 (최근 8개 분기)", ""]
    L += quarterly.table_md(b.quarters)
    L.append("")
    if b.fin:
        L += ["**연간 (최근 사업보고서)**", "", "| 계정 | 당기 | 전기 | 전전기 |", "|---|---:|---:|---:|"]
        for r in b.fin:
            L.append(f"| {r['계정']} | {_won(_num(r['당기']))} | {_won(_num(r['전기']))} | {_won(_num(r['전전기']))} |")
        L.append("")

    # 시세·수급
    if b.daily or b.flows:
        L += ["## 시세·수급 (네이버 금융, 전일 종가 기준)", ""]
        if b.daily:
            d = b.daily
            def _ret(n):
                if len(d) > n and d[-1 - n]["close"]:
                    return f"{(d[-1]['close'] / d[-1 - n]['close'] - 1) * 100:+.1f}%"
                return "-"
            L.append(f"- 수익률: 1주 {_ret(5)} · 1개월 {_ret(21)} · 3개월 {_ret(63)} · 6개월 {_ret(126)} · 1년 {_ret(250)}")
            hi = max(r["close"] for r in d[-250:]); lo = min(r["close"] for r in d[-250:] if r["close"])
            L.append(f"- 최근 1년 범위 {lo:,.0f} ~ {hi:,.0f}원, 현재 {d[-1]['close']:,.0f}원 (고점 대비 {(d[-1]['close'] / hi - 1) * 100:+.0f}%)")
        if b.flows:
            sf = prices.summary_flows(b.flows)
            def _s(k, label):
                v, st = sf.get(k), sf.get(k + "_streak") or 0
                if v is None:
                    return f"{label} -"
                return f"{label} {v / 10000:+,.0f}만주" + (f" ({abs(st)}일 연속 {'매수' if st > 0 else '매도'})" if abs(st) >= 3 else "")
            L.append(f"- 최근 {sf['days']}거래일 순매수: " + " · ".join([_s("individual", "개인"), _s("institution", "기관"), _s("foreign", "외국인")]))
            L += ["", "| 일자 | 종가 | 개인 | 기관 | 외국인 | 외인보유 |", "|---|---:|---:|---:|---:|---:|"]
            for r in b.flows[-10:]:
                L.append(f"| {_d(r['date'])} | {_c(r['close'])} | {_c(r['individual'])} | {_c(r['institution'])} | {_c(r['foreign'])} | {r['foreign_rate'] or '-'}% |")
        L.append("")

    # 업종 내 비교
    L += ["## 업종 내 비교", ""]
    pc = b.peer.get("cmp") or []
    if pc:
        L.append(f"표준산업분류 {b.peer['ind']}* 상장사 {b.peer['n']}곳, {b.peer['year']}년 사업보고서 기준 (연결 우선). 순위는 '좋은 쪽'이 1위.")
        L += ["", "| 지표 | 이 회사 | 업종 중앙값 | 순위 | 위치 |", "|---|---:|---:|---:|---|"]
        for r in pc:
            unit = "" if r["지표"] in ("매출액", "영업이익", "당기순이익", "자산총계") else "%"
            fmt = (lambda v: _won(int(v))) if unit == "" else (lambda v: f"{v:,.1f}%")
            pos = ("최상위" if r["순위"] == 1 else "최하위" if r["순위"] == r["표본"] else
                   "상위 10%" if r["상위%"] <= 10 else "상위 25%" if r["상위%"] <= 25 else "중간" if r["상위%"] <= 75 else "하위 25%" if r["상위%"] <= 90 else "하위 10%")
            L.append(f"| {r['지표']} | {fmt(r['값'])} | {fmt(r['중앙값'])} | {r['순위']}/{r['표본']} | {pos} |")
        if not b.peer.get("fresh"):
            L.append("")
            L.append("_⚠ 업종 지도가 아직 전체 상장사를 다 담지 못했거나 오래됨 — 표본이 실제 업종보다 작을 수 있음_")
        L.append("")
    else:
        L.append("_업종 지도 없음 (`python industry.py build` 로 생성) 또는 동종 상장사 재무 데이터 부족._")
        L.append("")

    L += ["---", "데이터: 금융감독원 DART (공시 원문, 주요사항보고서·정기보고서 정형 API), 시세·수급은 네이버 금융(전일 종가). '출처: DART정형' = 금감원 정형 데이터, '파서' = 원문에서 자동 추출."]
    if b.errors:
        L += ["", "수집 경고: " + "; ".join(b.errors[:5])]
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description="종목별 공시 리스크 보드")
    ap.add_argument("corp", nargs="?")
    ap.add_argument("--watchlist")
    ap.add_argument("--years", type=int, default=2)
    ap.add_argument("--out", default="reports/stocks")
    args = ap.parse_args(argv)
    targets = [args.corp] if args.corp else load_watchlist(args.watchlist) if args.watchlist else []
    if not targets:
        ap.error("종목 또는 --watchlist 필요")
    client = DartClient()
    col = Collector(client)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    summary = []
    for q in targets:
        try:
            b = col.collect(q, args.years)
        except Exception as e:  # noqa: BLE001
            print(f"[{q}] 실패: {e}", file=sys.stderr)
            continue
        path = out / f"{b.corp['corp_name']}.md"
        path.write_text(render(b), "utf-8")
        write_html(path, '<a href="index.html">← 종목 목록</a>')
        summary.append((b.corp["corp_name"], b.flags))
        print(f"[{b.corp['corp_name']}] 신호 {len(b.flags)}개: " + ", ".join(f"{l}{t}" for l, t, _ in b.flags))
    build_index(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
