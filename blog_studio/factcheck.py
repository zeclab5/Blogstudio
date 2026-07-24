# -*- coding: utf-8 -*-
"""
factcheck.py — 네이버 지역검색으로 '검증된 현지 정보'를 받아 글 생성에 주입(그라운딩).

LLM이 지역 정보(대표 메뉴·맛집 등)를 지어내는 문제를 막기 위해, 글을 쓰기 전에
그 장소의 '실제 업종·장소 분포'를 네이버 지역검색에서 받아 사실 근거로 프롬프트에 넣습니다.

키 필요: 네이버 검색 API (developers.naver.com에서 무료 발급).
  설정: naver_client_id / naver_client_secret. 키가 없으면 빈 문자열을 반환(글은 그대로 진행).

grounding_facts(topic, settings, log) → 프롬프트에 넣을 '검증된 현지 정보' 문자열
"""

import re
import json
import urllib.request
import urllib.parse

_BASE = "https://openapi.naver.com/v1/search/"


def has_keys(settings: dict) -> bool:
    return bool((settings.get("naver_client_id") or "").strip()
                and (settings.get("naver_client_secret") or "").strip())


def _strip(t: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", t or "")).strip()


def naver_search(kind: str, query: str, settings: dict, n: int = 5, sort: str = None) -> list:
    """네이버 검색 API 호출(kind: local/encyc/news 등). 키 없거나 실패 시 빈 목록."""
    if not has_keys(settings):
        return []
    params = {"query": query, "display": max(1, min(n, 5))}
    if sort:
        params["sort"] = sort
    url = _BASE + kind + ".json?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "X-Naver-Client-Id": settings["naver_client_id"].strip(),
        "X-Naver-Client-Secret": settings["naver_client_secret"].strip()})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode("utf-8")).get("items", [])
    except Exception:
        return []


def naver_local_search(query: str, settings: dict, n: int = 5, sort: str = "comment") -> list:
    """네이버 지역검색 → [{title, category, address}]."""
    out = []
    for it in naver_search("local", query, settings, n, sort):
        out.append({"title": _strip(it.get("title", "")),
                    "category": (it.get("category", "") or "").split(">")[-1].strip(),
                    "address": it.get("roadAddress") or it.get("address", "")})
    return out


def _fmt(items: list) -> str:
    return "; ".join(f"{it['title']}({it['category']})" for it in items if it["title"])


def grounding_sources(topic: str, location: str, settings: dict, n: int = 2) -> list:
    """백과사전 검색 결과의 '실제 출처 링크' [(title, url)] 를 반환(키 있을 때만, 없으면 []).
    글 하단에 아웃바운드 권위 링크(E-E-A-T)로 인용 — 지어낸 URL이 아니라 검증된 출처."""
    if not has_keys(settings):
        return []
    out, seen = [], set()
    for it in naver_search("encyc", (topic or location), settings, 3):
        title = _strip(it.get("title", ""))
        link = (it.get("link") or "").strip()
        if title and link.startswith("http") and link not in seen:
            seen.add(link)
            out.append((title, link))
        if len(out) >= n:
            break
    return out


def grounding_facts(topic: str, location: str, settings: dict, log=print) -> str:
    """검증 근거 블록 생성:
      · 지역검색(음식점·주변) — 실제 장소(location)가 있을 때만(노이즈 방지)
      · 백과사전 — 주제의 정의·연대·유래(항상)
      · 뉴스 — 최근 소식(참고용, 있을 때만 1~2건)"""
    if not has_keys(settings):
        return ""
    topic = (topic or "").strip()
    place = (location or "").strip()
    parts = []

    # 1) 지역검색 — 실제 장소일 때만 (예: '의왕 백운호수')
    if place:
        food = naver_local_search(place + " 맛집", settings, 5, "comment")
        spots = naver_local_search(place + " 가볼만한곳", settings, 5, "comment")
        loc_lines = []
        if food:
            loc_lines.append("음식점(맛집) 실제 결과: " + _fmt(food))
        if spots:
            loc_lines.append("주변 장소 실제 결과: " + _fmt(spots))
        if loc_lines:
            parts.append(
                "[검증된 현지 정보 — 네이버 지역검색 실제 결과. 이 사실에 어긋나는 단정 금지]\n"
                + "\n".join("  - " + l for l in loc_lines)
                + "\n→ '대표 메뉴/시그니처'를 단정하지 말고 위 실제 분포에 맞게만 서술.")

    # 2) 백과사전 — 정의·연대·유래(항상)
    encyc, seen = [], set()
    for it in naver_search("encyc", topic or place, settings, 3):
        title = _strip(it.get("title", "")); desc = _strip(it.get("description", ""))[:150]
        if title and desc and title not in seen:
            seen.add(title); encyc.append(f"{title}: {desc}")
    if encyc:
        parts.append(
            "[백과사전 사실 — 연대·유래·정의는 이 내용을 따르고, 어긋나는 단정 금지]\n"
            + "\n".join("  · " + e for e in encyc[:3]))

    # 3) 뉴스 — 최근 소식(참고용)
    news = [_strip(it.get("title", "")) for it in naver_search("news", topic or place, settings, 2)]
    news = [t for t in news if t][:2]
    if news:
        parts.append("[최근 뉴스(참고) ] " + " / ".join(news))

    if not parts:
        return ""
    log(f"   🔎 사실 검증: 네이버 지역·백과사전·뉴스 근거 반영")
    return "\n\n".join(parts) + "\n"
