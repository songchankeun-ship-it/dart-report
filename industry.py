"""
업종 비교용 데이터
  1) 상장사 업종 지도  (.dart_cache/industry_map.json)  — company.json 을 상장사 전부에 1회 호출, 이후 30일 캐시
  2) 동종업종 재무 스냅샷 — fnlttMultiAcnt(다중회사 주요계정) 로 50곳씩 묶어 최근 사업보고서 값 수집, 업종별 캐시

  python industry.py build            # 업종 지도 생성/갱신 (처음 60~90분, 하루 한도 4,000건 사용)
  python industry.py peers 005930     # 같은 업종 상장사와 비교표 출력

업종 코드: DART company.json 의 induty_code (한국표준산업분류 세세분류, 5자리 또는 축약).
비교 단위: 세세분류(5자리) 동일 → 10곳 미만이면 세분류(4자리) → 그래도 적으면 소분류(3자리)로 넓힘.
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from datetime import date, timedelta
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
    load_dotenv("dart.env")
except ImportError:
    pass

from dart_client import DartClient, DartError, CACHE_DIR, REPRT_ANNUAL

MAP_PATH = CACHE_DIR / "industry_map.json"
PEER_DIR = CACHE_DIR / "peers"
MAP_TTL_DAYS = 30
PEER_TTL_DAYS = 7
MIN_PEERS = 15

KEYS = {"매출액": ("매출액", "수익(매출액)", "영업수익"), "영업이익": ("영업이익", "영업이익(손실)"),
        "당기순이익": ("당기순이익", "당기순이익(손실)"), "자산총계": ("자산총계",), "부채총계": ("부채총계",),
        "자본총계": ("자본총계",), "유동자산": ("유동자산",), "유동부채": ("유동부채",)}


def _num(v):
    try:
        return int(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 업종 지도
def load_map() -> dict:
    if MAP_PATH.exists():
        return json.loads(MAP_PATH.read_text("utf-8"))
    return {"built": "", "corps": {}}


def build_map(client: DartClient, max_calls: int | None = None, resume: bool = True) -> dict:
    m = load_map() if resume else {"built": "", "corps": {}}
    listed = [r for r in client.corp_table() if r["stock_code"]]
    todo = [r for r in listed if r["corp_code"] not in m["corps"]]
    print(f"상장사 {len(listed)}곳 중 미수집 {len(todo)}곳")
    client.min_interval = 0.05
    n = 0
    for r in todo:
        if max_calls and n >= max_calls:
            break
        try:
            j = client._get_json("company.json", corp_code=r["corp_code"])
        except DartError as e:
            print("  실패", r["corp_name"], e)
            continue
        m["corps"][r["corp_code"]] = {"name": j.get("corp_name") or r["corp_name"], "stock": r["stock_code"],
                                      "cls": j.get("corp_cls"), "ind": (j.get("induty_code") or "").strip(),
                                      "acc_mt": j.get("acc_mt")}
        n += 1
        if n % 200 == 0:
            m["built"] = f"{date.today():%Y%m%d}"
            MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
            MAP_PATH.write_text(json.dumps(m, ensure_ascii=False), "utf-8")
            print(f"  {n}/{len(todo)} 저장")
    m["built"] = f"{date.today():%Y%m%d}"
    MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
    MAP_PATH.write_text(json.dumps(m, ensure_ascii=False), "utf-8")
    return m


def map_is_fresh(m: dict) -> bool:
    if not m.get("built"):
        return False
    b = date(int(m["built"][:4]), int(m["built"][4:6]), int(m["built"][6:]))
    return (date.today() - b).days <= MAP_TTL_DAYS and len(m["corps"]) >= 2000


def peers_of(m: dict, corp_code: str) -> tuple[list[str], str]:
    me = m["corps"].get(corp_code)
    if not me or not me["ind"]:
        return [], ""
    ind = me["ind"]
    for width in (len(ind), 4, 3):
        pre = ind[:width]
        ps = [k for k, v in m["corps"].items() if v["ind"].startswith(pre) and v["cls"] in ("Y", "K") and k != corp_code]
        if len(ps) >= MIN_PEERS or width == 3:
            return ps, pre
    return [], ind


# ---------------------------------------------------------------- 동종업종 재무
def fetch_peer_financials(client: DartClient, codes: list[str], year: int) -> dict[str, dict]:
    """다중회사 주요계정 → {corp_code: {계정: 값}} (연결 우선, 없으면 개별)."""
    out: dict[str, dict] = {}
    for i in range(0, len(codes), 50):
        chunk = codes[i:i + 50]
        try:
            rows = client._get_json("fnlttMultiAcnt.json", corp_code=",".join(chunk), bsns_year=str(year),
                                    reprt_code=REPRT_ANNUAL).get("list", []) or []
        except DartError:
            rows = []
        for r in rows:
            nm = r.get("account_nm", "").replace(" ", "")
            key = next((k for k, al in KEYS.items() if nm in al), None)
            if not key:
                continue
            d = out.setdefault(r["corp_code"], {"_fs": {}})
            fs = r.get("fs_div")                       # CFS/OFS
            cur = d["_fs"].get(key)
            if cur is None or (cur == "OFS" and fs == "CFS"):
                d[key] = _num(r.get("thstrm_amount"))
                d["_fs"][key] = fs
    return out


def ratios(f: dict) -> dict:
    r = {}
    s, op, ni, a, l, e = (f.get(k) for k in ("매출액", "영업이익", "당기순이익", "자산총계", "부채총계", "자본총계"))
    if s and op is not None:
        r["영업이익률"] = round(op / s * 100, 2)
    if s and ni is not None:
        r["순이익률"] = round(ni / s * 100, 2)
    if e and l is not None and e > 0:
        r["부채비율"] = round(l / e * 100, 1)
    if e and ni is not None and e > 0:
        r["ROE"] = round(ni / e * 100, 2)
    cu, cl = f.get("유동자산"), f.get("유동부채")
    if cu and cl:
        r["유동비율"] = round(cu / cl * 100, 1)
    if e is not None and e <= 0:
        r["자본잠식"] = True
    return r


def peer_snapshot(client: DartClient, m: dict, corp_code: str, year: int | None = None) -> dict:
    """캐시된 업종 스냅샷 반환: {ind, year, peers: {code: {name, fin, ratios}}}"""
    codes, pre = peers_of(m, corp_code)
    if not codes:
        return {}
    year = year or date.today().year - 1
    PEER_DIR.mkdir(parents=True, exist_ok=True)
    cache = PEER_DIR / f"{pre}_{year}.json"
    if cache.exists():
        snap = json.loads(cache.read_text("utf-8"))
        b = date(int(snap["built"][:4]), int(snap["built"][4:6]), int(snap["built"][6:]))
        if (date.today() - b).days <= PEER_TTL_DAYS:
            return snap
    allcodes = codes + [corp_code]
    fin = fetch_peer_financials(client, allcodes, year)
    if sum(1 for k in codes if k in fin) < max(3, len(codes) // 3) and year > 2020:
        fin2 = fetch_peer_financials(client, [k for k in allcodes if k not in fin], year - 1)
        for k, v in fin2.items():
            fin.setdefault(k, {**v, "_year": year - 1})
    snap = {"built": f"{date.today():%Y%m%d}", "ind": pre, "year": year,
            "peers": {k: {"name": m["corps"][k]["name"], "stock": m["corps"][k]["stock"],
                          "fin": {kk: vv for kk, vv in v.items() if not kk.startswith("_")}, "ratios": ratios(v),
                          "year": v.get("_year", year)} for k, v in fin.items() if k in m["corps"]}}
    cache.write_text(json.dumps(snap, ensure_ascii=False), "utf-8")
    return snap


def compare(snap: dict, corp_code: str) -> list[dict]:
    """[{지표, 값, 업종중앙값, 순위, 표본수, 방향}] — 방향: 높을수록 좋음(+)/나쁨(-)"""
    me = snap.get("peers", {}).get(corp_code)
    if not me:
        return []
    out = []
    spec = [("매출액", "fin", "+"), ("영업이익", "fin", "+"), ("당기순이익", "fin", "+"), ("자산총계", "fin", "+"),
            ("영업이익률", "ratios", "+"), ("순이익률", "ratios", "+"), ("ROE", "ratios", "+"),
            ("부채비율", "ratios", "-"), ("유동비율", "ratios", "+")]
    for key, grp, direction in spec:
        vals = [(k, v[grp].get(key)) for k, v in snap["peers"].items() if v[grp].get(key) is not None]
        mine = me[grp].get(key)
        if mine is None or len(vals) < 3:
            continue
        nums = [v for _, v in vals]
        rank = sorted(nums, reverse=(direction == "+")).index(mine) + 1
        out.append({"지표": key, "값": mine, "중앙값": statistics.median(nums), "순위": rank, "표본": len(nums), "방향": direction,
                    "상위%": round(rank / len(nums) * 100)})
    return out


# ---------------------------------------------------------------- CLI
def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["build", "peers"])
    ap.add_argument("corp", nargs="?")
    ap.add_argument("--max-calls", type=int)
    args = ap.parse_args(argv)
    client = DartClient()
    if args.cmd == "build":
        m = build_map(client, args.max_calls)
        print(f"업종 지도: {len(m['corps'])}곳, 업종 {len(set(v['ind'] for v in m['corps'].values()))}개")
        return 0
    m = load_map()
    corp = client.find_corp(args.corp)
    snap = peer_snapshot(client, m, corp["corp_code"])
    if not snap:
        print("업종 지도에 없음 — 먼저 build")
        return 1
    print(f"{corp['corp_name']} 업종 {snap['ind']} 동종 {len(snap['peers'])}곳 ({snap['year']}년 사업보고서)")
    for r in compare(snap, corp["corp_code"]):
        print(f"  {r['지표']:8} {r['값']:>20,} | 중앙값 {r['중앙값']:>20,} | {r['순위']}/{r['표본']}위")
    return 0


if __name__ == "__main__":
    sys.exit(main())
