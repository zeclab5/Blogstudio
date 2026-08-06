# -*- coding: utf-8 -*-
"""
image_finder.py — 글 주제에 맞는 '무료·저작권 안전' 이미지를 찾아주는 모듈.

세 블로그 콘텐츠 전략(v6)의 이미지 원칙을 따릅니다:
  · 직접 촬영 사진이 1순위(이 모듈은 '사진이 없을 때'의 보조 수단).
  · 실제 대상(장소·문화재·작품)은 공개 CC 자료(Openverse·Wikimedia)에서 찾고 출처를 표기.
  · 순수 개념·다이어그램만 AI 생성(현재 무료 무키 경로가 막혀 보류 — 키 입력 시 사용).

핵심 함수
  find_images(query, n, settings)   → 정규화된 후보 이미지 리스트
  attribution_caption(item, lang)   → 출처·라이선스 캡션 문자열

각 후보(dict) 형식:
  {url, thumb, title, source, source_page, license, creator, attribution}

키가 필요 없는 소스(Openverse·Wikimedia·LOC·미술관 2곳·국가유산청)만으로 바로 동작합니다.
Pexels/Pixabay/Unsplash/공유마당/위키미디어 공식API/TourAPI는 settings에 무료 API 키가
있을 때만 추가로 사용합니다.

⚠️ 공유마당(search_gongu)은 data.go.kr 키가 있어도 그 API 자체에 대한 개별
'활용신청' 승인이 따로 나야 동작합니다(같은 계정 키라도 API별로 승인 상태가 다름).
승인 전에는 "SERVICE KEY IS NOT REGISTERED ERROR" 응답으로 결과 0개가 됩니다.

국가유산청(search_heritage)은 2026-06-24 확인 결과 khs.go.kr 도메인에서 키 없이
바로 동작함을 실응답으로 확인했습니다(예전 cha.go.kr 도메인·키 필요 가정은 폐기).
"""

import re
import json
import urllib.request
import urllib.parse
import urllib.error
import xml.etree.ElementTree as ET

UA = "blog-studio/1.0 (https://k-arts-travel.blogspot.com; contact: zeclab5@gmail.com)"

# 블로그(광고 가능)에 안전하게 쓸 수 있는 라이선스 — 상업적 사용·2차 변형 허용 위주.
_SAFE_OPENVERSE_LICENSES = "cc0,pdm,by,by-sa"

# 한국 관련성 신호 — 점수 가산용(제목·창작자·출처에서 매칭)
_KO_TOKENS = (
    "korea", "korean", "joseon", "goryeo", "silla", "baekje", "hanok", "minhwa",
    "buddhism", "buddhist", "seoul", "busan", "gyeongju", "jeju", "jeonju",
    "gangneung", "andong", "suwon", "incheon", "gwangju", "daegu", "ulsan",
    "한국", "조선", "고려", "신라", "백제", "한옥", "민화", "사찰",
)
# 명백한 비한국(노이즈) 신호 — 점수 감산
_NEG_TOKENS = ("japan", "japanese", "china", "chinese", "vietnam", "thailand",
               "europe", "european", "america", "africa", "egypt", "rome")

# 매칭 안 돼도 '그나마 비슷한 것'을 항상 반환하는 느슨한 검색 소스.
# 한국 신호가 전혀 없으면(중립 0점) 노이즈로 간주해 제외(find_images 참고).
_LOOSE_MATCH_SOURCES = {"Art Institute of Chicago", "The Met"}


# 검색어 키워드 매칭에서 신호로 안 쳐주는 범용 단어(따로 _KO_TOKENS로 가산되거나
# 너무 흔해 변별력이 없음) — _korea_score의 콘텐츠 일치도 계산에서 제외.
_QUERY_STOPWORDS = {
    "the", "a", "an", "of", "and", "or", "in", "on", "at", "to", "for", "with",
    "is", "are", "&",
}


def _query_keywords(query: str) -> list:
    """검색어에서 변별력 있는 핵심 단어만 추출(국가명·필러·불용어 제외)."""
    words = re.findall(r"[a-zA-Z가-힣]+", (query or "").lower())
    stop = _QUERY_STOPWORDS | set(_KO_TOKENS) | {w.lower() for w in _QUERY_FILLER} \
        | set(_QUERY_FILLER_KO)
    return [w for w in words if len(w) > 2 and w not in stop]


def _korea_score(item: dict, query: str = "") -> int:
    """소스 이름이 아니라 '검색어(소주제)와 실제 내용이 얼마나 겹치는지'를 중심으로 점수화.
    정부기관 출처는 동률일 때만 근소하게 우대(무조건 최우선은 아님) — 출처 이름만으로
    내용과 안 맞는 사진이 1순위로 올라오는 문제를 막기 위함."""
    text = " ".join([item.get("title", ""), item.get("creator", ""),
                     item.get("source_page", "")]).lower()
    score = 10 if item.get("_korea_hint") else 0   # 카테고리로 한국 관련성 이미 확인됨
    for t in _KO_TOKENS:
        if t in text:
            score += 2
    for t in _NEG_TOKENS:
        if t in text:
            score -= 3
    for kw in _query_keywords(query):                 # 소주제 핵심어와 실제 일치하는지
        if kw in text:
            score += 6
    # 정식 명칭으로 찾아온 유물('도자기'로 검색했는데 제목은 '백자 항아리')도 일치로 인정.
    # 이게 없으면 정작 정확한 유물 사진이 0점이라 뒤로 밀린다(2026-08-07).
    for alt in archive_synonyms(query):
        if alt.lower() in text:
            score += 6
            break
    if item.get("source") in ("한국관광공사", "국가유산청", "공유마당(공공누리)"):
        score += 8              # 한국 정부기관 자체 API — 동률 시 타이브레이커
    elif item.get("source") in ("Unsplash", "Pexels"):
        score += 3              # 고화질 스톡 사진 — 동률 시 약한 가산
    return score


