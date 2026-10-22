"""weekly.py 합본 렌더링을 가짜 CorpResult로 검증."""
from pathlib import Path
from dart_client import Disclosure
from analyzer import build_fact_pack
from pipeline import CorpResult, load_watchlist
from weekly import render_weekly

assert len(load_watchlist("watchlist.txt")) == 10

def mk(corp, name, cat_name, rcept, dt, text, analysis=None):
    d = Disclosure("0", corp, "000000", cat_name, rcept, corp, dt, "")
    return {"pack": build_fact_pack(d, text), "analysis": analysis}

a1 = {"headline": "300억 사모 CB", "summary": "요약문.", "signal": "부정적", "confidence": "중간",
      "reasons": [], "watch_points": ["인수자 실체", "희석률"], "risks": [], "quotes": [],
      "_verification": {"passed": True}}
r1 = CorpResult({"corp_name": "테스트전자", "stock_code": "123456", "corp_code": "0"}, "20261014", "20261020",
                items=[mk("테스트전자", "x", "주요사항보고서(전환사채권발행결정)", "1", "20261018", "전환가액 (원/주) | 12,500", a1),
                       mk("테스트전자", "x", "임원ㆍ주요주주특정증권등소유상황보고서", "2", "20261015", "")])
r2 = CorpResult({"corp_name": "빈회사", "stock_code": "222222", "corp_code": "0"}, "20261014", "20261020")
r3 = CorpResult({"corp_name": "없는회사", "stock_code": "", "corp_code": ""}, "20261014", "20261020", error="'없는회사' 에 해당하는 회사를 찾을 수 없습니다.")
md = render_weekly([r2, r3, r1], "20261014", "20261020", llm_used=True, detail_dir=Path("20261014_20261020"))
Path("sample_weekly.md").write_text(md, "utf-8")
assert "핵심 공시 (중요도 ★4 이상, 1건)" in md and "테스트전자" in md and "⚠ '없는회사'" in md
print(md)
print("\nWEEKLY TEST PASSED → sample_weekly.md")
