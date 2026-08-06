# -*- coding: utf-8 -*-
"""
html_card.py — HTML/CSS로 설계한 화면을 헤드리스 브라우저로 캡처해 '유니크한 대표 이미지'를
만든다(2026-08-03).

왜 만드나
  일반 여행·정보 글은 직접 찍은 사진이 없으면 관광공사·무료스톡에서 아무 사진이나 끌어와
  본문과 연관성이 낮은 이미지가 붙는 문제가 있었다. 그런 자리에 '엉뚱한 사진' 대신 글의
  제목·장소를 담은 깔끔한 카드를 넣으면, 내용과 정확히 일치하면서 이 블로그에만 존재하는
  고유 이미지가 된다(같은 사진이 여러 블로그에 중복 노출되는 저품질 신호도 피함).

왜 Playwright인가(Puppeteer 아님)
  이 프로젝트는 전부 파이썬이고 Playwright가 이미 발행·업로드에 쓰이고 있다. Puppeteer는
  Node.js 런타임을 새로 얹어야 하는데, 화면을 띄워 screenshot을 찍는 원리와 결과물은 동일해
  이미 있는 Playwright를 그대로 재사용한다.

기존 title_card.py(Pillow)와의 차이
  title_card는 단어사전 글 전용으로 좌표를 계산해 텍스트를 직접 그린다. 이 모듈은 HTML/CSS를
  쓰므로 그라데이션·자간·그림자·자동 줄바꿈 같은 표현을 훨씬 쉽게 다루고, 템플릿만 바꾸면
  카드 종류를 늘릴 수 있다. 둘은 공존한다(단어글=title_card, 일반글=html_card).

핵심
  make_hero_card(title, subtitle, brand, out_path, ...) -> 저장 경로(실패 시 "")
  is_available() -> Playwright 사용 가능 여부
"""

import html as _html
import sys
from pathlib import Path

# 카드 규격 — OG/트위터 카드 친화 16:9. device_scale_factor=2로 실제 2400x1350 저장(선명).
CARD_W, CARD_H = 1200, 675
_SCALE = 2

# 블로그별 색 테마(브랜드 톤). 키는 blogs.json의 블로그 이름 일부와 매칭.
_THEMES = {
    "arts":    {"bg": ("#1a2638", "#2c4a6e", "#4a90d9"), "accent": "#7fc4ff"},
    "culture": {"bg": ("#2b1f36", "#4a3260", "#7e57a6"), "accent": "#c9a7e8"},
    "dance":   {"bg": ("#331f28", "#5c2f42", "#9c4368"), "accent": "#ff9ec4"},
    "default": {"bg": ("#1f2a33", "#33505e", "#4f8fa8"), "accent": "#8fd4e8"},
}

# 한글이 반드시 렌더링되도록 OS별 폰트를 순서대로 지정(맥 이식 대응).
_FONT_STACK = ('"Malgun Gothic","맑은 고딕","Apple SD Gothic Neo","AppleGothic",'
               '"Noto Sans KR","Nanum Gothic",sans-serif')


def is_available() -> bool:
    """Playwright(+Chromium)를 쓸 수 있는지. 없으면 카드 생성을 조용히 건너뛰면 된다."""
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
        return True
    except Exception:
        return False


def theme_for(brand: str) -> dict:
    b = (brand or "").lower()
    for key in ("arts", "culture", "dance"):
        if key in b:
            return _THEMES[key]
    return _THEMES["default"]


def _fit_title_px(title: str) -> int:
    """제목 길이에 따라 글자 크기를 줄여 카드 밖으로 넘치지 않게 한다.
    (CSS만으로는 '길면 자동 축소'가 안 되므로 글자 수 기준으로 미리 정한다.)"""
    n = len(title or "")
    if n <= 12:
        return 96
    if n <= 18:
        return 78
    if n <= 26:
        return 64
    if n <= 36:
        return 54
    return 46


def _hero_html(title: str, subtitle: str, brand: str) -> str:
    th = theme_for(brand)
    c1, c2, c3 = th["bg"]
    esc = _html.escape
    sub_block = (f'<div class="sub">{esc(subtitle)}</div>' if (subtitle or "").strip() else "")
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ width:{CARD_W}px; height:{CARD_H}px; display:flex; align-items:center;
  justify-content:center; overflow:hidden;
  background:linear-gradient(135deg,{c1} 0%,{c2} 55%,{c3} 100%);
  font-family:{_FONT_STACK}; }}
/* 은은한 대각선 패턴 — 단색 배경보다 덜 밋밋하고 인쇄물 느낌을 준다 */
body::before {{ content:""; position:absolute; inset:0; opacity:.06;
  background:repeating-linear-gradient(45deg,#fff 0 2px,transparent 2px 22px); }}
.card {{ position:relative; text-align:center; color:#fff; padding:0 90px; max-width:100%; }}
.label {{ font-size:21px; letter-spacing:.34em; opacity:.72; margin-bottom:26px;
  font-weight:500; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
.title {{ font-size:{_fit_title_px(title)}px; font-weight:800; line-height:1.18;
  text-shadow:0 4px 26px rgba(0,0,0,.38); word-break:keep-all; }}
.sub {{ font-size:29px; margin-top:26px; opacity:.9; font-weight:300; line-height:1.5;
  word-break:keep-all; }}
.rule {{ width:118px; height:5px; background:{th['accent']}; margin:34px auto 0;
  border-radius:3px; }}
</style></head><body><div class="card">
<div class="label">{esc(brand)}</div>
<div class="title">{esc(title)}</div>
{sub_block}
<div class="rule"></div>
</div></body></html>"""


def _capture(html_str: str, out_path: str, log=print) -> str:
    """HTML 문자열을 헤드리스 브라우저로 띄워 PNG로 캡처. 실패 시 "" 반환."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        log(f"   ⚠️ Playwright 없음 — 카드 생성 건너뜀: {e}")
        return ""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page(
                    viewport={"width": CARD_W, "height": CARD_H},
                    device_scale_factor=_SCALE)
                page.set_content(html_str, wait_until="load")
                page.screenshot(path=str(out))
            finally:
                browser.close()
    except Exception as e:
        log(f"   ⚠️ 카드 캡처 실패: {e}")
        return ""
    return str(out) if out.exists() else ""


def make_hero_card(title: str, out_path: str, subtitle: str = "", brand: str = "",
                   log=print) -> str:
    """글의 대표(히어로) 카드 이미지를 만들어 저장 경로를 반환(실패 시 "").
    title: 카드에 크게 들어갈 제목(글 제목 또는 장소)
    subtitle: 한 줄 부제(로마자 표기·요약 등, 없어도 됨)
    brand: 상단 작은 라벨(블로그 이름)"""
    title = (title or "").strip()
    if not title:
        return ""
    return _capture(_hero_html(title, subtitle, brand), out_path, log)
