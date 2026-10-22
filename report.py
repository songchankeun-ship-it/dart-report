"""마크다운 리포트 렌더링 (주간/임의 기간)."""
from __future__ import annotations

from datetime import date

from parsers import structured_lines

SIGNAL_ICON = {"긍정적": "🟢", "부정적": "🔴", "중립": "⚪", "판단보류": "🟡"}


def _fmt_amount(v) -> str:
    try:
        n = int(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return str(v or "-")
    if abs(n) >= 1_0000_0000_0000:
        return f"{n/1_0000_0000_0000:,.1f}조"
    if abs(n) >= 1_0000_0000:
        return f"{n/1_0000_0000:,.0f}억"
    return f"{n:,}"


def render(corp: dict, bgn: str, end: str, items: list[dict], financial_summary: list[dict],
           llm_used: bool) -> str:
    """items: [{"pack": FactPack, "analysis": dict|None}] (중요도 내림차순 정렬 권장)"""
    L = []
    L.append(f"# {corp['corp_name']} ({corp['stock_code'] or '비상장'}) 공시 리포트")
    L.append(f"기간: {bgn[:4]}-{bgn[4:6]}-{bgn[6:]} ~ {end[:4]}-{end[4:6]}-{end[6:]}  |  생성: {date.today()}  |  공시 {len(items)}건")
    L.append("")
    L.append("> 본 리포트는 DART 공시 원문을 자동 정리한 **사실 기반 자료**입니다. 특정 종목의 매수·매도를 권유하지 않으며, "
             "모든 숫자는 공시 원문에서 추출되었습니다. 투자 판단과 책임은 이용자 본인에게 있습니다.")
    L.append("")

    # ---- 요약 테이블
    L.append("## 한눈에 보기")
    L.append("")
    L.append("| 날짜 | 공시 | 유형 | 중요도 | 신호 | 한 줄 |")
    L.append("|---|---|---|---|---|---|")
    for it in items:
        p, a = it["pack"], it["analysis"]
        d, c = p.disclosure, p.classified
        sig = SIGNAL_ICON.get(a["signal"], "") + " " + a["signal"] if a else "-"
        head = a["headline"] if a else ""
        corr = " ⚠정정" if c["is_correction"] else ""
        L.append(f"| {d['rcept_dt'][4:6]}/{d['rcept_dt'][6:]} | [{d['report_nm']}]({d['url']}){corr} | {c['category']} | "
                 f"{'★'*c['importance']} | {sig} | {head} |")
    L.append("")

    # ---- 재무 스냅샷
    if financial_summary:
        L.append("## 재무 스냅샷 (최근 사업보고서)")
        L.append("")
        L.append("| 계정 | 당기 | 전기 | 전전기 |")
        L.append("|---|---:|---:|---:|")
        for r in financial_summary:
            L.append(f"| {r['계정']} | {_fmt_amount(r['당기'])} | {_fmt_amount(r['전기'])} | {_fmt_amount(r['전전기'])} |")
        L.append("")
        L.append("_단위: 원 기준 자동 환산(억/조). 출처: DART 단일회사 전체재무제표 API_")
        L.append("")

    # ---- 건별 상세
    L.append("## 공시별 상세")
    L.append("")
    for i, it in enumerate(items, 1):
        p, a = it["pack"], it["analysis"]
        d, c = p.disclosure, p.classified
        L.append(f"### {i}. {d['report_nm']}")
        L.append(f"- 접수일 {d['rcept_dt']} · 제출인 {d['flr_nm']} · 유형 **{c['category']}** · 중요도 {'★'*c['importance']}")
        L.append(f"- 원문: {d['url']}  (접수번호 {d['rcept_no']})")
        if c["hint"]:
            L.append(f"- 체크포인트: {c['hint']}")
        if p.related_history:
            hist = ", ".join(f"[{h['rcept_dt']}]({h['url']})" for h in p.related_history[:5])
            L.append(f"- 같은 회사 과거 유사 공시: {hist}")
        sl = structured_lines(p.structured)
        if sl:
            L.append("")
            L.append("**핵심 수치 (전용 파서)**")
            L += [f"- {x}" for x in sl]
            if p.structured and p.structured.get("_warning"):
                L.append(f"- ⚠ {p.structured['_warning']}")
        L.append("")
        if a:
            L.append(f"**{SIGNAL_ICON.get(a['signal'],'')} {a['signal']} (확신도 {a['confidence']}) — {a['headline']}**")
            L.append("")
            L.append(a["summary"])
            L.append("")
            if a.get("reasons"):
                L.append("근거")
                L += [f"- {x}" for x in a["reasons"]]
                L.append("")
            if a.get("watch_points"):
                L.append("추가 확인 필요")
                L += [f"- {x}" for x in a["watch_points"]]
                L.append("")
            if a.get("risks"):
                L.append("반대 시나리오 / 해석 한계")
                L += [f"- {x}" for x in a["risks"]]
                L.append("")
            if a.get("quotes"):
                L.append("원문 인용")
                L += [f"> {q}" for q in a["quotes"]]
                L.append("")
            v = a.get("_verification", {})
            if v and not v.get("passed", True):
                L.append(f"⚠ 자동검증 경고: 원문에 없는 인용 {len(v.get('rejected_quotes', []))}건 제거, "
                         f"원문에 없는 숫자 {v.get('numbers_not_in_source')} — 이 해석은 신뢰도를 낮췄습니다.")
                L.append("")
        else:
            L.append("_LLM 해석 생략 (--no-llm). 아래는 원문 추출 사실입니다._")
            L.append("")
        if p.key_facts:
            L.append("<details><summary>원문에서 추출한 수치</summary>")
            L.append("")
            L.append("| 항목 | 값 |")
            L.append("|---|---:|")
            for f in p.key_facts[:25]:
                L.append(f"| {f['label']} | {f['value']} {f['unit']} |")
            L.append("")
            L.append("</details>")
            L.append("")
        L.append("---")
        L.append("")

    L.append("## 방법론")
    L.append("- 데이터: 금융감독원 DART Open API (공시목록·원문·재무제표)")
    L.append("- 분류/중요도: 규칙 기반 (analyzer.RULES)")
    L.append("- 해석: " + ("LLM이 사실팩 안의 정보만으로 작성 → 인용문·숫자를 원문과 대조해 자동 검증"
                          if llm_used else "생략"))
    L.append("")
    return "\n".join(L)
