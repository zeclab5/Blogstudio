# -*- coding: utf-8 -*-
"""
title_card.py — 단어 사전 글의 '타이틀 카드' 이미지 생성(Pillow, 로컬·무료).

단어 글은 개념이라 어울리는 사진이 없습니다(억지 매칭 시 엉뚱한 이미지). 그래서
깔끔한 배경에 **한글 + 영어(로마자) 단어**를 크게 얹은 일관된 히어로 카드를 만듭니다.
ComfyUI/SD 불필요 — Windows 기본 폰트(맑은 고딕)로 텍스트만 렌더링.

핵심
  make_word_card(word_ko, word_en, out_path, subtitle="", brand=...) -> 경로(실패 시 "")
"""

import sys
from pathlib import Path

# 카드 규격(OG 친화 16:9). 색은 차분한 라이트 테마.
_W, _H = 1200, 675
_BG_TOP = (247, 249, 252)      # 거의 흰색(살짝 푸른 기운)
_BG_BOT = (231, 237, 246)      # 옅은 블루그레이
_INK = (26, 38, 56)            # 짙은 네이비(단어)
_ACCENT = (74, 144, 217)       # 포인트 블루
_MUTED = (120, 132, 150)       # 보조 텍스트

# 한글 지원 폰트 — OS별 후보(위에서부터 시도, 존재하는 첫 것 사용). 맥 이식 대응(2026-07-24):
# 실제 맥에서 검증 전이라 AppleSDGothicNeo.ttc의 굵게(index)는 시스템 버전에 따라 다를 수
# 있음 — _font()가 실패하면 자동으로 REGULAR로 폴백하므로 최악의 경우도 '안 굵을 뿐' 안전.
if sys.platform == "darwin":
    _FONT_CANDIDATES_BOLD = [
        (Path("/System/Library/Fonts/Supplemental/AppleSDGothicNeo.ttc"), 6),
        (Path("/System/Library/Fonts/Supplemental/AppleGothic.ttf"), 0),
    ]
    _FONT_CANDIDATES_REG = [
        (Path("/System/Library/Fonts/Supplemental/AppleSDGothicNeo.ttc"), 3),
        (Path("/System/Library/Fonts/Supplemental/AppleGothic.ttf"), 0),
    ]
else:
    _FONT_DIR = Path(r"C:\Windows\Fonts")
    _FONT_CANDIDATES_BOLD = [(_FONT_DIR / "malgunbd.ttf", 0)]
    _FONT_CANDIDATES_REG = [(_FONT_DIR / "malgun.ttf", 0)]


def _font(bold: bool, size: int):
    from PIL import ImageFont
    for path, idx in (_FONT_CANDIDATES_BOLD if bold else _FONT_CANDIDATES_REG):
        if not path.exists():
            continue
        try:
            return ImageFont.truetype(str(path), size, index=idx)
        except Exception:
            continue
    if bold:   # 굵게 전부 실패 — 레귤러로 폴백(레귤러도 실패하면 아래 for가 또 폴백)
        for path, idx in _FONT_CANDIDATES_REG:
            if path.exists():
                try:
                    return ImageFont.truetype(str(path), size, index=idx)
                except Exception:
                    continue
    return ImageFont.load_default()


def _text_w(draw, text, font) -> int:
    return draw.textbbox((0, 0), text, font=font)[2]


def _truncate_words(text: str, max_len: int) -> str:
    """단어 중간에서 끊기지 않도록 단어 경계에서 자르고, 잘렸으면 말줄임표를 붙인다.
    (예전엔 text[:20] 하드컷이라 영어는 'A deep emotional bon'처럼 단어 중간이 잘렸음)"""
    text = (text or "").strip()
    if len(text) <= max_len:
        return text
    cut = text[:max_len]
    sp = cut.rfind(" ")
    if sp > max_len * 0.4:
        cut = cut[:sp]
    return cut.rstrip(",.;:，、") + "…"


def _fit_font(draw, text, bold, start_size, max_width, min_size=40):
    """텍스트가 max_width 안에 들도록 폰트 크기를 줄여가며 맞춘다."""
    size = start_size
    while size > min_size:
        f = _font(bold, size)
        if _text_w(draw, text, f) <= max_width:
            return f
        size -= 4
    return _font(bold, min_size)


