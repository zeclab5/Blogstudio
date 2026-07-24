# -*- coding: utf-8 -*-
"""
curator.py — 이벤트 DB의 공연·전시를 'k-culture-now 시의성 큐레이션 글'로 변환 (v6 §4).

· 주어진 '이벤트 사실'만 사용해 정확히 안내(없는 공연·날짜를 지어내지 않음).
· 카테고리별 분량·톤(v6 §4.4)을 반영해 한/영 동시 작성.
· 결과 cfg는 generate_post 결과와 같은 필드라 기존 [미리보기]·[발행] 흐름과 호환.

generate_curation_post(category_key, events, settings, ref_date, log) → cfg dict
"""

import re
from datetime import date

import blog_core as core

# 카테고리별 분량·톤·각도 (v6 §4.4 / §4.2)
CATEGORY_SPEC = {
    "monthly_preview": {"ko": 800, "en": 500, "tone": "다음 달 전체 라인업을 종합해 미리 보여주는 안내", "angle": "D-30 · 다음 달 미리보기"},
    "coming_up":       {"ko": 600, "en": 400, "tone": "예매가 막 열린 인기 공연을 콕 집어 추천하는 실용 안내", "angle": "D-21 · 예매 시작"},
    "decision_time":   {"ko": 500, "en": 350, "tone": "갈지 말지 결정을 돕는 상세 일정·교통·관람 팁 안내", "angle": "D-14 · 관람 결정"},
    "this_week":       {"ko": 500, "en": 300, "tone": "이번 주 가볼 만한 것을 간결하게 추리는 안내", "angle": "D-7 · 이번 주"},
    "weekend_picks":   {"ko": 400, "en": 250, "tone": "이번 주말 한정으로 즉시 결정하게 돕는 짧고 강한 안내", "angle": "D-2 · 이번 주말"},
    "festival_watch":  {"ko": 1000, "en": 600, "tone": "다가오는 큰 페스티벌을 사전에 깊이 있게 안내", "angle": "장기 · 페스티벌 사전 알림"},
}

CURATION_SYSTEM = (
    "당신은 한국을 찾는 외국인을 위한 공연·전시 큐레이터입니다. "
    "주어진 '이벤트 사실'만 사용해 정확하고 실용적인 한/영 이중언어 안내 글을 씁니다. "
    "목록에 없는 공연·날짜·장소를 절대 지어내지 마세요. 한국어와 영어를 같은 깊이로 작성하고, "
    "지정한 JSON 하나만 출력하세요."
)


def _price_ko(p):
    return {"free": "무료", "paid": "유료", "mixed": "일부 무료"}.get((p or "").strip(), (p or "").strip())


def _events_block(events: list) -> str:
    """프롬프트에 넣을 '이벤트 사실' 목록(모델은 이 사실만 사용)."""
    lines = []
    for i, e in enumerate(events, 1):
        period = e.get("start_date", "")
        if e.get("end_date"):
            period += f" ~ {e['end_date']}"
        parts = [f"{i}. {e.get('title_ko','')}"]
        if e.get("title_en"):
            parts.append(f"(EN: {e['title_en']})")
        meta = [f"기간 {period}"]
        if e.get("venue"):
            meta.append(f"장소 {e['venue']}")
        if e.get("region"):
            meta.append(f"지역 {e['region']}")
        if e.get("price"):
            meta.append(f"요금 {_price_ko(e['price'])}")
        if e.get("category"):
            meta.append(f"장르 {e['category']}")
        if e.get("booking_url"):
            meta.append(f"안내 {e['booking_url']}")
        lines.append("  ".join(parts) + "\n     - " + " · ".join(meta)
                     + (f"\n     - 설명: {e['description'][:120]}" if e.get("description") else ""))
    return "\n".join(lines)


