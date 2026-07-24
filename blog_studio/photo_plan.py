# -*- coding: utf-8 -*-
"""
photo_plan.py — 생성된 글에 어울리는 '촬영 목록(샷 리스트)'을 만들어 줍니다.

직접 찍을 사진을 계획할 수 있게, 전체 주제 1장 + 소주제별 1장(보통 6장)에 대해
'무엇을·어떤 구도·시간대·분위기로 찍으면 좋은지' 한/영 설명과 검색 키워드를 만듭니다.
결과는 화면 표시·클립보드 복사·CSV/Markdown 파일 저장에 쓰입니다.

generate_shot_list(cfg, settings, log) → [{slot, heading, description_ko, description_en, search_en}]
shot_list_markdown(...) / shot_list_csv(...) → 내보내기 텍스트
"""

import re
import io
import csv

import blog_core as core

SHOT_SYSTEM = (
    "당신은 여행·문화 콘텐츠의 포토 디렉터입니다. 글에 어울리는 사진을 운영자가 직접 찍을 수 있도록 "
    "구체적인 촬영 가이드(피사체·구도·시간대·분위기)를 한국어와 영어로 제시합니다. "
    "지정한 JSON 하나만 출력하세요."
)


def section_headings(cfg: dict) -> list:
    """생성된 글(cfg)의 소주제 제목(<h2>)을 추출(갤러리·지도 등 제외)."""
    body = cfg.get("body_ko", "") or ""
    heads = re.findall(r"<h2>(.*?)</h2>", body, flags=re.S)
    out = []
    for h in heads:
        h = re.sub(r"<[^>]+>", "", h).strip()
        if not h or "갤러리" in h or "지도" in h or "📷" in h or "📍" in h:
            continue
        out.append(h)
    return out


def _shot_prompt(title, topic, identity, headings) -> str:
    head_lines = "\n".join(f"  {i+1}. {h}" for i, h in enumerate(headings)) or "  (소주제 없음)"
    return f"""[블로그 색깔]
{identity}

[글 제목] {title}
[전체 주제] {topic}
[소주제 목록]
{head_lines}

이 글에 들어갈 사진을 운영자가 '직접 찍을' 수 있도록 촬영 목록을 만드세요.
- 맨 앞 1장은 '전체 주제 대표(히어로) 사진', 그다음 각 소주제마다 1장씩.
- 각 사진마다: 무엇을 피사체로, 어떤 구도·앵글, 어떤 시간대·빛·분위기로 찍으면 좋은지 구체적으로.
- search_en 은 무료 이미지 사이트에서 찾을 때 쓸 영어 검색어(직접 못 찍을 때 대체용).

아래 JSON만 출력(설명·코드펜스 없이):

{{
  "shots": [
    {{"slot": "전체", "heading": "전체 주제", "description_ko": "촬영 가이드(한국어)", "description_en": "shot guide (English)", "search_en": "image search keywords"}}
  ]
}}

규칙:
- shots[0] 은 반드시 전체 대표 사진. 이후 소주제 순서대로 1장씩(소주제 수만큼).
- description 은 한두 문장으로 구체적이고 실용적으로(막연한 미사여구 금지).
{core.JSON_SAFE}
JSON 외 텍스트는 절대 출력하지 마세요."""


def generate_shot_list(cfg: dict, settings: dict, log=print) -> list:
    """cfg(생성된 글)에 맞는 촬영 목록을 생성해 리스트로 반환."""
    title = cfg.get("ko_title") or cfg.get("topic") or ""
    topic = cfg.get("topic") or title
    identity = core._identity(settings) or ""
    headings = section_headings(cfg)
    prompt = _shot_prompt(title, topic, identity, headings)

    d, last = None, None
    for attempt in range(3):
        try:
            d = core._extract_json(core._complete(settings, prompt, log, SHOT_SYSTEM))
            if d.get("shots"):
                break
            last = "shots 비어 있음"
        except Exception as e:
            last = str(e)
            log(f"      ↻ 촬영 목록 재시도({attempt + 1}/3): {e}")
        d = None
    if not d:
        raise ValueError(f"촬영 목록 생성 실패: {last}")

    shots = []
    for i, s in enumerate(d["shots"]):
        shots.append({
            "slot": (s.get("slot") or ("전체" if i == 0 else f"소주제 {i}")).strip(),
            "heading": (s.get("heading") or "").strip(),
            "description_ko": (s.get("description_ko") or "").strip(),
            "description_en": (s.get("description_en") or "").strip(),
            "search_en": (s.get("search_en") or "").strip(),
        })
    log(f"   📸 촬영 목록 {len(shots)}장 생성")
    return shots


# ── 내보내기 ──────────────────────────────────────────────────────────────────
def shot_list_markdown(blog_name, date_str, topic, shots) -> str:
    lines = [f"# 촬영 목록 — {blog_name}", "",
             f"- 날짜: {date_str}", f"- 주제: {topic}", "",
             "| # | 위치 | 제목 | 촬영 가이드 | 영어 검색어 |",
             "|---|------|------|-------------|-------------|"]
    for i, s in enumerate(shots):
        guide = s["description_ko"].replace("|", "/")
        lines.append(f"| {i+1} | {s['slot']} | {s['heading'].replace('|','/')} "
                     f"| {guide} | {s['search_en'].replace('|','/')} |")
    return "\n".join(lines)


def shot_list_csv(blog_name, date_str, topic, shots) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["blog", "date", "topic", "no", "slot", "heading",
                "description_ko", "description_en", "search_en"])
    for i, s in enumerate(shots):
        w.writerow([blog_name, date_str, topic, i + 1, s["slot"], s["heading"],
                    s["description_ko"], s["description_en"], s["search_en"]])
    return buf.getvalue()


def shot_list_plain(blog_name, date_str, topic, shots) -> str:
    """클립보드/화면용 사람이 읽기 좋은 텍스트."""
    out = [f"📸 촬영 목록 — {blog_name} / {date_str}", f"주제: {topic}", ""]
    for i, s in enumerate(shots):
        out.append(f"[{i+1}] {s['slot']} — {s['heading']}")
        out.append(f"    촬영: {s['description_ko']}")
        if s["search_en"]:
            out.append(f"    검색어: {s['search_en']}")
        out.append("")
    return "\n".join(out)