def _wrap_lines(draw, text, font, max_width):
    """단어 단위로 줄바꿈 처리. 한글은 공백 기준, max_width 초과 시 다음 줄로."""
    words = text.split()
    lines, cur = [], ""
    for word in words:
        test = (cur + " " + word).strip()
        if _text_w(draw, test, font) <= max_width:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines or [text]


def _gradient_bg(img):
    """위→아래 부드러운 세로 그라데이션 배경."""
    for y in range(_H):
        t = y / _H
        r = int(_BG_TOP[0] + (_BG_BOT[0] - _BG_TOP[0]) * t)
        g = int(_BG_TOP[1] + (_BG_BOT[1] - _BG_TOP[1]) * t)
        b = int(_BG_TOP[2] + (_BG_BOT[2] - _BG_TOP[2]) * t)
        for x in range(_W):
            img.putpixel((x, y), (r, g, b))


def make_word_card(word_ko: str, word_en: str, out_path: str,
                   subtitle: str = "", brand: str = "한국 문화 단어 사전 · K-CULTURE DICTIONARY",
                   log=print) -> str:
    """깔끔한 배경 + 한글/영어 단어 카드 PNG 생성. 성공 시 경로, 실패 시 ""."""
    word_ko = (word_ko or "").strip()
    word_en = (word_en or "").strip()
    if not word_ko and not word_en:
        return ""
    try:
        from PIL import Image, ImageDraw
    except Exception as e:
        log(f"   ⚠️ Pillow 없음 — 타이틀 카드 생략: {e}")
        return ""
    try:
        img = Image.new("RGB", (_W, _H), _BG_TOP)
        # 그라데이션은 픽셀단위라 느릴 수 있어, 빠른 방식(행 단위 paste)으로
        grad = Image.new("RGB", (1, _H))
        for y in range(_H):
            t = y / _H
            grad.putpixel((0, y), (
                int(_BG_TOP[0] + (_BG_BOT[0] - _BG_TOP[0]) * t),
                int(_BG_TOP[1] + (_BG_BOT[1] - _BG_TOP[1]) * t),
                int(_BG_TOP[2] + (_BG_BOT[2] - _BG_TOP[2]) * t)))
        img = grad.resize((_W, _H))
        draw = ImageDraw.Draw(img)

        margin = 90
        maxw = _W - margin * 2

        # 상단 브랜드 라벨(작게, 자간 느낌으로 대문자 위주)
        bf = _font(False, 30)
        bw = _text_w(draw, brand, bf)
        draw.text(((_W - bw) // 2, 70), brand, font=bf, fill=_MUTED)

        # 가운데 한글 단어(크게, 볼드, 폭에 맞춰 자동 축소)
        kf = _fit_font(draw, word_ko or word_en, True, 220, maxw, min_size=72)
        kbbox = draw.textbbox((0, 0), word_ko or word_en, font=kf)
        kw_w, kw_h = kbbox[2] - kbbox[0], kbbox[3] - kbbox[1]
        ky = _H // 2 - kw_h // 2 - 40
        draw.text(((_W - kw_w) // 2 - kbbox[0], ky - kbbox[1]),
                  word_ko or word_en, font=kf, fill=_INK)

        # 영어/로마자(단어 아래, 너무 크지 않게) — 밑줄(강조선) 없이
        word_bottom = ky + kw_h
        if word_en and word_en != word_ko:
            ef = _fit_font(draw, word_en, False, 56, maxw, min_size=34)
            ew = _text_w(draw, word_en, ef)
            ey = word_bottom + 42
            draw.text(((_W - ew) // 2, ey), word_en, font=ef, fill=_ACCENT)
            sub_y = ey + 56 + 32        # 설명글을 영문 아래로 충분히 띄움(겹침 방지)
        else:
            sub_y = word_bottom + 48

        # 부제(의미 요약, 있으면) — 최대 3줄 자동 줄바꿈
        subtitle = (subtitle or "").strip()
        if subtitle:
            sf = _font(False, 30)
            lines = _wrap_lines(draw, subtitle, sf, maxw)
            sb = draw.textbbox((0, 0), "가", font=sf)
            line_h = (sb[3] - sb[1]) + 8
            for i, line in enumerate(lines[:3]):
                lw = _text_w(draw, line, sf)
                draw.text(((_W - lw) // 2, sub_y + i * line_h), line, font=sf, fill=_MUTED)

        out_path = str(out_path)
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        img.save(out_path, format="PNG")
        return out_path
    except Exception as e:
        log(f"   ⚠️ 타이틀 카드 생성 실패: {e}")
        return ""


def _pill(draw, cx, cy, text_main, text_sub, font_main, font_sub,
          pad_x=28, pad_y=16, fill=None, text_color=_INK, sub_color=None):
    """가운데(cx,cy) 정렬 둥근 알약 + 텍스트(+작은 보조텍스트). 알약의 (left,top,right,bottom) 반환."""
    mw = _text_w(draw, text_main, font_main)
    mb = draw.textbbox((0, 0), text_main, font=font_main)
    mh = mb[3] - mb[1]
    sw = sh = 0
    if text_sub:
        sw = _text_w(draw, text_sub, font_sub)
        sb = draw.textbbox((0, 0), text_sub, font=font_sub)
        sh = (sb[3] - sb[1]) + 6
    w = max(mw, sw) + pad_x * 2
    h = mh + sh + pad_y * 2
    left, top = cx - w / 2, cy - h / 2
    right, bottom = cx + w / 2, cy + h / 2
    if fill:
        draw.rounded_rectangle([left, top, right, bottom], radius=h / 2, fill=fill)
    ty = cy - (mh + sh) / 2
    draw.text((cx - mw / 2 - mb[0], ty - mb[1]), text_main, font=font_main, fill=text_color)
    if text_sub:
        draw.text((cx - sw / 2, ty + mh + 6), text_sub, font=font_sub,
                  fill=sub_color or _MUTED)
    return (left, top, right, bottom)


def _pill_size(draw, text_main, text_sub, font_main, font_sub, pad_x=28, pad_y=16):
    """그릴 알약의 (너비, 높이)를 미리 계산(겹치지 않게 배치하려고)."""
    mw = _text_w(draw, text_main, font_main)
    mb = draw.textbbox((0, 0), text_main, font=font_main)
    mh = mb[3] - mb[1]
    sw = sh = 0
    if text_sub:
        sw = _text_w(draw, text_sub, font_sub)
        sb = draw.textbbox((0, 0), text_sub, font=font_sub)
        sh = (sb[3] - sb[1]) + 6
    return max(mw, sw) + pad_x * 2, mh + sh + pad_y * 2


def make_related_card(main_ko: str, related, out_path: str,
                      label: str = "연결된 단어 · CONNECTED WORDS", lang: str = "ko",
                      log=print) -> str:
    """주제 단어(가운데)에서 연관 단어들로 선이 뻗는 네트워크 카드. 성공 시 경로, 실패 시 "".
    lang="ko"면 related[i]["desc"](한국어 설명), lang="en"이면 related[i]["desc_en"](영어 설명)를
    그린다 — 카드가 두 언어 글에 따로 쓰이므로 설명 텍스트도 언어별로 구워야 함."""
    import math
    main_ko = (main_ko or "").strip()
    desc_key = "desc" if lang == "ko" else "desc_en"
    rel = []
    for r in (related or []):
        if isinstance(r, dict):
            ko = (r.get("ko") or "").strip()
            en = (r.get("en") or "").strip()
            desc = (r.get(desc_key) or "").strip()
        else:
            ko = str(r).strip(); en = ""; desc = ""
        if ko:
            rel.append((ko, en, desc))
    rel = rel[:4]
    if not main_ko or not rel:
        return ""
    try:
        from PIL import Image, ImageDraw
    except Exception as e:
        log(f"   ⚠️ Pillow 없음 — 연관 단어 카드 생략: {e}")
        return ""
    try:
        grad = Image.new("RGB", (1, _H))
        for y in range(_H):
            t = y / _H
            grad.putpixel((0, y), (
                int(_BG_TOP[0] + (_BG_BOT[0] - _BG_TOP[0]) * t),
                int(_BG_TOP[1] + (_BG_BOT[1] - _BG_TOP[1]) * t),
                int(_BG_TOP[2] + (_BG_BOT[2] - _BG_TOP[2]) * t)))
        img = grad.resize((_W, _H))
        draw = ImageDraw.Draw(img)

        # 상단 라벨
        lf = _font(False, 30)
        lw = _text_w(draw, label, lf)
        draw.text(((_W - lw) // 2, 60), label, font=lf, fill=_MUTED)

        cx, cy = _W // 2, _H // 2 + 6
        sef = _font(False, 23)                                  # 로마자(알약 안, 한글 아래)
        mf = _fit_font(draw, main_ko, True, 86, 380, min_size=48)   # 가운데 단어
        cw, ch = _pill_size(draw, main_ko, "", mf, sef, pad_x=40, pad_y=22)

        # 연관 단어 알약 = 한글 + 아래 로마자. 설명글은 알약 '아래'(바깥)에. 크기 재서 겹치지 않게.
        sat = []
        for ko, en, desc in rel:
            f = _fit_font(draw, ko, True, 46, 280, min_size=28)
            w, h = _pill_size(draw, ko, en, f, sef, pad_x=26, pad_y=14)
            sat.append((ko, en, desc, f, w, h))

        n = len(sat)
        hub = (cx, cy)
        placed = []
        if n == 2:
            # 허브+좌우 2단어(화면엔 3글자): 가로를 세 칸으로 나눠 중앙 칸=허브(그대로),
            # 좌/우 칸 중앙에 각 위성 단어(선이 짧아 중앙에 몰려 보이는 문제 방지).
            col_w = _W / 3
            xs = [col_w * 0.5, col_w * 2.5]
            for (ko, en, desc, f, w, h), x in zip(sat, xs):
                placed.append((ko, en, desc, f, h, (x, cy)))
        elif n == 3:
            # 세 단어: 주제 단어는 위쪽으로, 카드를 세 칸으로 나눠 각 칸 중앙에 한 단어씩
            # (선이 짧아 중앙에 몰려 보이는 문제 방지 — 칸 중앙 배치로 폭 전체를 씀).
            hub = (cx, 205)
            row_y = 480
            col_w = _W / 3
            xs = [col_w * 0.5, col_w * 1.5, col_w * 2.5]
            for (ko, en, desc, f, w, h), x in zip(sat, xs):
                placed.append((ko, en, desc, f, h, (x, row_y)))
        else:
            gap = 50
            slots = {1: ["right"]}.get(n, ["left", "right", "top", "bottom"])
            for (ko, en, desc, f, w, h), slot in zip(sat, slots):
                if slot == "left":
                    pos = (cx - (cw / 2 + w / 2 + gap), cy)        # 가로=폭으로 간격
                elif slot == "right":
                    pos = (cx + (cw / 2 + w / 2 + gap), cy)
                elif slot == "top":
                    pos = (cx, cy - (ch / 2 + h / 2 + gap))        # 세로=높이로 간격
                else:
                    pos = (cx, cy + (ch / 2 + h / 2 + gap))
                placed.append((ko, en, desc, f, h, pos))

        # 연결선(알약 뒤) + 중간 점(네트워크 느낌)
        for ko, en, desc, f, h, (x, y) in placed:
            draw.line([hub, (x, y)], fill=(176, 198, 226), width=3)
            mx, my = (hub[0] + x) / 2, (hub[1] + y) / 2
            draw.ellipse([mx - 4, my - 4, mx + 4, my + 4], fill=_ACCENT)

        # 연관 단어 알약(한글 + 로마자) + 알약 아래 설명글
        for ko, en, desc, f, h, (x, y) in placed:
            _pill(draw, x, y, ko, en, f, sef, fill=(255, 255, 255),
                  text_color=_INK, sub_color=_ACCENT)
            if desc:
                has_hangul = any("가" <= c <= "힣" for c in desc)
                dtext = _truncate_words(desc, 24 if has_hangul else 42)
                dff = _fit_font(draw, dtext, False, 20, 320, min_size=14)
                dw = _text_w(draw, dtext, dff)
                draw.text((x - dw / 2, y + h / 2 + 12), dtext, font=dff, fill=_MUTED)

        # 주제 단어 알약(포인트색) — 맨 위에 덮어 그림
        _pill(draw, hub[0], hub[1], main_ko, "", mf, sef, pad_x=40, pad_y=24,
              fill=_ACCENT, text_color=(255, 255, 255))

        out_path = str(out_path)
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        img.save(out_path, format="PNG")
        return out_path
    except Exception as e:
        log(f"   ⚠️ 연관 단어 카드 생성 실패: {e}")
        return ""


if __name__ == "__main__":
    # 샘플 생성(눈으로 확인용) — 이 모듈 폴더에 저장
    p = make_word_card("눈치", "Nunchi",
                       str(Path(__file__).resolve().parent / "_card_sample.png"),
                       subtitle="말 안 해도 분위기를 읽는 한국식 센스")
    print("saved:", p)