# 관광공사 결과가 '장소'임을 드러내는 말 — 이런 게 제목에 있으면 실물 작품이 아니라
# 그 작품을 전시·판매·체험하는 '건물/가게/행사'다(민화박물관, 한복대여점, 도자기축제 …).
_PLACE_WORDS = ("박물관", "미술관", "전시관", "기념관", "체험관", "마을", "축제", "거리",
                "시장", "상가", "매장", "공방", "카페", "센터", "공원", "학교", "대여",
                "museum", "gallery", "festival", "market", "shop", "center", "village")


# 유물 아카이브(공유마당·국가유산청)는 정식 명칭으로 등록돼 있어, 일상어로 검색하면
# 0건이 나오고 결국 관광공사의 상호·건물 사진만 남는다(실측: '도자기'→0건이지만
# '백자'→국보 항아리 3건, '청자'→국보 향로 3건). 그래서 일상어를 정식 명칭으로 넓혀 함께
# 조회한다. 값은 '그 말로 검색하면 실제 유물이 나오는지' 실측해 고른 것만 넣었다(2026-08-07).
_ARCHIVE_SYNONYMS = {
    "도자기": ["백자", "청자", "분청사기"],
    "도예": ["백자", "청자", "분청사기"],
    "자기": ["백자", "청자"],
    "한복": ["저고리", "당의", "원삼"],
    "전통 의복": ["저고리", "당의", "원삼"],
    "그릇": ["백자", "청자", "분청사기"],
}


def archive_synonyms(query: str) -> list:
    """유물 아카이브용 대체 검색어(없으면 빈 목록)."""
    q = (query or "").strip()
    if q in _ARCHIVE_SYNONYMS:
        return _ARCHIVE_SYNONYMS[q]
    for k, v in _ARCHIVE_SYNONYMS.items():      # '한복 입기'처럼 말이 붙은 경우도 인식
        if k in q:
            return v
    return []


def _source_bonus(item: dict, query: str = "") -> int:
    """소스 성격에 따른 가산·감산(2026-08-07 신설).
    공유마당·국가유산청은 '실물 작품·유물' 아카이브라 전통문화 주제에서 신뢰도가 높다.
    반면 관광공사는 장소 검색이라, 제목이 '~박물관/~마을/~축제'처럼 장소를 가리키면
    작품 자체가 아니므로 뒤로 보낸다(작품을 찾는데 간판 사진이 오는 것을 방지)."""
    src = item.get("source", "")
    title = (item.get("title", "") or "").lower()
    if src in ("공유마당(공공누리)", "국가유산청"):
        return 6                       # 실물 작품·유물 자료 — 우선
    if src == "한국관광공사" and any(w in title for w in _PLACE_WORDS):
        return -10                     # 작품이 아니라 장소·건물·행사
    return 0


def _augment_query(q: str) -> str:
    """영어 검색어에 한국 신호가 약하면 'Korea/Korean'을 자동 보강."""
    ql = q.lower()
    if any(t in ql for t in _KO_TOKENS):
        return q
    return f"{q} Korea Korean"


# SEO형 글 제목에 흔한 필러 단어 — 검색어에서 빼면 매칭률이 크게 오름.
_QUERY_FILLER = (
    "guide", "guides", "tips", "tip", "highlights", "highlight", "introduction",
    "overview", "ultimate", "complete", "explained", "exploring", "explore",
    "secrets", "essential", "essentials", "discover", "discovering",
    "understanding", "mastering",
)
_QUERY_FILLER_KO = ("가이드", "탐방", "이해하기", "탐구", "안내")


def _simplify_query(q: str) -> str:
    """긴 SEO형 글 제목을 짧은 키워드 검색어로 축약.
    'Jongmyo Shrine & Seoul Palaces Guide: Discover Korean Royal Heritage' 같은
    제목을 그대로 던지면 Openverse·LOC·Wikimedia 같은 키워드 검색 API가 전부
    0건을 반환하고(검색어가 너무 길고 추상적) 느슨하게 매칭하는 소스(ARTIC 등)만
    남아 결과가 한쪽 소스로 쏠리는 문제가 있어, 콜론 앞의 핵심부만 남기고
    '가이드/Guide' 같은 필러 단어를 제거해 매칭률을 회복함."""
    s = re.split(r"[:|–—]", q)[0]
    s = s.replace("&", " and ")
    words = [w for w in s.split() if w]
    words = [w for w in words
             if w.strip(",.'’").lower() not in _QUERY_FILLER
             and w not in _QUERY_FILLER_KO]
    out = " ".join(words).strip()
    return out or q


