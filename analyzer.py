"""
공시 분석 모듈
1) 공시 유형 분류 (규칙 기반)
2) 사실팩(fact pack) 구성: 원문에서 뽑은 숫자·문장, 재무 요약
3) LLM 해석: 사실팩 '안의' 정보만으로 해석. 숫자 생성 금지, 매수/매도 추천 금지, 근거 인용 필수.

설계 원칙
- 숫자는 항상 파싱된 원문/재무 API 값에서 온다. LLM은 해석과 구조화만 한다.
- LLM 출력은 JSON 스키마로 강제하고, 인용(quotes)이 실제 원문에 있는지 코드로 검증한다.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

from dart_client import Disclosure
from parsers import parse_structured

# ------------------------------------------------------------------ 분류
# (유형, 중요도 1~5, 기본 톤 힌트)  — 톤 힌트는 LLM 참고용, 최종 판단은 근거 기반으로 재평가
RULES: list[tuple[str, str, int, str]] = [
    (r"감사보고서|감사의견|한정|부적정|의견거절",            "감사/외부감사",   5, "감사의견 변경·비적정은 상장폐지 사유 가능 → 최우선 확인"),
    (r"불성실공시|공시번복|공시불이행",                       "불성실공시",     5, "신뢰도 훼손 신호"),
    (r"최대주주등?\s*소유\s*주식\s*변동",                    "지분변동",       2, "최대주주 측 보유주식 변동 신고 — 방향(증가/감소) 확인"),
    (r"최대주주\s*변경을?\s*수반하는\s*주식\s*담보",         "최대주주담보",   4, "최대주주 지분 담보제공 — 담보권 실행 시 지분율·반대매매 위험 확인"),
    (r"최대주주\s*변경을?\s*수반하는\s*주식\s*양수도.*(해제|취소)", "최대주주양수도해제", 5, "경영권 양수도 계약 해제/취소 — 사유·위약 조건 확인"),
    (r"최대주주\s*변경을?\s*수반하는\s*주식\s*양수도",       "최대주주양수도", 5, "경영권 양수도 계약 — 양수인 실체·대금 출처·잔금 일정 확인"),
    (r"최대주주\s*변경|경영권\s*변동",                        "지배구조",       5, "경영권 변동 — 인수자 성격·대금 출처가 핵심"),
    (r"회생|파산|채무불이행|기업회생",                        "부실",           5, "존속 위험 신호"),
    (r"횡령|배임",                                           "횡령배임",       5, "내부통제 실패 — 금액 대비 자기자본 비율 확인"),
    (r"정지\s*해제\s*\(?중요\s*내용|중요\s*내용\s*공시",          "중요내용정지",   2, "중요 공시에 따른 30분 매매정지 — 통상 절차"),
    (r"매매\s*거래\s*정지|거래정지",                           "거래정지",       4, "정지 사유가 핵심 — 주식병합/분할 같은 기술적 사유인지, 상장적격성·불성실 등 위험 사유인지 구분"),
    (r"(사채|CB|BW|EB).*(취득|재매각|매도|소각)",                "메자닌취득",     3, "기발행 사채 취득·재매각 — 재매각이면 희석 물량 재출현"),
    (r"주식\s*병합|주식\s*분할|액면\s*(병합|분할)",                 "주식병합분할",   4, "병합/분할 비율과 효력일 — 전후 주가·주식수 비교 불가 구간 확인"),
    (r"철회",                                                   "철회",           4, "증자·사채 발행 등 기존 결정의 철회 — 철회 사유와 대상 확인"),
    (r"전환가액의?\s*조정|행사가액의?\s*조정|교환가액의?\s*조정", "전환가액조정",   3, "리픽싱 실제 발생 — 조정 후 전환가능 주식수 증가분 확인"),
    (r"권리락|발행가액\s*(결정|확정)|신주인수권증서|실권주",      "증자진행",       2, "진행 중인 증자의 절차 공시"),
    (r"증권\s*발행\s*결과",                                     "발행결과",       3, "결정된 증자·사채가 실제로 납입됐는지 — 예정 대비 실제 금액 확인"),
    (r"자기\s*(전환사채|신주인수권부사채|교환사채).*(취득|매도|소각)|사채.*만기전\s*취득", "메자닌취득", 3, "자기 CB/BW 만기전 취득은 희석 물량 감소(통상 중립~긍정), 재매도는 물량 재출현 확인"),
    (r"(전환사채|신주인수권부사채|교환사채).*발행|CB|BW|EB",   "메자닌발행",     4, "희석 가능 물량·전환가·리픽싱·인수자 확인"),
    (r"기타\s*경영\s*사항",                                     "기타경영사항",   2, "자율공시 — 제목 괄호 안 내용 확인"),
    (r"유상증자",                                            "유상증자",       4, "주주배정/3자배정 여부, 할인율, 자금 사용목적 확인"),
    (r"무상증자",                                            "무상증자",       3, "실질 가치 변화 없음, 유통주식 증가"),
    (r"자기주식\s*(취득|처분|소각)|자사주",                   "자기주식",       3, "취득/소각은 주주환원, 처분은 물량 부담"),
    (r"현금.?배당|주식배당|배당",                             "배당",           3, "배당수익률·배당성향 비교"),
    (r"단일판매|공급계약",                                   "공급계약",       4, "계약금액 대비 최근 매출 비율이 핵심"),
    (r"타법인\s*주식.*(취득|처분)",                             "타법인지분",     4, "자기자본 대비 금액·상대방 관계(계열사 여부)·취득 목적 확인"),
    (r"특수관계인에?\s*대한\s*출자",                            "계열사출자",     3, "계열사 지원 성격 — 금액 규모와 누적 출자액 확인"),
    (r"출자|영업양수|영업양도|합병|회사\s*분할|물적\s*분할|인적\s*분할",  "M&A/구조조정",   4, "거래 규모·상대방·대금 조건"),
    (r"소송|가처분|제재|과징금|시정명령",                     "소송/제재",      3, "소송가액 대비 자기자본"),
    (r"임상|허가|승인|특허",                                 "R&D/인허가",     3, "단계(1상/2상/3상, 품목허가) 명확히"),
    (r"조회공시|풍문|보도",                                   "조회공시",       3, "회사 답변 내용(부인/검토중) 확인"),
    (r"주주총회|정관|사외이사|대표이사\s*변경",                "지배구조/IR",    2, "안건 내용 확인"),
    (r"사업보고서|분기보고서|반기보고서",                      "정기보고서",     3, "재무 추세·특이 주석(우발부채, 계속기업 불확실성)"),
    (r"지분\s*공시|대량보유|임원.*주요주주.*소유",              "지분변동",       2, "내부자 매매 방향"),
]


@dataclass
class Classified:
    category: str
    importance: int      # 1~5
    hint: str
    is_correction: bool  # 정정공시 여부


def classify(d: Disclosure) -> Classified:
    name = d.report_nm
    is_corr = "정정" in name or "[정정]" in name
    for pat, cat, imp, hint in RULES:
        if re.search(pat, name):
            return Classified(cat, imp, hint, is_corr)
    return Classified("기타", 1, "", is_corr)


# ------------------------------------------------------------------ 사실 추출
_NUM = re.compile(r"^-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?$")
_UNIT_IN_LABEL = re.compile(r"\((원/주|백만원|천원|억원|원|주|%|％|USD|\$)\)\s*$")
_INLINE = re.compile(
    r"([가-힣A-Za-z]{2,30}(?:\s[가-힣A-Za-z]{1,30}){0,3})\s*[:：]?\s*((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*(원|백만원|천원|억원|주|%|％)"
)


def extract_key_facts(text: str, limit: int = 40) -> list[dict]:
    """
    원문에서 '라벨 + 숫자 (+단위)' 사실 후보 추출. (검증용·LLM 입력용)
    DART 표는 보통  `라벨 (단위) | 값`  형태라 셀 단위로 먼저 보고, 본문 문장은 인라인 패턴으로 보완.
    """
    facts, seen = [], set()

    def add(label: str, value: str, unit: str):
        label = re.sub(r"\s+", " ", label).strip(" |-:：")
        label = re.sub(r"^\d+(\.\d+)*\.?\s*", "", label)        # "2. " 같은 항목 번호 제거
        if len(label) < 2:
            return
        key = (label, value, unit)
        if key in seen:
            return
        seen.add(key)
        facts.append({"label": label, "value": value, "unit": unit})

    for line in text.splitlines():
        cells = [c.strip() for c in line.split("|")]
        if len(cells) >= 2:
            prev = ""
            for c in cells:
                if _NUM.match(c) and prev:
                    m = _UNIT_IN_LABEL.search(prev)
                    unit = m.group(1) if m else ""
                    label = _UNIT_IN_LABEL.sub("", prev).strip()
                    add(label, c, unit)
                elif c:
                    prev = c
                if len(facts) >= limit:
                    return facts
        for m in _INLINE.finditer(line):
            add(m.group(1), m.group(2), m.group(3))
            if len(facts) >= limit:
                return facts
    return facts


@dataclass
class FactPack:
    disclosure: dict
    classified: dict
    key_facts: list[dict]
    excerpt: str
    financial_summary: list[dict] = field(default_factory=list)
    related_history: list[dict] = field(default_factory=list)   # 같은 회사의 과거 유사 공시
    structured: dict | None = None                               # 전용 파서 결과 (CB/유상증자/공급계약)

    def to_prompt_json(self) -> str:
        return json.dumps({
            "disclosure": self.disclosure,
            "classified": self.classified,
            "structured": self.structured,
            "key_facts": self.key_facts,
            "financial_summary": self.financial_summary,
            "related_history": self.related_history,
            "excerpt": self.excerpt,
        }, ensure_ascii=False, indent=1)


def build_fact_pack(d: Disclosure, text: str, financial_summary: list[dict] | None = None,
                    related_history: list[Disclosure] | None = None, excerpt_chars: int = 12_000) -> FactPack:
    c = classify(d)
    return FactPack(
        disclosure=d.to_dict(),
        classified={"category": c.category, "importance": c.importance,
                    "hint": c.hint, "is_correction": c.is_correction},
        key_facts=extract_key_facts(text),
        excerpt=text[:excerpt_chars],
        financial_summary=financial_summary or [],
        related_history=[h.to_dict() for h in (related_history or [])],
        structured=parse_structured(c.category, text),
    )


# ------------------------------------------------------------------ LLM 해석
SYSTEM_PROMPT = """당신은 한국 상장사 공시를 읽고 '사실 기반 해석'을 작성하는 리서치 보조원이다.

