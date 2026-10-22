"""
관심종목 일괄 → 주간 합본 리포트

  python weekly.py                          # watchlist.txt, 최근 7일, LLM 사용
  python weekly.py --days 7 --no-llm
  python weekly.py --watchlist my.txt --min-importance 3 --out reports/weekly

출력
  reports/weekly/주간공시_<시작>_<끝>.md      합본 (이번 주 핵심 공시 Top → 종목별)
  reports/weekly/<시작>_<끝>/<회사>.md        종목별 상세 (기존 main.py 포맷)
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
    load_dotenv("dart.env")   # 윈도우 탐색기에서 보이는 이름
except ImportError:
    pass

from dart_client import DartClient, DartError, days_ago
from pipeline import analyze_corp, load_watchlist, CorpResult
from parsers import structured_lines, headline
import report
from html_report import write_html, build_index

SIG = report.SIGNAL_ICON


def _d(s: str) -> str:
    return f"{s[:4]}-{s[4:6]}-{s[6:]}"


def render_weekly(results: list[CorpResult], bgn: str, end: str, llm_used: bool,
                  detail_dir: Path | None, top_n: int = 20, title: str = "주간",
                  stocks_link: bool = False) -> str:
    period = "이번 주" if title == "주간" else "이번 기간"
    L = [f"# {title} 공시 리포트  {_d(bgn)} ~ {_d(end)}",
         f"생성 {date.today()} · 관심종목 {len(results)}개 · 공시 {sum(len(r.items) for r in results)}건", "",
         "> DART 공시 원문을 자동 정리한 **사실 기반 자료**입니다. 특정 종목의 매수·매도를 권유하지 않으며, "
         "숫자는 공시 원문에서 추출되고 LLM 해석은 원문 대조 검증을 거칩니다. 투자 판단과 책임은 이용자 본인에게 있습니다.", ""]

    # ---- 이번 주 핵심 공시 (전 종목 통합, 중요도 ≥4)
    hot = []
    for r in results:
        for it in r.items:
            c = it["pack"].classified
            if c["importance"] >= 4:
                hot.append((c["importance"], it["pack"].disclosure["rcept_dt"], r, it))
    hot.sort(key=lambda x: (x[0], x[1]), reverse=True)
    L.append(f"## {period} 핵심 공시 (중요도 ★4 이상, {len(hot)}건)")
    L.append("")
    if hot:
        sigcol = llm_used
        L.append("| 종목 | 날짜 | 공시 | 유형 | 중요도 | " + ("신호 | " if sigcol else "") + "핵심 수치 |")
        L.append("|---|---|---|---|---|" + ("---|" if sigcol else "") + "---|")
        for imp, dt, r, it in hot[:top_n]:
            p, a = it["pack"], it["analysis"]
            d, c = p.disclosure, p.classified
            sig = (f"{SIG.get(a['signal'], '')} {a['signal']}" if a else "-") + " | " if sigcol else ""
            head = (a["headline"] + " · " if a else "") + headline(p.structured)
            if not head:                                   # 파서 없는 유형: 제목 괄호 안 사유라도
                m = re.search(r"\(([^()]*(?:\([^()]*\))?[^()]*)\)\s*$", d["report_nm"].replace("주요사항보고서", ""))
                head = f"사유: {m.group(1).strip()}" if m else ""
            corr = " ⚠정정" if c["is_correction"] else ""
            L.append(f"| **{r.corp['corp_name']}** | {dt[4:6]}/{dt[6:]} | [{d['report_nm']}]({d['url']}){corr} | "
                     f"{c['category']} | {'★'*imp} | {sig}{head} |")
        if len(hot) > top_n:
            L.append(f"| … | | 외 {len(hot)-top_n}건은 종목별 섹션 참고 | | |" + (" |" if sigcol else "") + " |")
    else:
        L.append(f"_{period} 중요도 ★4 이상 공시 없음._")
    L.append("")

    # ---- 신호 집계
    if llm_used:
        cnt = {"긍정적": 0, "부정적": 0, "중립": 0, "판단보류": 0}
        for r in results:
            for it in r.items:
                if it["analysis"]:
                    cnt[it["analysis"]["signal"]] = cnt.get(it["analysis"]["signal"], 0) + 1
        L.append("신호 집계: " + " · ".join(f"{SIG[k]} {k} {v}" for k, v in cnt.items() if v))
        L.append("")

    # ---- 종목별 요약
    L.append("## 종목별 요약")
    L.append("")
    L.append("| 종목 | 공시 수 | 최고 중요도 | 주요 공시 | 상세 | 리스크 보드 |")
    L.append("|---|---:|---|---|---|---|")
    for r in sorted(results, key=lambda r: (r.top_importance, len(r.items)), reverse=True):
        name = f"{r.corp['corp_name']} ({r.corp['stock_code'] or '-'})"
        if r.error:
            L.append(f"| {name} | - | - | ⚠ {r.error} | | |")
            continue
        board = f"[보드](../stocks/{r.corp['corp_name']}.html)" if stocks_link else ""
        if not r.items:
            L.append(f"| {name} | 0 | - | 공시 없음 | | {board} |")
            continue
        top = r.items[0]["pack"]
        main_desc = f"[{top.disclosure['report_nm']}]({top.disclosure['url']})"
        link = f"[보기]({(detail_dir / (r.corp['corp_name'] + '.html')).as_posix()})" if detail_dir else ""
        L.append(f"| {name} | {len(r.items)} | {'★'*r.top_importance} | {main_desc} | {link} | {board} |")
    L.append("")

    # ---- 종목별 상세 (합본 안에 요약 수준으로)
    L.append("## 종목별 공시")
    L.append("")
    for r in sorted(results, key=lambda r: (r.top_importance, len(r.items)), reverse=True):
        if r.error or not r.items:
            continue
        L.append(f"### {r.corp['corp_name']} ({r.corp['stock_code'] or '-'})")
        L.append("")
        for it in r.items:
            p, a = it["pack"], it["analysis"]
            d, c = p.disclosure, p.classified
            head = f"- {d['rcept_dt'][4:6]}/{d['rcept_dt'][6:]} {'★'*c['importance']} [{d['report_nm']}]({d['url']}) — {c['category']}"
            if a:
                head += f" · {SIG.get(a['signal'],'')} {a['signal']}: {a['headline']}"
            L.append(head)
            for x in structured_lines(p.structured)[:3]:
                L.append(f"  - {x}")
            if a and a.get("summary"):
                L.append(f"  - {a['summary']}")
            if a and a.get("watch_points"):
                L.append(f"  - 확인 필요: {'; '.join(a['watch_points'][:2])}")
        L.append("")

    L.append("---")
    L.append("데이터: 금융감독원 DART Open API · 분류: 규칙 기반 · 해석: " +
             ("LLM(사실팩 한정) + 원문 대조 자동검증" if llm_used else "생략"))
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description="관심종목 주간 합본 리포트")
    ap.add_argument("--watchlist", default="watchlist.txt")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--from", dest="bgn")
    ap.add_argument("--to", dest="end")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--min-importance", type=int, default=3, help="합본은 기본 3 이상만 LLM 해석(비용 절약)")
    ap.add_argument("--max-items", type=int, default=15)
    ap.add_argument("--out", default="reports/weekly")
    ap.add_argument("--no-detail", action="store_true", help="종목별 상세 md 생성 생략")
    ap.add_argument("--since-last", action="store_true",
                    help="마지막 실행일부터 오늘까지 (첫 실행이면 --days). 매일 자동 실행용 — 주말·PC꺼짐에도 빠짐/중복 없음")
    ap.add_argument("--prefix", default="주간공시", help="합본 파일명 앞부분 (예: 일일공시)")
    ap.add_argument("--stocks-link", action="store_true", help="종목별 리스크 보드(../stocks/종목.html) 링크 표시")
    args = ap.parse_args(argv)

    end = args.end or f"{date.today():%Y%m%d}"
    last_file = Path(args.out) / ".last_run"
    bgn = args.bgn or days_ago(args.days)
    if args.since_last and not args.bgn and last_file.exists():
        prev = last_file.read_text().strip()
        if prev.isdigit() and len(prev) == 8 and prev <= end:
            bgn = prev                       # 지난 실행일 포함 (그날 실행 시각 이후 공시까지 커버)
    try:
        corps = load_watchlist(args.watchlist)
    except FileNotFoundError:
        print(f"오류: {args.watchlist} 가 없습니다. 한 줄에 회사명/종목코드 하나씩 적어주세요.", file=sys.stderr)
        return 1
    if not corps:
        print("오류: 관심종목이 비어 있습니다.", file=sys.stderr)
        return 1

    try:
        client = DartClient()
    except DartError as e:
        print("오류:", e, file=sys.stderr)
        return 1

    out_dir = Path(args.out)
    detail_dir = None if args.no_detail else out_dir / f"{bgn}_{end}"
    (detail_dir or out_dir).mkdir(parents=True, exist_ok=True)

    results: list[CorpResult] = []
    for i, q in enumerate(corps, 1):
        print(f"\n=== [{i}/{len(corps)}] {q} ===")
        try:
            r = analyze_corp(client, q, bgn, end, use_llm=not args.no_llm,
                             min_importance=args.min_importance, max_items=args.max_items)
        except Exception as e:  # noqa: BLE001  — 한 종목 실패가 전체를 멈추지 않게
            r = CorpResult(corp={"corp_name": q, "stock_code": "", "corp_code": ""}, bgn=bgn, end=end, error=str(e))
            print("  실패:", e, file=sys.stderr)
        results.append(r)
        if detail_dir and r.items and not r.error:
            md = report.render(r.corp, bgn, end, r.items, r.fin_summary, llm_used=not args.no_llm)
            (detail_dir / f"{r.corp['corp_name']}.md").write_text(md, "utf-8")
            write_html(detail_dir / f"{r.corp['corp_name']}.md", '<a href="../index.html">← 리포트 목록</a>')

    rel_detail = Path(f"{bgn}_{end}") if detail_dir else None
    md = render_weekly(results, bgn, end, llm_used=not args.no_llm, detail_dir=rel_detail,
                       title="일일" if args.prefix.startswith("일일") else "주간",
                       stocks_link=args.stocks_link)
    path = out_dir / f"{args.prefix}_{bgn}_{end}.md"
    path.write_text(md, "utf-8")
    h = write_html(path, '<a href="index.html">← 리포트 목록</a>')
    build_index(out_dir)
    failed = [r.corp["corp_name"] for r in results if r.error]
    if args.since_last:
        if failed:
            print(f"\n⚠ 실패 종목 {failed} — 다음 실행 때 이번 기간을 다시 조회하도록 마지막 실행일을 갱신하지 않음")
        else:
            last_file.write_text(end)
    print(f"\n합본 저장: {path}\n브라우저로 열기: {h}  (목록: {out_dir / 'index.html'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
