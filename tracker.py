"""
신호 적중 추적

기록: state/signals.jsonl — 리스크 보드가 만들어질 때마다 종목별 한 줄
  {"date","code","name","close","flags":[{"lvl","title"}], "max_lvl"}
평가: 신호가 '처음 켜진 날'(그 제목이 직전 기록에 없던 날)을 이벤트로 보고
      +5·+10·+20 거래일 뒤 수익률을 신호 유형별로 집계. 비교군 = 같은 날 신호 없는 종목.

  python tracker.py log   reports/stocks   state/signals.jsonl   # riskboard 뒤에 자동 호출됨
  python tracker.py eval  state/signals.jsonl  reports/tracker.md

판단이 아니라 통계. 표본이 수십 건은 쌓여야 의미가 있으므로 그 전엔 '표본 부족'으로 표시.
"""
from __future__ import annotations

import json
import re
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
    load_dotenv("dart.env")
except ImportError:
    pass

HORIZONS = (5, 10, 20)
MIN_N = 5


# ------------------------------------------------------------------ 기록
def log_from_boards(stocks_dir: Path, out_path: Path, today: str | None = None) -> int:
    """reports/stocks/*.md 의 '주의 신호' 표와 헤더 종가를 읽어 한 줄씩 추가."""
    today = today or f"{date.today():%Y%m%d}"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    existing = set()
    if out_path.exists():
        for ln in out_path.read_text("utf-8").splitlines():
            try:
                r = json.loads(ln)
                existing.add((r["date"], r["code"]))
            except (ValueError, KeyError):
                pass
    n = 0
    with out_path.open("a", encoding="utf-8") as f:
        for md in sorted(stocks_dir.glob("*.md")):
            text = md.read_text("utf-8")
            m = re.match(r"# (.+?) \((\d{6}|-)\) 공시 리스크 보드", text)
            if not m or m.group(2) == "-":
                continue
            name, code = m.group(1), m.group(2)
            if (today, code) in existing:
                continue
            pm = re.search(r"\*\*([\d,]+)원\*\* ([+-][\d.]+%)? ?\((\d{4}-\d{2}-\d{2}) 종가\)", text)
            close = int(pm.group(1).replace(",", "")) if pm else None
            halted = "**거래정지**" in text[:1500]
            flags = []
            for ln in text.splitlines():
                if ln.startswith("| 🔴") or ln.startswith("| 🟠") or ln.startswith("| 🟡"):
                    cells = [c.strip() for c in ln.strip("|").split("|")]
                    flags.append({"lvl": cells[0], "title": cells[1].strip("*")})
            max_lvl = "🔴" if any(x["lvl"] == "🔴" for x in flags) else "🟠" if any(x["lvl"] == "🟠" for x in flags) else "🟡" if flags else ""
            f.write(json.dumps({"date": today, "code": code, "name": name, "close": close, "halted": halted,
                                "flags": flags, "max_lvl": max_lvl}, ensure_ascii=False) + "\n")
            n += 1
    return n


# ------------------------------------------------------------------ 평가
def _family(title: str) -> str:
    """신호 제목 → 유형 (숫자 제거해서 묶음)"""
    t = re.sub(r"[\d,.%]+", "", title)
    for key, fam in (("감사의견", "감사의견 비적정"), ("계속기업", "계속기업 불확실성"), ("자본잠식", "자본잠식"),
                     ("CB·BW 잔여", "CB 잠재주식"), ("유상증자 누적", "유상증자 누적"), ("유상증자 건", "유상증자 반복"),
                     ("담보 제공", "최대주주 담보"), ("담보계약 이후", "담보 후 주가하락"), ("최대주주 변경", "최대주주 변경"),
                     ("불성실공시", "불성실공시"), ("회생", "회생·부도"), ("횡령", "횡령·배임"), ("거래정지", "거래정지"),
                     ("연속 영업적자", "연속 영업적자"), ("영업적자", "분기 영업적자"), ("매출 전년", "매출 급감"),
                     ("부채비율 업종", "부채비율 업종하위"), ("부채비율", "부채비율 과다"), ("영업이익률 업종", "이익률 업종하위"),
                     ("리픽싱 하한", "CB 하한근접"), ("현재가 < 전환가", "CB 전환가 상회"), ("현재가 > 전환가", "CB 전환가능"),
                     ("진행 중 유상증자", "유증 발행가 > 현재가"), ("주식병합", "주식병합·분할"), ("최대주주 측 지분", "최대주주 지분 낮음")):
        if key in title:
            return fam
    return t.strip()[:20]


def load(path: Path) -> list[dict]:
    rows = []
    for ln in path.read_text("utf-8").splitlines():
        try:
            rows.append(json.loads(ln))
        except ValueError:
            pass
    rows.sort(key=lambda r: (r["code"], r["date"]))
    return rows


def _trading_days_later(series: list[dict], i: int, k: int) -> dict | None:
    """series(같은 종목, 날짜순)에서 i 번째 기록의 k 거래일(=기록일) 뒤 기록. 매일 1회 기록 가정(주말 제외 자동)."""
    j = i + k
    return series[j] if j < len(series) else None


