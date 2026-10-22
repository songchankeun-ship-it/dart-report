"""
회사 1곳 리포트

  python main.py 삼성전자 --days 7
  python main.py 005930 --from 20261001 --to 20261020
  python main.py 에코프로 --days 30 --no-llm          # LLM 없이 사실팩만
  python main.py 삼성전자 --days 7 --min-importance 3  # 중요 공시만 해석
  python main.py 삼성전자 --days 7 --out reports/

여러 종목 + 주간 합본은 weekly.py 참고.

환경변수(.env)
  DART_API_KEY=...          필수
  ANTHROPIC_API_KEY=...     LLM 해석 시 필요
  ANALYZER_MODEL=...        기본 claude-sonnet-5-5
"""
from __future__ import annotations

import argparse
import json
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
from pipeline import analyze_corp
import report
from html_report import write_html, build_index


def main(argv=None):
    ap = argparse.ArgumentParser(description="DART 공시 자동 리포트")
    ap.add_argument("corp", help="회사명 또는 6자리 종목코드")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--from", dest="bgn")
    ap.add_argument("--to", dest="end")
    ap.add_argument("--no-llm", action="store_true", help="LLM 해석 없이 사실팩만")
    ap.add_argument("--min-importance", type=int, default=2, help="이 값 미만 중요도는 해석 생략")
    ap.add_argument("--max-items", type=int, default=20)
    ap.add_argument("--out", default="reports")
    ap.add_argument("--history-days", type=int, default=365 * 3, help="과거 유사 공시 탐색 기간")
    args = ap.parse_args(argv)

    bgn = args.bgn or days_ago(args.days)
    end = args.end or f"{date.today():%Y%m%d}"

    try:
        client = DartClient()
    except DartError as e:
        print("오류:", e, file=sys.stderr)
        return 1

    res = analyze_corp(client, args.corp, bgn, end, use_llm=not args.no_llm,
                       min_importance=args.min_importance, max_items=args.max_items,
                       history_days=args.history_days)
    if res.error:
        print("오류:", res.error, file=sys.stderr)
        return 1
    if not res.items:
        return 0

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{res.corp['corp_name']}_{bgn}_{end}"
    md = report.render(res.corp, bgn, end, res.items, res.fin_summary, llm_used=not args.no_llm)
    (out_dir / f"{stem}.md").write_text(md, "utf-8")
    (out_dir / f"{stem}.json").write_text(json.dumps(
        [{"pack": it["pack"].__dict__, "analysis": it["analysis"]} for it in res.items],
        ensure_ascii=False, indent=1), "utf-8")
    nav = '<a href="index.html">← 리포트 목록</a>'
    h = write_html(out_dir / f"{stem}.md", nav)
    build_index(out_dir)
    print(f"저장: {out_dir / (stem + '.md')}  →  브라우저로 열기: {h}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
