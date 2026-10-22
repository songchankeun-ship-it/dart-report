"""네트워크 없이 파이프라인 검증. python test_offline.py"""
import json
from dart_client import Disclosure, extract_text, summarize_financials
from analyzer import classify, extract_key_facts, build_fact_pack, _verify_quotes, _verify_numbers
import report

SAMPLE_HTML = """<DOCUMENT><BODY><TABLE>
<TR><TD>1. 사채의 종류</TD><TD>제5회 무기명식 이권부 무보증 사모 전환사채</TD></TR>
<TR><TD>2. 사채의 권면(전자등록)총액 (원)</TD><TD>30,000,000,000</TD></TR>
<TR><TD>3. 자금조달의 목적</TD><TD>운영자금 (원)</TD><TD>30,000,000,000</TD></TR>
<TR><TD>5. 사채의 이율</TD><TD>표면이자율 (%)</TD><TD>0</TD></TR>
<TR><TD>8. 전환에 관한 사항</TD><TD>전환가액 (원/주)</TD><TD>12,500</TD></TR>
<TR><TD></TD><TD>전환청구기간</TD><TD>2027년 10월 20일 ~ 2031년 09월 20일</TD></TR>
<TR><TD></TD><TD>시가하락에 따른 발행가액 조정</TD><TD>전환가액의 70%까지 조정 가능</TD></TR>
<TR><TD>14. 특정인에 대한 대상자별 사채발행내역</TD><TD>에이비씨투자조합 외 2인</TD></TR>
</TABLE><P>본 사채는 이사회 결의일 기준 자기자본 대비 약 18.3%에 해당합니다.</P>
</BODY></DOCUMENT>"""

d = Disclosure("00123456", "테스트전자", "123456", "주요사항보고서(전환사채권발행결정)",
               "20261020000123", "테스트전자", "20261020", "")

text = extract_text(SAMPLE_HTML.encode("utf-8"))
print("--- extracted text ---\n", text, "\n")

c = classify(d)
assert c.category == "메자닌발행" and c.importance == 4, c
print("classify OK:", c)

facts = extract_key_facts(text)
print("key facts:", json.dumps(facts, ensure_ascii=False))
assert any(f["value"] == "30,000,000,000" for f in facts)
assert any(f["value"] == "12,500" for f in facts)

pack = build_fact_pack(d, text)

# 가짜 LLM 결과로 검증 로직 테스트
fake = {
    "headline": "300억 사모 CB 발행, 전환가 12,500원",
    "summary": "테스트전자는 300억원 규모 사모 전환사채 발행을 결의했다. 전환가액은 12,500원이며 70%까지 리픽싱 가능하다.",
    "signal": "부정적", "confidence": "중간",
    "reasons": ["자기자본 대비 약 18.3% 규모", "표면이자율 0%로 투자자는 전환차익을 노리는 구조"],
    "watch_points": ["인수자 에이비씨투자조합의 실체", "9,999억원 전환 시 희석률"],   # 9,999 는 원문에 없음 → 검증 걸려야 함
    "risks": ["운영자금 목적이 실제 성장 투자라면 중립 해석 가능"],
    "quotes": ["전환가액의 70%까지 조정 가능", "이 문장은 원문에 없음"],
}
ok, bad = _verify_quotes(fake["quotes"], pack.excerpt)
assert ok == ["전환가액의 70%까지 조정 가능"] and bad == ["이 문장은 원문에 없음"], (ok, bad)
missing = _verify_numbers(fake, pack.excerpt)
print("quote check:", ok, bad, "| missing numbers:", missing)
assert missing == ["9,999억"], missing   # 300억→30,000,000,000 환산 통과, 12,500 통과, 9,999억만 걸려야 함
fake["quotes"] = ok
fake["_verification"] = {"rejected_quotes": bad, "numbers_not_in_source": missing, "passed": False}
fake["confidence"] = "낮음"

fin_rows = [
    {"account_nm": "매출액", "sj_nm": "손익계산서", "thstrm_amount": "1,234,567,000,000", "frmtrm_amount": "1,100,000,000,000", "bfefrmtrm_amount": "900,000,000,000", "thstrm_nm": "제30기"},
    {"account_nm": "영업이익", "sj_nm": "손익계산서", "thstrm_amount": "-5,000,000,000", "frmtrm_amount": "20,000,000,000", "bfefrmtrm_amount": "15,000,000,000", "thstrm_nm": "제30기"},
    {"account_nm": "자본총계", "sj_nm": "재무상태표", "thstrm_amount": "164,000,000,000", "frmtrm_amount": "170,000,000,000", "bfefrmtrm_amount": "160,000,000,000", "thstrm_nm": "제30기"},
]
fin = summarize_financials(fin_rows)
assert [r["계정"] for r in fin] == ["매출액", "영업이익", "자본총계"]

corp = {"corp_name": "테스트전자", "stock_code": "123456", "corp_code": "00123456"}
md = report.render(corp, "20261014", "20261020", [{"pack": pack, "analysis": fake}], fin, llm_used=True)
open("sample_report.md", "w", encoding="utf-8").write(md)
print("\n--- report head ---\n" + "\n".join(md.splitlines()[:14]))
print("\nALL TESTS PASSED → sample_report.md 생성")
