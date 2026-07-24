# -*- coding: utf-8 -*-
"""
collector.py — 문화체육관광부 문화예술공연(통합) OpenAPI → events.db 수집기.

API: 「문화체육관광부_문화예술공연(통합)」 (kcisa CNV_060)
  엔드포인트: https://api.kcisa.kr/openapi/CNV_060/request
  필수 파라미터: serviceKey, dtype(분류명: 연극/뮤지컬/오페라/음악/콘서트/국악/무용/전시/기타),
                title(2자 이상 검색어)
  선택: numOfRows, pageNo / 응답: XML (날짜구간 파라미터 없음 → 응답의 공연기간을 파싱)

⚠️ title이 필수이고 날짜 필터가 없으므로, '무용' 등 dtype에 대해 여러 '시드 키워드'로
   반복 검색해 모읍니다. 응답 XML의 정확한 필드명은 키 승인 후 실제 응답으로 최종 확정합니다
   (아래 후보 태그명으로 관대하게 매핑 — 실데이터 확인 시 _FIELD_CANDIDATES만 손보면 됨).
"""

import re
import hashlib
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import date

import events_db as db

KCISA_URL = "https://api.kcisa.kr/openapi/CNV_060/request"
UA = "blog-studio/1.0"

# dtype(분류명) → events.db의 (type, category)
DTYPE_MAP = {
    "무용": ("performance", "dance"),
    "연극": ("performance", "theater"),
    "뮤지컬": ("performance", "musical"),
    "오페라": ("performance", "opera"),
    "음악": ("performance", "music"),
    "콘서트": ("performance", "music"),
    "국악": ("performance", "gugak"),
    "전시": ("exhibition", "art"),
    "기타": ("performance", "etc"),
}

# 무용 위주 수집용 기본 시드 키워드(title 필수 제약 대응 — 폭넓게 긁어 모음).
DEFAULT_DANCE_SEEDS = [
    "무용", "발레", "현대무용", "한국무용", "전통무용", "춤", "댄스",
    "무용제", "무용축제", "댄스페스티벌",
]

# 응답 필드 후보(대문자 비교). 실제 응답 확인 후 필요시 보강.
_FIELD_CANDIDATES = {
    "title":   ["TITLE", "PRFNM", "SUBJECT", "NAME"],
    "id":      ["LOCAL_ID", "ID", "SEQ", "CONTENTID", "MT20ID"],
    "url":     ["URL", "REFERENCE_IDENTIFIER", "HOMEPAGE"],
    "image":   ["IMAGE_OBJECT", "IMAGE_URL", "IMG", "THUMBNAIL", "POSTER"],
    "venue":   ["EVENT_SITE", "SPATIAL_COVERAGE", "PLACE", "FCLTYNM", "VENUE", "LOCATION"],
    "period":  ["PERIOD", "DURATION", "TEMPORAL_COVERAGE", "EVENT_PERIOD", "PRFPDFROM"],
    "charge":  ["CHARGE", "PRICE", "PRICES", "FEE"],
    "desc":    ["DESCRIPTION", "SUB_DESCRIPTION", "CONTENTS", "INTRO"],
    "genre":   ["GENRE", "REALM_NAME", "REALMNAME", "CATEGORY", "DTYPE"],
    "region":  ["REGION", "AREANM", "SIDO", "ADDRESS"],
}