def _get(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# 위키미디어는 'File:' 네임스페이스 전체(PDF·DjVu 스캔본·오디오 등)를 검색해서
# <img>로 띄울 수 없는 결과가 섞여 들어옴 — 확장자로 한 번 더 걸러냄.
_NON_IMAGE_EXTS = {"pdf", "djvu", "ogg", "ogv", "oga", "webm", "flac", "wav",
                   "mid", "midi", "stl", "mp3", "mp4", "xcf"}


def _is_real_image(url: str) -> bool:
    if not url:
        return False
    ext = url.rsplit(".", 1)[-1].lower() if "." in url else ""
    return ext not in _NON_IMAGE_EXTS


def _strip_tags(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


def _clean_title(s: str) -> str:
    """'File:Foo_bar.jpg' 같은 파일명 제목을 사람이 읽기 좋게 정리."""
    s = (s or "").strip()
    s = re.sub(r"^File:", "", s)
    s = re.sub(r"\.(jpe?g|png|gif|webp|tiff?|svg)$", "", s, flags=re.IGNORECASE)
    s = s.replace("_", " ").strip()
    return s


# ── 응답 필드명이 불확실한 정부 OpenAPI용 관대한 파싱 헬퍼 (collector.py와 같은 패턴) ──
def _pick(d: dict, candidates: list) -> str:
    """후보 필드명을 순서대로 찾아 처음 값이 있는 것을 반환(대문자/소문자 변형도 시도)."""
    for c in candidates:
        for k in (c, c.upper(), c.lower()):
            v = d.get(k)
            if v:
                return str(v).strip()
    return ""


def _dig_list(data, *paths) -> list:
    """data.go.kr 표준 래퍼(response.body.items.item) 등 흔한 중첩 구조에서
    항목 리스트를 찾는다. paths는 후보 경로(키 튜플) 목록."""
    for path in paths:
        cur = data
        ok = True
        for key in path:
            if isinstance(cur, dict) and key in cur:
                cur = cur[key]
            else:
                ok = False
                break
        if ok and cur:
            return cur if isinstance(cur, list) else [cur]
    return []


def _xml_leaf_dict(root) -> dict:
    """XML 트리 전체에서 자식이 없는(leaf) 태그만 모아 {태그명: 텍스트} dict로.
    중첩 깊이·네임스페이스에 둔감하게 동작(국가유산청 상세 응답처럼 구조를 모를 때)."""
    d = {}
    for el in root.iter():
        if len(list(el)) == 0 and (el.text or "").strip():
            tag = el.tag.split("}")[-1] if "}" in el.tag else el.tag
            d[tag] = el.text.strip()
    return d


# ── Openverse (키 불필요, CC·퍼블릭도메인 8억+) ──────────────────────────────
def search_openverse(query: str, n: int = 6, safe_only: bool = True) -> list:
    base = "https://api.openverse.org/v1/images/"
    params = {"q": query, "page_size": str(max(1, min(n, 20)))}
    if safe_only:
        params["license"] = _SAFE_OPENVERSE_LICENSES
    try:
        data = json.loads(_get(base + "?" + urllib.parse.urlencode(params)))
    except Exception:
        return []
    out = []
    for it in data.get("results", []):
        url = it.get("url") or ""
        if not url:
            continue
        lic = it.get("license", "")
        ver = it.get("license_version", "")
        lic_label = ("CC " + lic.upper() + (" " + ver if ver else "")).strip() if lic else ""
        out.append({
            "url": url,
            "thumb": it.get("thumbnail") or url,
            "title": _clean_title(it.get("title") or ""),
            "source": "Openverse",
            "source_page": it.get("foreign_landing_url") or it.get("detail_url") or url,
            "license": lic_label,
            "creator": (it.get("creator") or "").strip(),
            "attribution": (it.get("attribution") or "").strip(),
        })
    return out


# ── Wikimedia Commons (키 불필요, 한국 문화재·장소에 강함) ────────────────────
def search_wikimedia(query: str, n: int = 6) -> list:
    base = "https://commons.wikimedia.org/w/api.php"
    params = {
        "action": "query", "generator": "search",
        # filetype:bitmap|drawing 로 PDF·DjVu 스캔본·오디오 등을 검색 단계에서 제외.
        "gsrsearch": f"{query} filetype:bitmap|drawing",
        "gsrnamespace": "6", "gsrlimit": str(max(1, min(n, 20))),
        "prop": "imageinfo", "iiprop": "url|extmetadata", "iiurlwidth": "1280",
        "format": "json",
    }
    try:
        data = json.loads(_get(base + "?" + urllib.parse.urlencode(params)))
    except Exception:
        return []
    pages = (data.get("query") or {}).get("pages") or {}
    out = []
    for pg in pages.values():
        ii = (pg.get("imageinfo") or [{}])[0]
        url = ii.get("url") or ii.get("thumburl") or ""
        if not url or not _is_real_image(url):
            continue
        em = ii.get("extmetadata") or {}
        lic = (em.get("LicenseShortName") or {}).get("value", "")
        creator = _strip_tags((em.get("Artist") or {}).get("value", ""))
        out.append({
            "url": url,
            "thumb": ii.get("thumburl") or url,
            "title": _clean_title(pg.get("title") or ""),
            "source": "Wikimedia Commons",
            "source_page": ii.get("descriptionurl") or url,
            "license": lic,
            "creator": creator,
            "attribution": "",
        })
    return out


# ── Wikimedia Commons 카테고리 검색 (키 불필요, 추상적 개념어 주제에 강함) ───
# "한국 지형/기후/역사" 같은 추상 개념어는 일반 텍스트 검색(search_wikimedia)으로
# 거의 매칭이 안 되는데, Commons는 "Category:X of South Korea" 카테고리 트리가
# 잘 정리돼 있어 카테고리 검색이 훨씬 강함(2026-06-24, 실측으로 카테고리 존재·
# 콘텐츠 보유 확인 후 추가). 쿼리에 힌트 단어가 없으면 추가 호출 없이 빈 리스트.
_WIKI_CATEGORY_HINTS = {
    "지형": "Geography of South Korea", "지리": "Geography of South Korea",
    "geography": "Geography of South Korea", "terrain": "Geography of South Korea",
    "기후": "Climate of South Korea", "climate": "Climate of South Korea",
    "weather": "Climate of South Korea",
    "산맥": "Mountains of South Korea", "mountain": "Mountains of South Korea",
    "해안": "Coasts of South Korea", "coast": "Coasts of South Korea",
    "강": "Rivers of South Korea", "river": "Rivers of South Korea",
    "섬": "Islands of South Korea", "island": "Islands of South Korea",
    "역사": "History of South Korea", "history": "History of South Korea",
    "건축": "Architecture of South Korea", "architecture": "Architecture of South Korea",
    "음식": "Cuisine of South Korea", "요리": "Cuisine of South Korea",
    "cuisine": "Cuisine of South Korea", "food": "Cuisine of South Korea",
    "문화": "Culture of South Korea", "culture": "Culture of South Korea",
    "종교": "Religion in South Korea", "religion": "Religion in South Korea",
}


def _category_files(cat: str, limit: int) -> list:
    """카테고리(예: 'Geography of South Korea')의 실제 이미지 파일 목록(imageinfo 포함)."""
    base = "https://commons.wikimedia.org/w/api.php"
    params = {
        "action": "query", "list": "categorymembers",
        "cmtitle": f"Category:{cat}", "cmtype": "file",
        "cmlimit": str(max(1, min(limit, 30))), "format": "json",
    }
    try:
        data = json.loads(_get(base + "?" + urllib.parse.urlencode(params)))
    except Exception:
        return []
    titles = [m.get("title") for m in (data.get("query") or {}).get("categorymembers", [])
              if m.get("title")]
    if not titles:
        return []
    params = {
        "action": "query", "titles": "|".join(titles[:50]),
        "prop": "imageinfo", "iiprop": "url|extmetadata", "iiurlwidth": "1280",
        "format": "json",
    }
    try:
        data = json.loads(_get(base + "?" + urllib.parse.urlencode(params)))
    except Exception:
        return []
    out = []
    for pg in (data.get("query") or {}).get("pages", {}).values():
        ii = (pg.get("imageinfo") or [{}])[0]
        url = ii.get("url") or ii.get("thumburl") or ""
        if not url or not _is_real_image(url):
            continue
        em = ii.get("extmetadata") or {}
        out.append({
            "url": url,
            "thumb": ii.get("thumburl") or url,
            "title": _clean_title(pg.get("title") or ""),
            "source": "Wikimedia Commons",
            "source_page": ii.get("descriptionurl") or url,
            "license": (em.get("LicenseShortName") or {}).get("value", ""),
            "creator": _strip_tags((em.get("Artist") or {}).get("value", "")),
            "attribution": "",
            "_korea_hint": True,    # 카테고리로 이미 한국 관련성 확인됨 — 점수 가산용
        })
    return out


def search_wikimedia_category(query: str, n: int = 6) -> list:
    """쿼리에 추상 개념어 힌트가 있으면 해당 'X of South Korea' 카테고리의
    실제 파일을 가져옴(일반 텍스트 검색보다 추상적 주제에 훨씬 강함).
    카테고리 여러 개가 매칭되면 한쪽(예: 봇이 대량 생성한 기후 그래프)으로
    쏠리지 않도록 라운드로빈으로 섞어서 채움."""
    ql = query.lower()
    cats = []
    for kw, cat in _WIKI_CATEGORY_HINTS.items():
        if kw in ql and cat not in cats:
            cats.append(cat)
    if not cats:
        return []
    per_cat = [_category_files(cat, n * 2) for cat in cats[:3]]
    total_available = sum(len(lst) for lst in per_cat)
    out, idx = [], 0
    while len(out) < n and len(out) < total_available:
        for lst in per_cat:
            if idx < len(lst):
                out.append(lst[idx])
                if len(out) >= n:
                    break
        idx += 1
    return out


# ── api.wikimedia.org 공식 통합 Core REST API (개인 API 토큰 필요) ──────────
# 위(search_wikimedia)는 옛 MediaWiki Action API(commons.wikimedia.org/w/api.php, 키 불필요).
# 이건 Wikimedia Foundation이 2022년부터 운영하는 신규 통합 게이트웨이로, 검색 관련성/순위가
# 다르게 나올 수 있어 같은 Commons 자료를 보강하는 용도. api.wikimedia.org 가입 → 앱 생성 →
# "Personal API token" 발급(무료, 즉시) 후 settings의 wikimedia_token에 입력.
def search_wikimedia_api(query: str, token: str, n: int = 6) -> list:
    if not token:
        return []
    qs = urllib.parse.urlencode({"q": query, "limit": str(max(1, min(n, 30)))})
    headers = {"User-Agent": UA, "Authorization": f"Bearer {token}"}
    try:
        req = urllib.request.Request(
            "https://api.wikimedia.org/core/v1/commons/search/page?" + qs, headers=headers)
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
    except Exception:
        return []
    out = []
    for p in data.get("pages", []) or []:
        thumb = (p.get("thumbnail") or {}).get("url") or ""
        if thumb.startswith("//"):
            thumb = "https:" + thumb
        if not thumb or not _is_real_image(thumb):
            continue
        page_key = p.get("key") or p.get("title") or ""
        full = thumb
        try:
            dreq = urllib.request.Request(
                "https://api.wikimedia.org/core/v1/commons/file/" + urllib.parse.quote(page_key, safe=""),
                headers=headers)
            with urllib.request.urlopen(dreq, timeout=15) as r2:
                fd = json.loads(r2.read())
            orig = (fd.get("original") or {}).get("url") or ""
            if orig and _is_real_image(orig):
                full = orig
        except Exception:
            pass
        out.append({
            "url": full,
            "thumb": thumb,
            "title": _clean_title(p.get("title") or ""),
            "source": "Wikimedia (api.wikimedia.org)",
            "source_page": "https://commons.wikimedia.org/wiki/" + urllib.parse.quote(page_key, safe=""),
            "license": "Wikimedia Commons (출처 표기)",
            "creator": "",
            "attribution": _strip_tags(p.get("description") or ""),
        })
        if len(out) >= n:
            break
    return out


# ── Library of Congress (키 불필요, 공공기록·근현대 자료에 강함) ─────────────
def search_loc(query: str, n: int = 6) -> list:
    """미국 의회도서관 사진 자료 검색. 한국전쟁 등 근현대 공공기록 사진이 많고
    대부분 '저작권 제한 없음(no known restrictions)'으로 표기되어 안전합니다."""
    base = "https://www.loc.gov/photos/"
    qs = urllib.parse.urlencode({"q": query, "fo": "json", "c": str(max(1, min(n + 6, 25)))})
    try:
        data = json.loads(_get(base + "?" + qs))
    except Exception:
        return []
    out = []
    for it in data.get("results", []) or []:
        imgs = it.get("image_url") or []
        if isinstance(imgs, str):
            imgs = [imgs]
        if not imgs:
            continue
        img = imgs[-1]
        thumb = imgs[0] if len(imgs) > 1 else img
        rights = it.get("rights")
        rights = " ".join(rights) if isinstance(rights, list) else (rights or "")
        if "no known restriction" in rights.lower():
            lic = "공공기록 — 저작권 제한 없음"
        elif rights:
            lic = rights
        elif it.get("access_restricted") is False:
            lic = "미 의회도서관 공공기록 (제한 없음으로 표시됨)"
        else:
            lic = "Library of Congress"
        out.append({
            "url": img,
            "thumb": thumb,
            "title": _clean_title(it.get("title") or ""),
            "source": "Library of Congress",
            "source_page": it.get("url") or "",
            "license": lic,
            "creator": "",
            "attribution": "",
        })
        if len(out) >= n:
            break
    return out


# ── 한국관광공사 TourAPI (data.go.kr 키 필요, 한국 관광지·문화재에 최적) ──────
def search_tourapi(query: str, key: str, n: int = 6) -> list:
    """한국관광공사 KorService 키워드검색 → 관광지 대표이미지 목록.
    매칭된 장소 수가 적어 결과가 모자라면, 가장 관련도 높은 장소의 실제 사진
    갤러리(detailImage2)로 같은 장소의 추가 사진을 보강(2026-06-24 추가).
    공공누리 라이선스(상업·변형 허용형)로 블로그 사용 안전, 출처 표기 자동."""
    if not key:
        return []
    base = "http://apis.data.go.kr/B551011/KorService2/searchKeyword2"
    qs = urllib.parse.urlencode({
        "serviceKey": key, "MobileOS": "ETC", "MobileApp": "blog-studio",
        "keyword": query, "_type": "json",
        "numOfRows": str(max(1, min(n + 3, 15))), "pageNo": "1",
    }, safe="+%")
    try:
        with urllib.request.urlopen(base + "?" + qs, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception:
        return []
    items = (((data.get("response") or {}).get("body") or {}).get("items") or {}).get("item") or []
    if isinstance(items, dict):
        items = [items]
    out = []
    for it in items:
        img = (it.get("firstimage") or "").strip()
        if not img:
            continue
        out.append({
            "url": img,
            "thumb": (it.get("firstimage2") or img).strip(),
            "title": (it.get("title") or "").strip(),
            "source": "한국관광공사",
            "source_page": "https://korean.visitkorea.or.kr",
            "license": "공공누리 (출처 표기)",
            "creator": "한국관광공사",
            "attribution": (it.get("addr1") or "").strip(),
        })
        if len(out) >= n:
            break
    if len(out) < n and items:
        out.extend(_tourapi_gallery(items[0], key, n - len(out)))
    return out[:n]


def _tourapi_gallery(item: dict, key: str, n: int) -> list:
    """detailImage2: 특정 장소(contentid)의 실제 촬영 사진 갤러리(여러 장)."""
    cid = (item.get("contentid") or "").strip()
    if not cid or n <= 0:
        return []
    base = "http://apis.data.go.kr/B551011/KorService2/detailImage2"
    qs = urllib.parse.urlencode({
        "serviceKey": key, "MobileOS": "ETC", "MobileApp": "blog-studio",
        "contentId": cid, "_type": "json", "imageYN": "Y",
        "numOfRows": str(max(1, min(n + 4, 20))), "pageNo": "1",
    }, safe="+%")
    try:
        with urllib.request.urlopen(base + "?" + qs, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception:
        return []
    gitems = (((data.get("response") or {}).get("body") or {}).get("items") or {}).get("item") or []
    if isinstance(gitems, dict):
        gitems = [gitems]
    title = (item.get("title") or "").strip()
    out = []
    for g in gitems:
        img = (g.get("originimgurl") or "").strip()
        if not img:
            continue
        nuri = (g.get("cpyrhtDivCd") or "").replace("Type", "")
        out.append({
            "url": img,
            "thumb": (g.get("smallimageurl") or img).strip(),
            "title": title,
            "source": "한국관광공사",
            "source_page": "https://korean.visitkorea.or.kr",
            "license": f"공공누리 {nuri}유형" if nuri else "공공누리 (출처 표기)",
            "creator": "한국관광공사",
            "attribution": "",
        })
        if len(out) >= n:
            break
    return out


# ── 한국관광공사 관광공모전(사진) 수상작 (data.go.kr 키 필요, 별도 활용신청) ──
# 전문 작가가 촬영한 95건(2026-06-24 기준)의 소규모 고품질 컬렉션, 전부 공공누리 1유형.
# 엔드포인트: apis.data.go.kr/B551011/PhokoAwrdService/phokoAwrdList (실응답으로 확정).
# ⚠️ cond[xxx::LIKE] 서버 필터가 동작하지 않음(실측: 존재하는 키워드로도 0건) — 건수가
# 적어서(95건) 전체를 한 번에 받아 클라이언트에서 제목·촬영지·키워드로 직접 매칭함.
def search_tourapi_award(query: str, key: str, n: int = 6) -> list:
    """한국관광공사 관광공모전(사진) 수상작 — 전문 작가 촬영 고품질 사진(공공누리 1유형)."""
    if not key:
        return []
    base = "http://apis.data.go.kr/B551011/PhokoAwrdService/phokoAwrdList"
    qs = urllib.parse.urlencode({
        "serviceKey": key, "MobileOS": "ETC", "MobileApp": "blog-studio",
        "_type": "json", "numOfRows": "200", "pageNo": "1",
    }, safe="+%")
    try:
        with urllib.request.urlopen(base + "?" + qs, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception:
        return []
    items = (((data.get("response") or {}).get("body") or {}).get("items") or {}).get("item") or []
    if isinstance(items, dict):
        items = [items]
    words = [w for w in re.split(r"[^\w가-힣]+", query.lower()) if len(w) > 1]
    scored = []
    for it in items:
        text = " ".join([it.get("enTitle", ""), it.get("koTitle", ""),
                          it.get("enFilmst", ""), it.get("koFilmst", ""),
                          it.get("enKeyWord", ""), it.get("koKeyWord", "")]).lower()
        hits = sum(1 for w in words if w in text)
        if hits:
            scored.append((hits, it))
    scored.sort(key=lambda t: -t[0])
    out = []
    for _, it in scored[:n]:
        img = (it.get("orgImage") or "").strip()
        if not img:
            continue
        nuri = (it.get("cpyrhtDivCd") or "").replace("Type", "")
        out.append({
            "url": img,
            "thumb": (it.get("thumbImage") or img).strip(),
            "title": (it.get("enTitle") or it.get("koTitle") or "").strip(),
            "source": "한국관광공사",
            "source_page": "https://phoko.visitkorea.or.kr/",
            "license": f"공공누리 {nuri}유형" if nuri else "공공누리(출처 표기)",
            "creator": (it.get("enCmanNm") or it.get("koCmanNm") or "").strip(),
            "attribution": (it.get("enFilmst") or it.get("koFilmst") or "").strip(),
        })
    return out


# ── 공유마당/공공누리 (한국저작권위원회, 키 필요) ───────────────────────────
# 2026-06-24 확인: 레거시 직접 도메인(openapi.copyright.or.kr)은 키 등록 여부와
# 무관하게 "SERVICE KEY IS NOT REGISTERED ERROR"만 반환 — data.go.kr 활용신청 페이지의
# 공식 End Point는 게이트웨이(apis.data.go.kr/B552546/ShrWrtgService)임을 확인하고 변경.
# 데이터포맷이 XML로 고정(json 파라미터 무시됨)이라 XML로 파싱.
# 실제 API 응답으로 확정한 필드명(2026-08-07 실측). 예전엔 문서가 없어 추정치를 넣어뒀는데
# 전부 빗나가서 이미지가 한 장도 안 나왔음(응답은 정상인데 파싱이 실패).
#   limgpath=큰 이미지, simgpath=썸네일, writingName=제목, writingSeq=일련번호,
#   authorNameKor=저자(한글). 라이선스 필드는 목록 응답에 없어 기본 표기를 쓴다.
_GONGU_FIELDS = {
    "title": ["writingName", "WRT_NM", "TITLE"],
    "image": ["limgpath", "simgpath", "IMG_URL", "FILE_URL"],
    "id": ["writingSeq", "WRT_SN", "SN"],
    "license": ["FREE_USE_TYPE", "GONGU_MARK_NM", "OL_SORT_NM"],
    "author": ["authorNameKor", "authorNameChn", "WRT_WRTR"],
}


def search_gongu(query: str, key: str, n: int = 6) -> list:
    """공유마당(공공누리 자유이용 저작물) 이미지 검색.
    키: data.go.kr에서 '한국저작권위원회_공유(만료)저작물 서비스' 활용신청으로 발급."""
    if not key:
        return []
    base = "https://apis.data.go.kr/B552546/ShrWrtgService/getImgExpWrtgList"
    # 검색 파라미터는 'writingName'(제목 부분일치). 예전에 쓰던 'cond[WRT_NM::LIKE]' 형식은
    # 서버가 조용히 무시해 매번 전체 목록(15,802건)의 첫 페이지만 돌려줬다 — 검색어와 무관한
    # 서예 작품이 나오던 원인(2026-08-07 실측으로 확정).
    qs = urllib.parse.urlencode({
        "serviceKey": key, "numOfRows": str(max(1, min(n + 4, 30))), "pageNo": "1",
        "writingName": query,
    })
    try:
        root = ET.fromstring(_get(base + "?" + qs))
    except Exception:
        return []
    out = []
    for item in root.iter("item"):
        d = _xml_leaf_dict(item)
        img = _pick(d, _GONGU_FIELDS["image"])
        if not img:
            continue
        # API는 http:// 주소를 주는데 그 서버가 http를 거부(503)하고 https만 받는다 —
        # 그대로 쓰면 다운로드가 전부 실패한다(2026-08-07 실측).
        if img.startswith("http://"):
            img = "https://" + img[len("http://"):]
        wid = _pick(d, _GONGU_FIELDS["id"])
        out.append({
            "url": img,
            "thumb": img,
            "title": _pick(d, _GONGU_FIELDS["title"]),
            "source": "공유마당(공공누리)",
            "source_page": (f"https://gongu.copyright.or.kr/gongu/wrt/wrt/view.do?wrtSn={wid}"
                            if wid else "https://gongu.copyright.or.kr/"),
            "license": _pick(d, _GONGU_FIELDS["license"]) or "공공누리(자유이용 저작물)",
            "creator": _pick(d, _GONGU_FIELDS["author"]),
            "attribution": "",
        })
        if len(out) >= n:
            break
    return out


# ── 국가유산청(옛 문화재청) 국가지정문화유산 이미지 (키 불필요) ─────────────
# 2024년 문화재청→국가유산청 개편으로 도메인이 cha.go.kr → khs.go.kr 로 바뀌었고,
# 목록검색(SearchKindOpenapiList.do)·이미지검색(SearchImageOpenapi.do) 모두
# serviceKey 없이 바로 호출 가능함을 실응답으로 확인함(2026-06-24).
# 목록: ccbaMnm1=검색어(부분일치, 한글 OK) → ccbaKdcd/ccbaAsno/ccbaCtcd 획득
# 이미지: 위 3개 키로 item.imageUrl/ccimDesc/imageNuri(공공누리 유형) 반환
def search_heritage(query: str, n: int = 6) -> list:
    """국가유산청 국가지정문화유산 이미지 검색(키 불필요, 전부 공공누리 표기)."""
    list_base = "http://www.khs.go.kr/cha/SearchKindOpenapiList.do"
    qs = urllib.parse.urlencode({
        "ccbaCncl": "N", "ccbaMnm1": query,
        "pageUnit": str(max(1, min(n + 4, 20))), "pageIndex": "1",
    })
    try:
        list_root = ET.fromstring(_get(list_base + "?" + qs))
    except Exception:
        return []
    out = []
    for item in list_root.iter("item"):
        if len(out) >= n:
            break
        d = {c.tag: (c.text or "").strip() for c in item if (c.text or "").strip()}
        kdcd, asno, ctcd = d.get("ccbaKdcd"), d.get("ccbaAsno"), d.get("ccbaCtcd")
        name = d.get("ccbaMnm1", "")
        if not (kdcd and asno and ctcd):
            continue
        try:
            iqs = urllib.parse.urlencode({"ccbaKdcd": kdcd, "ccbaAsno": asno, "ccbaCtcd": ctcd})
            img_root = ET.fromstring(_get(
                "http://www.khs.go.kr/cha/SearchImageOpenapi.do?" + iqs))
        except Exception:
            continue
        for im in img_root.iter("item"):
            if len(out) >= n:
                break
            idd = {c.tag: (c.text or "").strip() for c in im if (c.text or "").strip()}
            url = idd.get("imageUrl")
            if not url:
                continue
            nuri = idd.get("imageNuri", "")
            out.append({
                "url": url,
                "thumb": url,
                "title": idd.get("ccimDesc") or name,
                "source": "국가유산청",
                "source_page": "https://www.heritage.go.kr/",
                "license": f"공공누리 {nuri}유형" if nuri else "공공저작물 (국가유산청, 출처 표기)",
                "creator": "",
                "attribution": "",
            })
    return out


# ── Art Institute of Chicago (키 불필요, CC0 퍼블릭도메인 미술품) ─────────────
def search_artic(query: str, n: int = 6) -> list:
    base = "https://api.artic.edu/api/v1/artworks/search"
    qs = urllib.parse.urlencode({
        "q": query, "fields": "id,title,image_id,artist_title,is_public_domain",
        "limit": str(max(1, min(n, 15)))})
    try:
        data = json.loads(_get(base + "?" + qs))
    except Exception:
        return []
    iiif = (data.get("config") or {}).get("iiif_url", "https://www.artic.edu/iiif/2")
    out = []
    for a in data.get("data", []):
        img = a.get("image_id")
        if not img:
            continue
        out.append({
            "url": f"{iiif}/{img}/full/843,/0/default.jpg",
            "thumb": f"{iiif}/{img}/full/400,/0/default.jpg",
            "title": _clean_title(a.get("title", "")),
            "source": "Art Institute of Chicago",
            "source_page": f"https://www.artic.edu/artworks/{a.get('id')}",
            "license": "CC0 (Public Domain)" if a.get("is_public_domain") else "AIC",
            "creator": (a.get("artist_title") or "").strip(),
            "attribution": "",
        })
    return out


# ── The Met (키 불필요, 퍼블릭도메인 소장품) ──────────────────────────────────
def search_met(query: str, n: int = 6) -> list:
    try:
        s = json.loads(_get(
            "https://collectionapi.metmuseum.org/public/collection/v1/search?"
            + urllib.parse.urlencode({"q": query, "hasImages": "true"})))
    except Exception:
        return []
    ids = (s.get("objectIDs") or [])[:max(1, min(n + 4, 12))]
    out = []
    for oid in ids:
        try:
            o = json.loads(_get(
                f"https://collectionapi.metmuseum.org/public/collection/v1/objects/{oid}"))
        except Exception:
            continue
        img = o.get("primaryImageSmall") or o.get("primaryImage")
        if not img:
            continue
        out.append({
            "url": o.get("primaryImage") or img,
            "thumb": img,
            "title": _clean_title(o.get("title", "")),
            "source": "The Met",
            "source_page": o.get("objectURL", ""),
            "license": "CC0 (Public Domain)" if o.get("isPublicDomain") else "The Met",
            "creator": (o.get("artistDisplayName") or "").strip(),
            "attribution": "",
        })
        if len(out) >= n:
            break
    return out


# ── Pexels / Pixabay (무료 API 키가 있을 때만 — 화질 업그레이드) ──────────────
def search_pexels(query: str, key: str, n: int = 6) -> list:
    if not key:
        return []
    url = "https://api.pexels.com/v1/search?" + urllib.parse.urlencode(
        {"query": query, "per_page": str(max(1, min(n, 20))), "orientation": "landscape"})
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Authorization": key})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
    except Exception:
        return []
    out = []
    for p in data.get("photos", []):
        src = (p.get("src") or {})
        out.append({
            "url": src.get("large2x") or src.get("large") or src.get("original") or "",
            "thumb": src.get("medium") or src.get("small") or "",
            "title": (p.get("alt") or "").strip(),
            "source": "Pexels",
            "source_page": p.get("url") or "",
            "license": "Pexels License (출처 표기 불필요)",
            "creator": (p.get("photographer") or "").strip(),
            "attribution": "",
        })
    return [x for x in out if x["url"]]


def search_pixabay(query: str, key: str, n: int = 6) -> list:
    if not key:
        return []
    url = "https://pixabay.com/api/?" + urllib.parse.urlencode(
        {"key": key, "q": query, "per_page": str(max(3, min(n, 20))),
         "image_type": "photo", "safesearch": "true"})
    try:
        data = json.loads(_get(url))
    except Exception:
        return []
    out = []
    for h in data.get("hits", []):
        out.append({
            "url": h.get("largeImageURL") or h.get("webformatURL") or "",
            "thumb": h.get("webformatURL") or h.get("previewURL") or "",
            "title": (h.get("tags") or "").strip(),
            "source": "Pixabay",
            "source_page": h.get("pageURL") or "",
            "license": "Pixabay License (출처 표기 불필요)",
            "creator": (h.get("user") or "").strip(),
            "attribution": "",
        })
    return [x for x in out if x["url"]]


def search_unsplash(query: str, key: str, n: int = 6) -> list:
    """Unsplash 검색(무료 Access Key, unsplash.com/developers에서 즉시 발급)."""
    if not key:
        return []
    url = "https://api.unsplash.com/search/photos?" + urllib.parse.urlencode(
        {"query": query, "per_page": str(max(1, min(n, 30))), "orientation": "landscape"})
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": UA, "Authorization": f"Client-ID {key}"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
    except Exception:
        return []
    out = []
    for p in data.get("results", []):
        urls = p.get("urls") or {}
        user = p.get("user") or {}
        img = urls.get("regular") or urls.get("full") or ""
        if not img:
            continue
        out.append({
            "url": img,
            "thumb": urls.get("small") or urls.get("thumb") or img,
            "title": (p.get("alt_description") or p.get("description") or "").strip(),
            "source": "Unsplash",
            "source_page": (p.get("links") or {}).get("html") or "",
            "license": "Unsplash License (출처 표기 권장)",
            "creator": (user.get("name") or "").strip(),
            "attribution": "",
        })
    return out


# ── 통합 검색 ────────────────────────────────────────────────────────────────
def find_images(query: str, n: int = 6, settings: dict = None,
                korea_focus: bool = True) -> list:
    """주제(query)에 맞는 무료·저작권 안전 이미지 후보를 모아 반환.
    korea_focus=True 면 'Korea/Korean'을 자동 보강하고, 한국 관련성으로 재정렬+필터.
    다른 나라 작품(일본·중국·유럽 등) 노이즈가 줄어듭니다."""
    settings = settings or {}
    query = (query or "").strip()
    if not query:
        return []
    query = _simplify_query(query)              # SEO 제목 필러 제거(콜론 앞부분만)
    q = _augment_query(query) if korea_focus else query
    pool = max(n * 3, 12)         # 후처리에서 추리려고 넉넉히 모음
    results, seen = [], set()

    def _add(items):
        for it in items:
            u = it.get("url", "")
            if u and u not in seen:
                seen.add(u)
                results.append(it)

    # 1순위: 한국 전용 소스 — 한국 관광지·문화재·공공저작물 정확도 최상
    tk = (settings.get("tourapi_key") or "").strip()
    if tk:
        _add(search_tourapi(query, tk, pool))   # 원 검색어 그대로(한글 OK)
        _add(search_tourapi_award(query, tk, pool))  # 관광공모전 수상작(고품질 95건)
    _add(search_heritage(query, pool))          # 키 불필요(khs.go.kr)
    gk = (settings.get("gongu_key") or "").strip()
    if gk:
        _add(search_gongu(query, gk, pool))
    # 유물 아카이브는 정식 명칭으로만 등록돼 있어 일상어('도자기','한복')로는 0건이 나온다.
    # 정식 명칭(백자·청자·저고리 …)으로도 함께 조회해 실제 유물 사진을 확보한다.
    for alt in archive_synonyms(query):
        _add(search_heritage(alt, pool))
        if gk:
            _add(search_gongu(alt, gk, pool))
    _add(search_openverse(q, pool))
    _add(search_wikimedia(q, pool))
    _add(search_wikimedia_category(query, pool))  # 추상 개념어 주제 보강(카테고리 검색)
    wmk = (settings.get("wikimedia_token") or "").strip()
    if wmk:
        _add(search_wikimedia_api(q, wmk, pool))  # 공식 통합 API — 같은 Commons 자료 보강
    _add(search_loc(q, pool))                   # 근현대 공공기록 사진(한국전쟁 등)에 강함
    if len(results) < pool:                    # 미술품(민화·도자기·전통미술)에 강함
        _add(search_artic(q, pool))
    if len(results) < pool:
        _add(search_met(q, pool))
    pk = (settings.get("pexels_key") or "").strip()
    if pk:
        _add(search_pexels(q, pk, pool))
    xk = (settings.get("pixabay_key") or "").strip()
    if xk:
        _add(search_pixabay(q, xk, pool))
    uk = (settings.get("unsplash_key") or "").strip()
    if uk:
        _add(search_unsplash(q, uk, pool))

    if not korea_focus:
        return results[:n]

    # 한국 관련성으로 재정렬: 명백한 노이즈(음수 점수)만 제거하고, 나머지는
    # 점수 높은 순으로 정렬해 한국 관련성이 강한 이미지를 위로 올림.
    # (이전엔 점수>0인 게 하나라도 있으면 점수 0짜리를 통째로 버려서
    #  결과 수가 n보다 훨씬 적게 나오는 문제가 있었음 — 이제는 채워서 보여줌)
    # 단, ARTIC·MET은 매칭 안 돼도 '그나마 비슷한 것'을 항상 반환하는 느슨한
    # 검색이라(실측: 'Self-Portrait' 같은 무관한 서양 미술품도 중립 0점으로 통과),
    # 이 두 소스는 score==0이면 노이즈로 간주해 제외(score>0만 인정).
    scored = [(_korea_score(r, query), i, r) for i, r in enumerate(results)]
    use = [t for t in scored
           if t[0] > 0 or (t[0] == 0 and t[2].get("source") not in _LOOSE_MATCH_SOURCES)]
    # 소스 성격 가중치(2026-08-07): 관광공사(TourAPI)는 '장소 이름'에 검색어가 들어가면
    # 무조건 반환해서, '민화'로 찾으면 민화박물관·민화마을·주민화합축제 같은 간판·건물
    # 사진이, '한복'으로 찾으면 한복 대여점·시장 상가가 상위를 전부 차지했다. 정작 필요한
    # '실제 민화 그림·한복 유물'(공유마당·국가유산청)은 뒤로 밀려 한 장도 안 들어갔음.
    # → 실물 작품·유물 아카이브를 관광 장소보다 앞에 세운다.
    use.sort(key=lambda t: (-(t[0] + _source_bonus(t[2], query)), t[1]))
    return [t[2] for t in use[:n]]


def attribution_caption(item: dict, lang: str = "ko") -> str:
    """이미지 캡션 한 줄: 사진 제목(있으면) + 출처(표기 불필요 라이선스는 생략).
    라이선스 종류를 설명하는 문구('Unsplash License (출처 표기 권장)' 등)는
    캡션에 노출하지 않고, 출처 필요 여부 판단에만 내부적으로 사용한다."""
    src = item.get("source", "")
    lic = item.get("license", "") or ""
    who = item.get("creator", "") or src
    if lang == "en":
        title = (item.get("title") or "").strip()
    else:
        title = (item.get("title_ko") or item.get("title") or "").strip()
    needs_attribution = "불필요" not in lic

    parts = []
    if title:
        parts.append(title)
    if needs_attribution:
        if lang == "en":
            s = f"Source: {who}"
            if src and src not in who:
                s += f" via {src}"
        else:
            s = f"출처: {who}"
            if src and src not in who:
                s += f" — {src}"
        parts.append(s)
    return " · ".join(parts)
