# -*- coding: utf-8 -*-
"""
make_seed.py - 사용자/개발자 캐시에서 번들 DB 시드(assets/seed)를 만든다.

사용: python -X utf8 tools/make_seed.py [--from <캐시폴더>] [--out <시드폴더>]
  * 캐시 기본값: 후보(개발자 실행 data/cache, %APPDATA%/JourneyHelperLite/cache) 중 '유효하고 가장 최근' 것
  * 복사 대상: journey/ko/journey.json, arcanas/arcanas.json, v2/{journeys,journey_items,journey_buffs,potentials}.json (meta 제외)
  * 앱의 구조 검증(jh_db.validate_db_file / jh_v2.validate)과 이벤트 캐시 빌드를 통과해야 한다. 깨진 캐시는 거부(종료코드 2).
  * 캐시 폴더는 읽기만 한다. 생성 정보는 SEED_INFO.txt 에 기록.
"""
import argparse
import datetime
import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import jh_db  # noqa: E402
import jh_v2  # noqa: E402

# (종류, 캐시 상대경로, 시드 상대경로)
FILES = [("journey", "journey/ko/journey.json", "journey.json"),
         ("arcanas", "arcanas/arcanas.json", "arcanas.json")] + \
        [(n, "v2/" + rel, "v2/" + rel) for n, rel in jh_v2.V2_FILES.items()]


class SeedError(Exception):
    pass


def load_and_validate(cache):
    """캐시 폴더에서 시드 대상 파일을 읽어 검증. -> {시드 상대경로: (원본경로, 데이터)}. 문제 있으면 SeedError."""
    out = {}
    for kind, rel, seed_rel in FILES:
        path = os.path.join(cache, *rel.split("/"))
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if kind in ("journey", "arcanas"):
                jh_db.validate_db_file(kind, data)
            else:
                jh_v2.validate(kind, data)
        except FileNotFoundError:
            raise SeedError(f"{rel}: 파일 없음")
        except Exception as e:
            raise SeedError(f"{rel}: {type(e).__name__} {str(e)[:100]}")
        out[seed_rel] = (path, data)
    try:      # 앱이 실제로 쓰는 변환까지 성공해야 한다
        events = jh_db.build_event_cache(out["journey.json"][1], out["arcanas.json"][1],
                                         v2={k: out["v2/" + r][1] for k, r in jh_v2.V2_FILES.items()})
    except Exception as e:
        raise SeedError(f"이벤트 캐시 빌드 실패: {type(e).__name__} {str(e)[:100]}")
    if not events:
        raise SeedError("이벤트가 0개")
    return out, len(events)


def candidate_caches():
    c = [os.path.join(ROOT, "data", "cache")]
    ap = os.environ.get("APPDATA")
    if ap:
        c.append(os.path.join(ap, "JourneyHelperLite", "cache"))
    return c


def newest_valid_cache():
    best = None
    for c in candidate_caches():
        try:
            out, _ = load_and_validate(c)
        except SeedError:
            continue
        mt = max(os.path.getmtime(p) for p, _d in out.values())
        if best is None or mt > best[0]:
            best = (mt, c)
    if best is None:
        raise SeedError("유효한 캐시를 찾지 못했습니다 (--from 으로 지정)")
    return best[1]


def make_seed(cache, out_dir, today=None):
    files, n_events = load_and_validate(cache)     # 검증 실패 시 out_dir 에 아무것도 쓰지 않는다
    os.makedirs(os.path.join(out_dir, "v2"), exist_ok=True)
    lines, total = [], 0
    for seed_rel, (src, _d) in files.items():
        dst = os.path.join(out_dir, *seed_rel.split("/"))
        shutil.copyfile(src, dst)
        size = os.path.getsize(dst)
        total += size
        lines.append(f"  {seed_rel}: {size:,} bytes")
    info = [f"generated: {today or datetime.date.today().isoformat()}",
            f"events (app cache count): {n_events}",
            f"total: {total:,} bytes", "files:"] + lines + \
           ["", "Source: starsavior-guide.github.io / starsavior-journey-choice.pages.dev (ko). Used only when no user cache exists."]
    with open(os.path.join(out_dir, "SEED_INFO.txt"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(info) + "\n")
    return n_events, total


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--from", dest="src", help="캐시 폴더 (기본: 가장 최근 유효한 캐시)")
    ap.add_argument("--out", default=os.path.join(ROOT, "assets", "seed"))
    a = ap.parse_args(argv)
    try:
        src = a.src or newest_valid_cache()
        n, total = make_seed(src, a.out)
    except SeedError as e:
        print(f"[make_seed] 거부: {e}", file=sys.stderr)
        return 2
    print(f"[make_seed] {src} -> {a.out}: 이벤트 {n}개, {total:,} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