def _get(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def fetch_raw(key: str, dtype: str, title: str, rows: int = 50, page: int = 1) -> str:
    """CNV_060 호출 → XML 문자열. serviceKey는 그대로 전달(인코딩 키도 허용)."""
    qs = urllib.parse.urlencode({
        "serviceKey": key, "dtype": dtype, "title": title,
        "numOfRows": str(rows), "pageNo": str(page),
    })
    return _get(KCISA_URL + "?" + qs)


def _localname(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def _parse_items(xml_text: str) -> list:
    """응답에서 반복되는 항목을 찾아 {태그(대문자): 값} dict 목록으로. 태그명에 둔감하게 동작."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    items = []
    for el in root.iter():
        # 'TITLE' 자식을 가진 요소를 하나의 항목으로 본다.
        children = list(el)
        if not children:
            continue
        names = {_localname(c.tag).upper() for c in children}
        if "TITLE" in names or "PRFNM" in names or "SUBJECT" in names:
            d = {}
            for c in children:
                d[_localname(c.tag).upper()] = (c.text or "").strip()
            items.append(d)
    return items


def _pick(d: dict, key: str) -> str:
    for cand in _FIELD_CANDIDATES.get(key, []):
        if d.get(cand):
            return d[cand]
    return ""


def _parse_period(s: str):
    """'2026.05.31~2026.06.07' / '20260531~20260607' / '2026-05-31' → (start_iso, end_iso)."""
    if not s:
        return None, None
    nums = re.findall(r"(\d{4})[.\-/]?\s*(\d{1,2})[.\-/]?\s*(\d{1,2})", s)
    def iso(t):
        y, m, d = t
        return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
    if not nums:
        return None, None
    start = iso(nums[0])
    end = iso(nums[1]) if len(nums) > 1 else None
    return start, end


def _mk_id(title: str, start: str, url: str) -> str:
    base = (url or "") + "|" + (title or "") + "|" + (start or "")
    return "ev-" + hashlib.md5(base.encode("utf-8")).hexdigest()[:16]


def _to_event(item: dict, dtype: str) -> dict:
    title = _pick(item, "title")
    if not title:
        return None
    start, end = _parse_period(_pick(item, "period"))
    if not start:
        return None                       # 날짜를 못 구하면 시기별 트리거에 못 씀 → 제외
    typ, cat = DTYPE_MAP.get(dtype, ("performance", "etc"))
    genre = _pick(item, "genre") or dtype
    if any(k in (title + genre) for k in ("축제", "페스티벌", "Festival", "무용제")):
        typ = "festival"
    url = _pick(item, "url")
    charge = _pick(item, "charge")
    price = "free" if charge and re.search(r"무료|free", charge, re.I) else ("paid" if charge else "")
    ext_id = _pick(item, "id")
    return {
        "id": _mk_id(title, start, url or ext_id),
        "title_ko": title,
        "title_en": "",
        "type": typ,
        "category": cat,
        "start_date": start,
        "end_date": end,
        "venue": _pick(item, "venue"),
        "region": _pick(item, "region"),
        "price": price,
        "booking_url": url,
        "source": "culture-api(CNV_060)",
        "image_url": _pick(item, "image"),
        "description": _pick(item, "desc"),
        "importance": 3,
        "collected_at": date.today().isoformat(),
    }


def collect(settings: dict, log=print, dtypes=("무용",), titles=None,
            rows: int = 50, max_pages: int = 1, path=None) -> int:
    """문화예술공연(통합) API로 이벤트를 모아 events.db에 저장. 저장(신규/갱신) 건수 반환."""
    key = (settings.get("culture_api_key") or "").strip()
    if not key:
        raise RuntimeError("문화예술공연 API 서비스키가 없습니다. [⚙️ 설정]에 입력하세요.")
    titles = list(titles or DEFAULT_DANCE_SEEDS)
    db.init_db(path)
    seen, saved = set(), 0
    for dtype in dtypes:
        for title in titles:
            for page in range(1, max_pages + 1):
                try:
                    xml = fetch_raw(key, dtype, title, rows, page)
                except Exception as e:
                    log(f"   ⚠️ 요청 실패({dtype}/{title} p{page}): {e}")
                    break
                items = _parse_items(xml)
                if not items:
                    break
                for it in items:
                    ev = _to_event(it, dtype)
                    if not ev or ev["id"] in seen:
                        continue
                    seen.add(ev["id"])
                    db.upsert_event(ev, path)
                    saved += 1
            log(f"   · '{dtype}/{title}' 처리")
    log(f"   ✅ 이벤트 수집 완료 — {saved}건 저장(중복 제외)")
    return saved