def _curation_prompt(category_key, events, identity, ref_date) -> str:
    spec = CATEGORY_SPEC.get(category_key, CATEGORY_SPEC["this_week"])
    return f"""[블로그 색깔]
{identity}

[글 종류] {spec['angle']} — {spec['tone']}
[기준 날짜] {ref_date}
[분량] 한국어 약 {spec['ko']}자, 영어 약 {spec['en']} 단어 (간결하고 정보 밀도 높게)

[이 글에 담을 이벤트 사실 — 이 목록만 사용, 추가/창작 금지]
{_events_block(events)}

위 '이벤트 사실'만으로 외국인 독자를 위한 시의성 큐레이션 글을 한국어와 영어로 작성하세요.
아래 JSON 하나만 출력(설명·코드펜스 없이):

{{
  "ko_title": "한국어 제목(이 글 종류·시기가 드러나게)",
  "en_title": "영어 제목",
  "ko_meta": "검색 설명(한국어 1문장)",
  "en_meta": "검색 설명(영어 1문장)",
  "ko_slug": "english-slug-for-ko",
  "en_slug": "english-slug-for-en",
  "ko_labels": ["라벨 3~6개"],
  "en_labels": ["labels 3~6"],
  "body_ko": "한국어 본문 HTML",
  "body_en": "영어 본문 HTML"
}}

작성 규칙:
- 제목에는 'D-7'·'D-30' 같은 내부 코드나 대괄호 표시를 넣지 마세요(독자용 자연스러운 제목).
- 첫 문단은 '{spec['angle']}'에 맞는 후킹 도입(이 글이 왜 지금 유용한지).
- 각 이벤트를 <h3>제목</h3> + 짧은 소개 + <ul>에 날짜·장소·요금(무료 여부)·예매/안내를 정확히 정리.
- 날짜·장소·요금은 위 사실을 그대로 쓰고, 모르면 적지 말 것(추측 금지).
- 외국인 관점의 실용 팁(교통·언어·무료 여부)을 자연스럽게. 마지막에 가벼운 마무리.
- 본문은 <p>,<h3>,<ul>,<li>,<strong>,<a> 만 사용. 한국어·영어 동일한 깊이.
{core.JSON_SAFE}
JSON 외 텍스트는 절대 출력하지 마세요."""


def generate_curation_post(category_key, events, settings, ref_date=None, log=print) -> dict:
    """이벤트 목록으로 시기별 큐레이션 글(cfg)을 생성. events가 비면 ValueError."""
    if not events:
        raise ValueError("큐레이션할 이벤트가 없습니다.")
    ref_date = ref_date or date.today().isoformat()
    identity = core._identity(settings) or "K-Culture Now — 외국인 대상 한국 공연·전시 시의성 큐레이션"
    prompt = _curation_prompt(category_key, events, identity, ref_date)
    label = CATEGORY_SPEC.get(category_key, {}).get("angle", category_key)

    d, last = None, None
    for attempt in range(3):
        try:
            d = core._extract_json(core._complete(settings, prompt, log, CURATION_SYSTEM))
            if d.get("body_ko") and d.get("body_en"):
                break
            last = "본문이 비어 있음"
        except Exception as e:
            last = str(e)
            log(f"      ↻ 큐레이션 글 재시도({attempt + 1}/3): {e}")
        d = None
    if not d:
        raise ValueError(f"큐레이션 글 생성 실패: {last}")

    cfg = {
        "ko_title": (d.get("ko_title") or "").strip(),
        "en_title": (d.get("en_title") or "").strip(),
        "ko_meta": (d.get("ko_meta") or "").strip(),
        "en_meta": (d.get("en_meta") or "").strip(),
        "ko_slug": core._fix_slug(d.get("ko_slug"), "ko"),
        "en_slug": core._fix_slug(d.get("en_slug"), "en"),
        "ko_labels": core._norm_labels(d.get("ko_labels"), "ko"),
        "en_labels": core._norm_labels(d.get("en_labels"), "en"),
        "body_ko": d.get("body_ko") or "",
        "body_en": d.get("body_en") or "",
        "category": category_key,
        "location": "",
        "date": ref_date,
        "topic": label,
        "event_ids": [e.get("id") for e in events if e.get("id")],
    }
    log(f"   ✅ 큐레이션 글 생성 완료 — {label} / 이벤트 {len(events)}건 "
        f"(한 {len(cfg['body_ko']):,}자 / 영 {len(cfg['body_en']):,}자)")
    return cfg
