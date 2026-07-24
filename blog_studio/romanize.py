# -*- coding: utf-8 -*-
"""
romanize.py — 한글을 '국립국어원 로마자 표기법(Revised Romanization)'에 가깝게
결정적으로 변환. LLM 추측(예: 발림→"Baldim" 오류)을 대체해 단어 카드·word_en에 정확한 표기.

핵심 규칙만 구현(사전 용어 대부분 커버):
  · 음절 분해(초성·중성·종성) + 표준 표기표
  · 연음(받침 + ㅇ초성 → 받침이 다음 음절 초성으로)
  · ㄹㄹ/ㄴㄹ/ㄹㄴ → ll
  · 받침 ㄱ/ㄷ/ㅂ + ㄴ/ㅁ 비음화(→ ng/n/m + n/m)
검증된 예: 발림→Ballim, 살풀이→Salpuri, 신라→Silla, 한국어→Hangugeo, 단청→Dancheong.

  romanize(text, cap=True) → 로마자 문자열(한글 아닌 글자는 그대로)
"""

_INITIALS = ['g', 'kk', 'n', 'd', 'tt', 'r', 'm', 'b', 'pp', 's', 'ss',
             '', 'j', 'jj', 'ch', 'k', 't', 'p', 'h']
_MEDIALS = ['a', 'ae', 'ya', 'yae', 'eo', 'e', 'yeo', 'ye', 'o', 'wa', 'wae',
            'oe', 'yo', 'u', 'wo', 'we', 'wi', 'yu', 'eu', 'ui', 'i']
# 종성(받침) 표준 표기(발음 기준 7종 + 없음). 인덱스 0=받침없음.
_FINALS = ['', 'k', 'k', 'k', 'n', 'n', 'n', 't', 'l', 'k', 'm', 'l', 'l', 'l',
           'p', 'l', 'm', 'p', 'p', 't', 't', 'ng', 't', 't', 'k', 't', 'p', 't']
# 받침 자모가 '연음'되어 다음 음절 초성이 될 때의 소리(원래 자음값).
#   (홑받침 위주 — 사전 용어에 흔한 경우. 겹받침 연음은 근사치.)
_FINAL_AS_ONSET = {
    1: 'g', 2: 'kk', 4: 'n', 7: 'd', 8: 'r', 16: 'm', 17: 'b', 19: 's',
    20: 'ss', 21: 'ng', 22: 'j', 23: 'ch', 24: 'k', 25: 't', 26: 'p', 27: '',
    9: 'g', 10: 'm', 11: 'b', 17: 'b',
}


def _decompose(ch):
    """한글 음절 → (초성idx, 중성idx, 종성idx). 한글 아니면 None."""
    code = ord(ch) - 0xAC00
    if 0 <= code < 11172:
        return code // 588, (code % 588) // 28, code % 28
    return None


def romanize(text: str, cap: bool = True) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    # 음절 단위로 (초성, 중성, 종성) 추출. 한글 아닌 글자는 마커로 보관.
    syls = []
    for ch in text:
        d = _decompose(ch)
        syls.append(d if d else ch)

    out = []
    for i, s in enumerate(syls):
        if not isinstance(s, tuple):
            out.append(s)
            continue
        ini, med, fin = s
        ini_r = _INITIALS[ini]
        med_r = _MEDIALS[med]
        fin_r = _FINALS[fin]

        # 다음 음절과의 경계 규칙
        nxt = syls[i + 1] if i + 1 < len(syls) else None
        if isinstance(nxt, tuple):
            n_ini = nxt[0]
            if fin != 0:
                if n_ini == 11:            # 다음 초성 ㅇ → 연음(받침이 넘어감)
                    fin_r = ''             # 받침 소리는 다음 음절 초성으로 (아래서 처리)
                elif fin == 8 and n_ini == 5:      # ㄹ + ㄹ → ll
                    pass                            # 둘 다 l → 자연히 ll
                elif fin == 8 and n_ini == 2:      # ㄹ + ㄴ → ll
                    pass                            # 다음 ㄴ을 l로 바꾸는 건 아래 처리
        # 연음: 이전 음절 종성을 이 음절 초성으로 끌어오기
        prev = syls[i - 1] if i - 1 >= 0 else None
        if isinstance(prev, tuple) and ini == 11 and prev[2] != 0:
            ini_r = _FINAL_AS_ONSET.get(prev[2], _FINALS[prev[2]])
        # ㄴ/ㄹ 동화: 이전 종성이 ㄹ이고 이번 초성이 ㄴ → ㄴ을 l로
        if isinstance(prev, tuple) and ini == 2 and prev[2] == 8:
            ini_r = 'l'
        # 이전 종성이 ㄴ/ㄹ이고 이번 초성이 ㄹ → ㄹ을 l로 (ㄴㄹ·ㄹㄹ → ll)
        if isinstance(prev, tuple) and ini == 5 and prev[2] in (4, 5, 6, 8):
            ini_r = 'l'

        out.append(ini_r + med_r + fin_r)

    # 비음화 등 종성 보정(받침 ㄴ뒤 ㄹ→ll의 앞쪽): 이전 종성 ㄴ+다음 초성 ㄹ이면 이전 종성을 l로
    # → 위에서 다음 초성을 l로 바꿨으니, 앞 음절 종성도 l로 맞춤
    res = []
    for i, s in enumerate(syls):
        chunk = out[i]
        if isinstance(s, tuple) and i + 1 < len(syls) and isinstance(syls[i + 1], tuple):
            fin = s[2]; n_ini = syls[i + 1][0]
            if fin in (4, 5, 6) and n_ini == 5:      # 종성 ㄴ + 초성 ㄹ → 앞을 l
                chunk = chunk[:-1] + 'l' if chunk.endswith('n') else chunk
            elif fin in (1, 9, 24) and n_ini in (2, 6):   # ㄱ받침 + ㄴ/ㅁ → ng
                if chunk.endswith('k'):
                    chunk = chunk[:-1] + 'ng'
            elif fin in (17, 18, 26) and n_ini in (2, 6):  # ㅂ받침 + ㄴ/ㅁ → m
                if chunk.endswith('p'):
                    chunk = chunk[:-1] + 'm'
            elif fin in (7, 19, 20, 25) and n_ini in (2, 6):  # ㄷ받침 + ㄴ/ㅁ → n
                if chunk.endswith('t'):
                    chunk = chunk[:-1] + 'n'
        res.append(chunk)

    s = "".join(res)
    if cap and s:
        # 각 알파벳 덩어리(단어)의 첫 글자를 대문자로 — 복합어도 'Sangsu / Hasu'처럼
        out, prev_sep = [], True
        for ch in s:
            out.append(ch.upper() if (prev_sep and ch.isalpha()) else ch)
            prev_sep = not ch.isalpha()
        s = "".join(out)
    return s


if __name__ == "__main__":
    tests = {
        "발림": "Ballim", "살풀이": "Salpuri", "신라": "Silla",
        "한국어": "Hangugeo", "단청": "Dancheong", "정중동": "Jeongjungdong",
        "한삼": "Hansam", "추임새": "Chuimsae", "오방색": "Obangsaek",
        "여백": "Yeobaek", "준법": "Junbeop", "발묵": "Balmuk", "눈치": "Nunchi",
    }
    ok = 0
    for k, v in tests.items():
        r = romanize(k)
        mark = "✓" if r == v else "✗"
        if r == v:
            ok += 1
        print(f"  {mark} {k} → {r}  (정답 {v})")
    print(f"\n{ok}/{len(tests)} 일치")
