# -*- coding: utf-8 -*-
"""장비 세트 아이콘(공격/방어/생명/섬멸/속도/장벽/저항/적중/정밀/통찰/투지/파괴): 가이드 사이트에서 지연 다운로드해 캐시한다.

카드 썸네일(jh_cards.CardStore)과 같은 규칙(서킷브레이커, 시간당 상한, 실패/없음 음성 캐시)을 쓴다. 게임 그림을 앱에 넣지 않고,
받지 못하면 글자만 표시한다. 세트 이름은 사이트 목록에 있는 것만 받는다(임의 문자열로 요청하지 않는다).
"""
import os
import re
import urllib.parse

import jh_cards
import jh_net
import jh_paths

ICON_URL = "https://starsavior-guide.github.io/data/EQUIPMENT/{name}.webp"
SET_NAMES = ("공격", "방어", "생명", "섬멸", "속도", "장벽", "저항", "적중", "정밀", "통찰", "투지", "파괴")
ICON_W = 48                                   # 캐시에 저장하는 폭(px): 툴팁에서는 12~18px 로 줄여 쓴다
ICONS_PER_HOUR = 60
_PART = re.compile(r"^([가-힣]+)\s*\(\s*(\d+)\s*\)(.*)$")


def parse_part(text):
    """'공격(4)' -> ('공격', '4', 접미사). 형식이 다르면 None."""
    m = _PART.match((text or "").strip())
    return (m.group(1), m.group(2), m.group(3).strip()) if m else None


def split_set(value):
    """'공격(4) + 투지(2)' -> [('공격','4',''), ('투지','2','')]. 한 부분이라도 형식이 다르면 None(글자 그대로 표시)."""
    parts = [p for p in re.split(r"\s*\+\s*", (value or "").strip()) if p]
    parsed = [parse_part(p) for p in parts]
    return parsed if parsed and all(parsed) else None


def names_in(equipment):
    """장비 데이터에 나오는 세트 이름들(사이트 목록에 있는 것만, 순서 유지)."""
    out = []
    for o in (equipment or {}).get("sets") or []:
        for part in split_set(o.get("value")) or []:
            if part[0] in SET_NAMES and part[0] not in out:
                out.append(part[0])
    return out


class IconStore(jh_cards.CardStore):
    """세트 이름 -> 캐시된 PNG (폭 48px). 이름은 SET_NAMES 에 있는 것만 받는다."""
    REF_W = ICON_W

    def __init__(self, cache_dir=None, **kw):
        kw.setdefault("limiter", jh_net.get_limiter("icon_dl", ICONS_PER_HOUR, 3600))
        super().__init__(cache_dir or jh_paths.ensure_dir(os.path.join(jh_paths.cache_dir(), "seticons")), **kw)

    def _file(self, name):
        return f"{name}.png"

    def _url(self, name):
        if name not in SET_NAMES:
            raise ValueError("unknown set name")
        return ICON_URL.format(name=urllib.parse.quote(name))


_store = None


def default_store():
    global _store
    if _store is None:
        _store = IconStore()
    return _store


def cached_path(name, store=None):
    """캐시된 아이콘 PNG 경로, 없으면 None (다운로드하지 않음). 툴팁용."""
    if name not in SET_NAMES:
        return None
    p = (store or default_store())._path(name)
    return p if os.path.exists(p) else None


def missing(equipment, store=None):
    """아직 캐시에 없고 장기 음성 캐시도 아닌 아이콘 이름들"""
    st = store or default_store()
    return [n for n in names_in(equipment) if not cached_path(n, st) and not st.is_missing(n)]
