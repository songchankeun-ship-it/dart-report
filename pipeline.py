"""회사 1곳 분석 파이프라인 (main.py / weekly.py 공용)."""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date

from dart_client import DartClient, DartError, days_ago, summarize_financials, REPRT_ANNUAL
from analyzer import FactPack, build_fact_pack, classify, interpret


@dataclass
class CorpResult:
    corp: dict
    bgn: str
    end: str
    items: list[dict] = field(default_factory=list)      # [{"pack": FactPack, "analysis": dict|None}]
    fin_summary: list[dict] = field(default_factory=list)
    error: str | None = None

    @property
    def top_importance(self) -> int:
        return max((it["pack"].classified["importance"] for it in self.items), default=0)


def analyze_corp(client: DartClient, corp_query: str, bgn: str, end: str, *,
                 use_llm: bool = True, min_importance: int = 2, max_items: int = 20,
                 history_days: int = 365 * 3, log=print) -> CorpResult:
    try:
        corp = client.find_corp(corp_query)
    except DartError as e:
        return CorpResult(corp={"corp_name": corp_query, "stock_code": "", "corp_code": ""},
                          bgn=bgn, end=end, error=str(e))
    res = CorpResult(corp=corp, bgn=bgn, end=end)
    log(f"[{corp['corp_name']}] 공시 조회 {bgn}~{end}")

    discs = client.list_disclosures(corp["corp_code"], bgn, end)
    if not discs:
        log(f"[{corp['corp_name']}] 해당 기간 공시 없음")
        return res
    discs.sort(key=lambda d: (classify(d).importance, d.rcept_dt), reverse=True)
    discs = discs[:max_items]

    # 재무 스냅샷
    for yr in (date.today().year - 1, date.today().year - 2):
        try:
            rows = client.financials(corp["corp_code"], yr, REPRT_ANNUAL)
        except DartError:
            rows = []
        if rows:
            res.fin_summary = summarize_financials(rows)
            break

    # 과거 유사 공시
    by_cat: dict[str, list] = {}
    try:
        for h in client.list_disclosures(corp["corp_code"], days_ago(history_days), _prev_day(bgn)):
            by_cat.setdefault(classify(h).category, []).append(h)
    except DartError as e:
        log(f"[{corp['corp_name']}] 과거 공시 조회 실패: {e}")

    for d in discs:
        c = classify(d)
        try:
            text = client.fetch_document_text(d.rcept_no)
        except DartError as e:
            log(f"  원문 실패: {d.report_nm} {e}")
            text = ""
        pack = build_fact_pack(d, text, res.fin_summary if c.category == "정기보고서" else [],
                               by_cat.get(c.category, [])[:5])
        analysis = None
        if use_llm and c.importance >= min_importance and text:
            try:
                analysis = interpret(pack)
                tag = "✔" if analysis["_verification"]["passed"] else "⚠"
                log(f"  {tag} [{c.category}] {d.report_nm} → {analysis['signal']}")
            except Exception as e:  # noqa: BLE001
                log(f"  해석 실패: {d.report_nm} {e}")
        else:
            log(f"  · [{c.category}] {d.report_nm}")
        res.items.append({"pack": pack, "analysis": analysis})
    return res


def _prev_day(yyyymmdd: str) -> str:
    from datetime import datetime, timedelta
    return (datetime.strptime(yyyymmdd, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")


def load_watchlist(path: str) -> list[str]:
    """한 줄에 회사명 또는 종목코드 하나. '#' 뒤는 주석."""
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            s = line.split("#", 1)[0].strip()
            if s:
                out.append(s)
    return out
