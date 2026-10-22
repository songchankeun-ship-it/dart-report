import sys, shutil
from pathlib import Path
import audit
from dart_client import Disclosure
from test_parsers import cb, ro, sc        # 모의 원문 재사용 (import 시 테스트도 같이 돎)

class Fake:
    def __init__(self):
        self.d = {"1": cb, "2": ro, "3": sc, "4": "의미없는 텍스트"}
    def list_disclosures(self, code, bgn, end, pblntf_ty=None):
        if pblntf_ty != "B": return []
        return [Disclosure("0","테스트전자","123456","주요사항보고서(전환사채권발행결정)","1","x","20261020",""),
                Disclosure("0","빈회사","222222","유상증자결정","2","x","20261020",""),
                Disclosure("0","공급사","333333","단일판매ㆍ공급계약체결","3","x","20261020",""),
                Disclosure("0","깨진사","444444","유상증자결정","4","x","20261020","")]
    def fetch_document_text(self, r, max_chars=0): return self.d[r]
    def find_corp(self, q): raise NotImplementedError

audit.DartClient = lambda: Fake()
shutil.rmtree("/tmp/audit_t", ignore_errors=True)
assert audit.main(["--days","3","--out","/tmp/audit_t"]) == 0
out = next(Path("/tmp/audit_t").iterdir())
h = (out/"audit.html").read_text("utf-8"); fr = (out/"fill_rate.md").read_text("utf-8")
assert "20,000,000,000" in h and "원문근거" in h and "깨진사" in fr and "희석률 (파생)" in fr
print(fr[:600]); print("AUDIT TEST PASSED")
