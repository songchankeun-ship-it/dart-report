"""alerts.py 를 가짜 DartClient 로 검증 (네트워크 없음)."""
import json, os
from pathlib import Path
from datetime import date
import alerts
from dart_client import Disclosure

TODAY = f"{date.today():%Y%m%d}"
CB_TEXT = "2. 사채의 권면(전자등록)총액 (원) | 30,000,000,000\n8. 사채발행방법 | 사모\n9. 전환에 관한 사항 | 전환가액 (원/주) | 12,500\n| 시가하락에 따른 전환가액 조정 | 최저 조정가액 (원) | 8,750"

class FakeClient:
    def __init__(self):
        self.docs = {"A1": CB_TEXT, "A2": "", "B1": "x"}
        self.discs = {
            "001": [Disclosure("001", "테스트전자", "123456", "주요사항보고서(전환사채권발행결정)", "A1", "테스트전자", TODAY, ""),
                    Disclosure("001", "테스트전자", "123456", "임원ㆍ주요주주특정증권등소유상황보고서", "A2", "홍길동", TODAY, ""),
                    Disclosure("001", "테스트전자", "123456", "최대주주변경", "A0", "테스트전자", "20200101", "")],   # 과거
            "002": [Disclosure("002", "빈회사", "222222", "단일판매ㆍ공급계약체결", "B1", "빈회사", TODAY, "")],
        }
    def find_corp(self, q):  return {"corp_code": {"테스트전자": "001", "빈회사": "002"}[q]}
    def list_disclosures(self, code, bgn, end): return self.discs[code]
    def fetch_document_text(self, r): return self.docs[r]

import tempfile
alerts.STATE = Path(tempfile.gettempdir()) / "_alert_state_test.json"
if alerts.STATE.exists(): alerts.STATE.unlink()
c = FakeClient()

# 1회차: 오늘 공시 중 중요도 3 이상 → CB(4), 공급계약(4) 2건. 과거 A0 는 기전송 처리.
st = alerts.load_state()
n = alerts.check_once(c, ["테스트전자", "빈회사"], st, min_importance=3, use_llm=False, dry_run=True, lookback_days=2)
assert n == 2, n
saved = json.loads(alerts.STATE.read_text("utf-8"))
assert set(saved["seen"]) == {"A0", "A1", "A2", "B1"} and saved["corp_codes"] == {"테스트전자": "001", "빈회사": "002"}

# 2회차: 새 공시 없음 → 0
n = alerts.check_once(c, ["테스트전자", "빈회사"], st, min_importance=3, use_llm=False, dry_run=True, lookback_days=2)
assert n == 0

# 3회차: 유상증자 신규 → 1건
c.discs["002"].append(Disclosure("002", "빈회사", "222222", "유상증자결정", "B2", "빈회사", TODAY, ""))
c.docs["B2"] = "1. 신주의 종류와 수 | 보통주식 (주) | 1,000,000\n3. 증자전 발행주식총수 (주) | 10,000,000\n5. 증자방식 | 제3자배정증자"
n = alerts.check_once(c, ["테스트전자", "빈회사"], st, min_importance=3, use_llm=False, dry_run=True, lookback_days=2)
assert n == 1
print("ALERT TESTS PASSED")
