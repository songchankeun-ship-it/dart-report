"""tracker 기록/평가 로직 검증 (합성 데이터)."""
import json, tempfile
from pathlib import Path
import tracker

tmp = Path(tempfile.mkdtemp())
# 1) 보드 md → 기록
stocks = tmp / "stocks"; stocks.mkdir()
(stocks / "가나.md").write_text("# 가나 (111111) 공시 리스크 보드\n기간 x\n\n**1,000원** +1.00% (2026-10-23 종가) · 시총 1억\n\n## 주의 신호\n\n| | 항목 | 근거 |\n|---|---|---|\n| 🟠 | **최대주주 보유분 90.0% 담보 제공** | x |\n| 🟡 | **CB 2개 회차: 현재가 < 전환가** | y |\n", "utf-8")
(stocks / "다라.md").write_text("# 다라 (222222) 공시 리스크 보드\n기간 x\n\n**500원** -2.00% (2026-10-23 종가)\n\n## 주의 신호\n\n_표시 기준에 해당하는 항목 없음._\n", "utf-8")
log = tmp / "signals.jsonl"
assert tracker.log_from_boards(stocks, log, today="20261023") == 2
assert tracker.log_from_boards(stocks, log, today="20261023") == 0      # 같은 날 중복 방지
rows = tracker.load(log)
assert rows[0]["close"] == 1000 and rows[0]["max_lvl"] == "🟠" and len(rows[0]["flags"]) == 2
assert rows[1]["close"] == 500 and rows[1]["flags"] == []

# 2) 합성 시계열: 가나는 day0 에 신호 켜짐 → 20일 뒤 -20%, 다라는 무신호 +5%
rows = []
for i in range(25):
    d = f"2026{11:02d}{i + 1:02d}"
    rows.append({"date": d, "code": "111111", "name": "가나", "close": 1000 * (1 - 0.01 * i), "halted": False,
                 "flags": [{"lvl": "🟠", "title": "최대주주 보유분 90.0% 담보 제공"}] if i >= 0 else [], "max_lvl": "🟠"})
    rows.append({"date": d, "code": "222222", "name": "다라", "close": 500 * (1 + 0.0025 * i), "halted": False, "flags": [], "max_lvl": ""})
ev = tracker.evaluate(rows)
assert ev["events"] == 1, ev["events"]                    # 신호는 첫날만 '신규'
fam = ev["fam"]["최대주주 담보"]
assert fam["n"] == 1 and abs(fam["rets"][20][0] - (-20.0)) < 0.01, fam["rets"]
assert len(ev["ctrl"][20]) == 5 and all(v > 0 for v in ev["ctrl"][20])   # 다라 25일 중 +20일 가능한 5일
md = tracker.render(ev)
assert "최대주주 담보" in md and "-20.0%" in md and "표본 부족" in md
print("TRACKER TESTS PASSED")