def evaluate(rows: list[dict]) -> dict:
    by_code: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_code[r["code"]].append(r)
    events = []          # (family, lvl, code, date, close, {h: ret})
    controls = []        # 신호 없는 날의 기준 수익률 (code, date, {h: ret})
    for code, series in by_code.items():
        seen: set[str] = set()
        for i, r in enumerate(series):
            if not r.get("close") or r.get("halted"):
                seen = {f["title"] for f in r.get("flags", [])} | seen
                continue
            titles = {f["title"] for f in r.get("flags", [])}
            fwd = {}
            for h in HORIZONS:
                later = _trading_days_later(series, i, h)
                if later and later.get("close"):
                    fwd[h] = round((later["close"] / r["close"] - 1) * 100, 2)
            if not titles:
                controls.append((code, r["date"], fwd))
            for f in r.get("flags", []):
                if f["title"] in seen:
                    continue                                   # 이미 켜져 있던 신호
                events.append((_family(f["title"]), f["lvl"], code, r["name"], r["date"], r["close"], fwd))
            seen |= titles
    # 집계
    fam_stats: dict[str, dict] = {}
    for fam, lvl, code, name, d, c, fwd in events:
        st = fam_stats.setdefault(fam, {"lvl": lvl, "n": 0, "rets": {h: [] for h in HORIZONS}, "examples": []})
        st["n"] += 1
        for h, v in fwd.items():
            st["rets"][h].append(v)
        if len(st["examples"]) < 5:
            st["examples"].append(f"{name} {d[:4]}-{d[4:6]}-{d[6:]}")
    ctrl = {h: [fwd[h] for _, _, fwd in controls if h in fwd] for h in HORIZONS}
    return {"events": len(events), "controls": len(controls), "fam": fam_stats, "ctrl": ctrl,
            "first": min((r["date"] for r in rows), default=""), "last": max((r["date"] for r in rows), default=""),
            "days": len({r["date"] for r in rows}), "codes": len(by_code)}


def _m(vals):
    return f"{statistics.median(vals):+.1f}%" if vals else "-"


def _hit(vals, direction=-1):
    """음수 수익률 비율(위험 신호는 하락이 '적중')"""
    if not vals:
        return "-"
    k = sum(1 for v in vals if (v < 0 if direction < 0 else v > 0))
    return f"{k}/{len(vals)}"


def render(ev: dict) -> str:
    L = [f"# 신호 적중 추적", f"기록 {ev['first'][:4]}-{ev['first'][4:6]}-{ev['first'][6:]} ~ {ev['last'][:4]}-{ev['last'][4:6]}-{ev['last'][6:]} · "
         f"{ev['days']}일 · {ev['codes']}종목 · 신규 신호 이벤트 {ev['events']}건 · 비교군(신호 없는 종목-일) {ev['controls']}건", "",
         "> 신호가 **처음 켜진 날** 종가 대비 N거래일 뒤 수익률의 중앙값. '하락 적중'은 N일 뒤 수익률이 음수인 비율. "
         "표본이 적으면(＜5) 통계로 읽지 말 것. 미래 수익을 보장하지 않으며, 신호의 유용성을 사후 점검하는 용도.", ""]
    L += ["## 비교군 (신호 없는 종목-일의 N거래일 뒤 수익률 중앙값)", "",
          "| +5일 | +10일 | +20일 | 표본 |", "|---:|---:|---:|---:|",
          f"| {_m(ev['ctrl'][5])} | {_m(ev['ctrl'][10])} | {_m(ev['ctrl'][20])} | {len(ev['ctrl'][5])}/{len(ev['ctrl'][10])}/{len(ev['ctrl'][20])} |", ""]
    L += ["## 신호 유형별", "", "| 신호 | 등급 | 건수 | +5일 | +10일 | +20일 | 하락 적중(+20) | 예시 |", "|---|---|---:|---:|---:|---:|---:|---|"]
    order = {"🔴": 0, "🟠": 1, "🟡": 2}
    for fam, st in sorted(ev["fam"].items(), key=lambda kv: (order.get(kv[1]["lvl"], 9), -kv[1]["n"])):
        r = st["rets"]
        tag = " _(표본 부족)_" if st["n"] < MIN_N else ""
        L.append(f"| {fam}{tag} | {st['lvl']} | {st['n']} | {_m(r[5])} | {_m(r[10])} | {_m(r[20])} | {_hit(r[20])} | {', '.join(st['examples'][:3])} |")
    if not ev["fam"]:
        L.append("| _아직 신규 신호 이벤트 없음_ | | | | | | | |")
    L += ["", "주의: 기록 첫날의 신호는 전부 '신규'로 잡히므로 첫 2주는 집계에서 과대평가될 수 있음. 거래정지 종목은 수익률 계산에서 제외.", ""]
    return "\n".join(L)


def main(argv=None):
    argv = argv or sys.argv[1:]
    if not argv or argv[0] not in ("log", "eval"):
        print(__doc__)
        return 1
    if argv[0] == "log":
        n = log_from_boards(Path(argv[1]), Path(argv[2]))
        print(f"signals logged: {n}")
        return 0
    rows = load(Path(argv[1]))
    out = Path(argv[2])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(evaluate(rows)), "utf-8")
    print(f"tracker: {len(rows)} rows → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
