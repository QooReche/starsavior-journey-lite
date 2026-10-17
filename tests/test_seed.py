# -*- coding: utf-8 -*-
import json
import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import jh_db  # noqa: E402
import jh_v2  # noqa: E402
import make_seed  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
REAL_SEED = os.path.join(ROOT, "assets", "seed")
FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def small_seed(dst):
    """픽스처로 만든 작은 시드 폴더"""
    os.makedirs(os.path.join(dst, "v2"))
    shutil.copy(os.path.join(FIX, "journey", "ko", "journey.json"), os.path.join(dst, "journey.json"))
    shutil.copy(os.path.join(FIX, "arcanas", "arcanas.json"), os.path.join(dst, "arcanas.json"))
    for rel in jh_v2.V2_FILES.values():
        shutil.copy(os.path.join(FIX, "v2", rel), os.path.join(dst, "v2", rel))
    return str(dst)


class Resp:
    def __init__(self, code, content=b"", headers=None):
        self.status_code, self.content, self.headers = code, content, headers or {}


class Sess:
    def __init__(self):
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append((url, dict(headers or {})))
        if "/json/" in url or "pages.dev" in url:
            rel = url.rsplit("/", 1)[1]
            p = os.path.join(FIX, "v2", rel)
        elif url.endswith("arcanas.json"):
            p = os.path.join(FIX, "arcanas", "arcanas.json")
        else:
            p = os.path.join(FIX, "journey", "ko", "journey.json")
        with open(p, "rb") as f:
            return Resp(200, f.read(), {"ETag": '"x"'})


def test_no_cache_uses_seed_without_meta_and_first_update_unconditional(tmp_path):
    seed = small_seed(tmp_path / "seed")
    cache = tmp_path / "cache"
    s = Sess()
    st = jh_db.DbStore(str(cache), session=s, seed_dir=seed)
    j, a = st.load()
    assert j and a and st.seed_used and st.using_seed
    assert st.load_v2()["journeys"] and st.v2_seed_used
    assert not (cache / "meta.json").exists() and not (cache / "v2" / "meta.json").exists()
    assert not st.has_cache()
    r = st.update()
    assert r.status == "updated"
    assert s.calls and all("If-None-Match" not in h and "If-Modified-Since" not in h for _u, h in s.calls)
    assert st.update_v2().status == "updated"
    assert all("If-None-Match" not in h for _u, h in s.calls)
    st.load(), st.load_v2()
    assert not st.using_seed and st.has_cache()


def test_cache_takes_priority(tmp_path):
    seed = small_seed(tmp_path / "seed")
    cache = tmp_path / "cache"
    st = jh_db.DbStore(str(cache), session=Sess(), seed_dir=seed)
    st.update(), st.update_v2()
    # 시드 쪽을 망가뜨려도 캐시가 있으면 캐시를 쓴다
    with open(os.path.join(seed, "journey.json"), "w") as f:
        f.write("{}")
    st2 = jh_db.DbStore(str(cache), session=Sess(), seed_dir=seed)
    assert st2.load()[0] and not st2.using_seed


def test_broken_cache_falls_back_and_first_update_unconditional(tmp_path):
    seed = small_seed(tmp_path / "seed")
    cache = tmp_path / "cache"
    s = Sess()
    st = jh_db.DbStore(str(cache), session=s, seed_dir=seed)
    st.update()
    (cache / "journey" / "ko" / "journey.json").write_text("<html>broken", encoding="utf-8")
    st = jh_db.DbStore(str(cache), session=s, seed_dir=seed)
    j, _a = st.load()
    assert j and st.seed_used
    s.calls.clear()
    st.meta["last_check"] = st.clock()          # 방금 확인한 것으로 되어 있어도, 캐시가 깨졌으면 대기하지 않는다
    assert st.update().status in ("updated", "not_modified")
    assert "If-None-Match" not in s.calls[0][1]          # 깨진 파일에 대해 조건부 요청 금지
    assert jh_db.DbStore._read_valid(str(cache / "journey" / "ko" / "journey.json"), "journey")