절대 규칙
1. 아래 제공된 fact pack 안에 있는 정보만 사용한다. 외부 지식으로 숫자·날짜·인물·계약조건을 보태지 않는다.
2. 숫자를 새로 만들지 않는다. 언급하는 모든 숫자는 structured, excerpt, key_facts, financial_summary 중 어딘가에 그대로 존재해야 한다.
   structured 는 전용 파서가 뽑은 정형 값이며 희석률·리픽싱 하한비율 같은 파생치가 들어 있다. 이 값을 최우선으로 신뢰하고 인용한다.
   structured 에 _warning 이 있으면 파싱이 불완전한 것이니 excerpt 로 교차 확인하고 확신도를 낮춘다.
3. 매수/매도/보유 추천, 목표가, 수익률 전망을 쓰지 않는다. "주가가 오를 것" 같은 표현 금지.
   가능한 표현: "이 공시는 통상 ○○으로 해석된다", "확인이 필요한 점은 ○○이다".
4. 근거가 부족하면 signal 을 "판단보류"로 두고 reasons 에 무엇이 부족한지 쓴다.
5. quotes 에는 excerpt 안의 문장을 '토씨 하나 바꾸지 말고' 그대로 복사한다. (코드로 검증됨)
6. 반드시 아래 JSON 스키마로만 응답한다. 그 외 텍스트 금지.

출력 스키마
{
  "headline": "한 줄 요약 (40자 이내)",
  "summary": "3~5문장. 무엇을, 얼마에, 누가, 언제. 사실만.",
  "signal": "긍정적|부정적|중립|판단보류",
  "confidence": "높음|중간|낮음",
  "reasons": ["근거 1 (사실 인용 포함)", "근거 2", "..."],
  "watch_points": ["투자자가 추가로 확인해야 할 점 1", "..."],
  "risks": ["이 해석이 틀릴 수 있는 이유 / 반대 시나리오"],
  "quotes": ["excerpt 에서 그대로 복사한 핵심 문장 1", "..."]
}"""


def _verify_quotes(quotes: list[str], text: str) -> tuple[list[str], list[str]]:
    norm = re.sub(r"\s+", " ", text)
    ok, bad = [], []
    for q in quotes:
        qn = re.sub(r"\s+", " ", q).strip()
        (ok if qn and qn in norm else bad).append(q)
    return ok, bad


_SCALE = {"조": 10**12, "억": 10**8, "백만": 10**6, "만": 10**4, "천": 10**3}
_NUM_TOKEN = re.compile(r"\d[\d,]*(?:\.\d+)?")
_NUM_WITH_UNIT = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(조|억|백만|만|천)?")


def _source_numbers(text: str) -> set[str]:
    """원문의 숫자 토큰 집합 (콤마 제거, 정수형 문자열). '300억원' 같이 단위 붙은 표기는 환산값도 포함."""
    out = set()
    for m in _NUM_WITH_UNIT.finditer(text):
        raw, unit = m.group(1).replace(",", ""), m.group(2)
        out.add(raw)
        try:
            val = float(raw)
        except ValueError:
            continue
        if unit and val * _SCALE[unit] == int(val * _SCALE[unit]):
            out.add(str(int(val * _SCALE[unit])))
    return out


def _verify_numbers(analysis: dict, text: str) -> list[str]:
    """
    해석문에 등장하는 숫자가 사실팩(원문) 안에 '토큰 단위로' 존재하는지 검사.
    - 부분 문자열 매칭 금지 (300 ⊂ 30000000000 같은 오탐 방지)
    - '300억' 처럼 한글 단위가 붙으면 환산값(30,000,000,000)이 원문에 있으면 통과
    - 1~2자리 숫자(1상, 3자배정, 70% 등)는 검사 제외
    """
    src = _source_numbers(text)
    fields = [analysis.get("headline", ""), analysis.get("summary", ""),
              *analysis.get("reasons", []), *analysis.get("watch_points", [])]
    missing = []
    for f in fields:
        for m in _NUM_WITH_UNIT.finditer(f):
            raw, unit = m.group(1), m.group(2)
            n = raw.replace(",", "")
            if len(n.replace(".", "")) < 3:
                continue
            ok = n in src
            if not ok and unit:
                try:
                    scaled = float(n) * _SCALE[unit]
                    ok = str(int(scaled)) in src
                except ValueError:
                    pass
            if not ok:
                missing.append(raw + (unit or ""))
    return sorted(set(missing))


def interpret(pack: FactPack, model: str | None = None) -> dict:
    """Anthropic API 로 해석. ANTHROPIC_API_KEY 필요. 반환: 스키마 JSON + 검증 결과."""
    try:
        import anthropic
    except ImportError as e:
        raise RuntimeError("pip install anthropic 이 필요합니다.") from e

    client = anthropic.Anthropic()
    model = model or os.environ.get("ANALYZER_MODEL", "claude-sonnet-5-5")
    msg = client.messages.create(
        model=model, max_tokens=2000, temperature=0,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": "fact pack:\n" + pack.to_prompt_json()}],
    )
    raw = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    try:
        analysis = json.loads(raw)
    except json.JSONDecodeError:
        analysis = {"headline": "(파싱 실패)", "summary": raw[:800], "signal": "판단보류",
                    "confidence": "낮음", "reasons": [], "watch_points": [], "risks": [], "quotes": []}

    ok, bad = _verify_quotes(analysis.get("quotes", []), pack.excerpt)
    source = pack.excerpt + " " + json.dumps(pack.financial_summary, ensure_ascii=False) \
             + " " + json.dumps(pack.structured or {}, ensure_ascii=False)
    missing_nums = _verify_numbers(analysis, source)
    analysis["quotes"] = ok
    analysis["_verification"] = {
        "rejected_quotes": bad,
        "numbers_not_in_source": missing_nums,
        "passed": not bad and not missing_nums,
    }
    if not analysis["_verification"]["passed"]:
        analysis["confidence"] = "낮음"
    return analysis