def test_v2_broken_cache_falls_back(tmp_path):
    seed = small_seed(tmp_path / "seed")
    cache = tmp_path / "cache"
    v2d = cache / "v2"
    v2d.mkdir(parents=True)
    (v2d / "journeys.json").write_text("nope", encoding="utf-8")
    st = jh_v2.V2Store(str(cache), seed_dir=os.path.join(seed, "v2"))
    assert st.load()["journeys"] and st.seed_used
    assert not (v2d / "meta.json").exists()


def test_lang_en_never_uses_seed(tmp_path):
    seed = small_seed(tmp_path / "seed")
    st = jh_db.DbStore(str(tmp_path / "c"), session=Sess(), lang="en", seed_dir=seed)
    assert st.load() == (None, None) and not st.using_seed
    assert st.load_v2() is None


def test_seed_is_baseline_for_shrink_guard(tmp_path):
    seed = small_seed(tmp_path / "seed")
    # 시드를 크게 만들어(이벤트 8개) 사이트 응답(5개)이 절반 미만이 되게 한다
    p = os.path.join(seed, "journey.json")
    d = json.load(open(p, encoding="utf-8"))
    d["events"] = d["events"] * 4
    json.dump(d, open(p, "w", encoding="utf-8"))
    st = jh_db.DbStore(str(tmp_path / "c"), session=Sess(), seed_dir=seed)
    r = st.update()
    assert r.status == "offline" and "감소" in r.message
    assert not st.has_cache()


def test_real_seed_files_valid():
    if not os.path.isdir(REAL_SEED):
        pytest.skip("no seed")
    st = jh_db.DbStore(os.path.join(REAL_SEED, "_none_"), seed_dir=REAL_SEED)
    j, a = st.load()
    v2 = st.load_v2()
    assert j and a and v2 and st.seed_used and st.v2_seed_used
    assert len(jh_db.build_event_cache(j, a, v2=v2)) > 100
    assert os.path.isfile(os.path.join(REAL_SEED, "SEED_INFO.txt"))
    assert not os.path.exists(os.path.join(REAL_SEED, "meta.json"))
    assert not os.path.exists(os.path.join(REAL_SEED, "_none_"))
    total = sum(os.path.getsize(os.path.join(dp, f)) for dp, _d, fs in os.walk(REAL_SEED) for f in fs)
    assert total < 6 * 1024 * 1024


def _cache_from_fixtures(dst):
    os.makedirs(os.path.join(dst, "journey", "ko"))
    os.makedirs(os.path.join(dst, "arcanas"))
    os.makedirs(os.path.join(dst, "v2"))
    shutil.copy(os.path.join(FIX, "journey", "ko", "journey.json"), os.path.join(dst, "journey", "ko", "journey.json"))
    shutil.copy(os.path.join(FIX, "arcanas", "arcanas.json"), os.path.join(dst, "arcanas", "arcanas.json"))
    for rel in jh_v2.V2_FILES.values():
        shutil.copy(os.path.join(FIX, "v2", rel), os.path.join(dst, "v2", rel))
    return str(dst)


def test_make_seed_accepts_valid_and_rejects_broken(tmp_path):
    cache = _cache_from_fixtures(tmp_path / "cache")
    out = tmp_path / "out"
    assert make_seed.main(["--from", cache, "--out", str(out)]) == 0
    assert (out / "SEED_INFO.txt").exists() and (out / "v2" / "journeys.json").exists()
    assert not (out / "meta.json").exists()
    # 깨진 캐시 거부 + 출력 없음
    with open(os.path.join(cache, "v2", "journeys.json"), "w") as f:
        f.write("<html>")
    out2 = tmp_path / "out2"
    assert make_seed.main(["--from", cache, "--out", str(out2)]) == 2
    assert not out2.exists()
    # 파일 누락도 거부
    os.remove(os.path.join(cache, "arcanas", "arcanas.json"))
    assert make_seed.main(["--from", cache, "--out", str(out2)]) == 2
