# -*- coding: utf-8 -*-
"""
stock_sites.py — 3단계 사이트 어댑터(Playwright). 크라우드픽·Unsplash·Pexels 업로드 자동화.

안전 원칙(중요): 스톡 업로드는 '외부 사이트에 공개 게시'하는 행위다. 이 모듈은 파일 선택과
메타데이터(제목·키워드·설명) '자동 입력'까지만 하고, **최종 '제출' 버튼은 사용자가 직접**
누르도록 브라우저를 열어 둔 채 대기한다(submit=True를 명시적으로 준 경우에만 자동 제출).

로그인은 영구 프로필(stock_profile)에 저장 — 처음 한 번 각 사이트에 로그인해 두면 유지된다.
각 사이트의 업로드 폼 구조는 자주 바뀌므로, 셀렉터는 SITES에 후보 목록으로 두고 실제 화면에
맞춰 조정한다(사용자와 함께 1회 맞추면 이후 재사용).
"""
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROFILE_DIR = SCRIPT_DIR / "stock_profile"

# 사이트별 설정 — 업로드 페이지 + 폼 셀렉터 후보(위에서부터 시도).
# file: <input type=file>, title/desc/keywords: 입력 필드, submit: 제출 버튼.
# login_marker: 이 URL 조각이 주소에 있으면 아직 로그인 안 된 것으로 간주.
SITES = {
    "크라우드픽": {
        "home_url": "https://www.crowdpic.net/",
        # 심사형 다단계 업로드 → 실용형(홈을 열어주고 사용자가 작가 업로드로 이동해 직접 업로드).
        # XMP 키워드는 파일에 이미 심어져 있어 크라우드픽이 자동으로 읽어들임.
        "upload_url": "https://www.crowdpic.net/",
        "login_marker": "login.php",
        "skip_file_ids": ["images"],   # 사이트 전역 '이미지로 검색' 입력칸 무시(업로드 아님)
        "assist": True,                # 자동 폼입력 안 함 — 페이지만 열어 줌
        "file": ['input[type="file"]'],
        "title": ['input[name="title"]', 'input[placeholder*="제목"]'],
        "keywords": ['input[name="tags"]', 'input[placeholder*="태그"]',
                     'input[placeholder*="키워드"]'],
        "desc": ['textarea[name="desc"]', 'textarea[placeholder*="설명"]'],
        "submit": ['button:has-text("등록")', 'button:has-text("업로드")',
                   'input[type="submit"]'],
        "kw_sep": "enter",     # 태그 입력 방식: 엔터로 하나씩
        "kw_field": "ko",      # 크라우드픽은 한국어 키워드
    },
    "Unsplash": {
        "home_url": "https://unsplash.com/",
        "upload_url": "https://unsplash.com/submit",   # 업로더 모달을 여는 진입점
        "login_marker": "login",
        "file": ['input[type="file"]'],
        "title": [],
        "keywords": ['input[placeholder*="tag" i]', 'input[aria-label*="tag" i]'],
        "desc": ['textarea', 'input[placeholder*="description" i]'],
        "submit": ['button:has-text("Submit")'],
        "kw_sep": "enter",
        "kw_field": "en",
    },
    "Pexels": {
        "home_url": "https://www.pexels.com/",
        "upload_url": "https://www.pexels.com/upload/",
        "login_marker": "login",
        "file": ['input[type="file"]'],
        "title": [],
        "keywords": ['input[placeholder*="tag" i]', 'input[aria-label*="keyword" i]'],
        "desc": ['textarea'],
        "submit": ['button:has-text("Submit")', 'button:has-text("Publish")'],
        "kw_sep": "comma",
        "kw_field": "en",
    },
}


def _first(page, selectors, timeout=4000):
    """후보 셀렉터 중 처음으로 보이는 요소 핸들 반환(없으면 None)."""
    for sel in selectors or []:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=timeout)
            return loc
        except Exception:
            continue
    return None


def _fill_keywords(loc, keywords, sep, log):
    """태그 입력 필드에 키워드 채우기 — enter/comma 방식."""
    try:
        loc.click()
        if sep == "comma":
            loc.fill(", ".join(keywords))
        else:  # enter: 하나씩 입력하고 Enter
            for k in keywords:
                loc.type(k)
                loc.press("Enter")
                time.sleep(0.15)
        return True
    except Exception as e:
        log(f"      ⚠️ 키워드 입력 실패: {e}")
        return False


def _launch_ctx(p):
    kwargs = dict(headless=False, slow_mo=120,
                  args=["--disable-blink-features=AutomationControlled",
                        "--no-first-run", "--no-default-browser-check",
                        "--start-maximized", "--window-size=1680,1000"],
                  ignore_default_args=["--enable-automation"],
                  no_viewport=True)   # 창 크기에 맞춰 표시(작게 잘리지 않게)
    try:
        return p.chromium.launch_persistent_context(str(PROFILE_DIR), channel="chrome", **kwargs)
    except Exception:
        return p.chromium.launch_persistent_context(str(PROFILE_DIR), **kwargs)


_DUMP_JS = r"""
() => {
  const vis = el => {
    const r = el.getBoundingClientRect();
    const s = window.getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const pick = el => ({
    tag: el.tagName.toLowerCase(),
    type: el.getAttribute('type') || '',
    name: el.getAttribute('name') || '',
    id: el.id || '',
    placeholder: el.getAttribute('placeholder') || '',
    aria: el.getAttribute('aria-label') || '',
    cls: (el.getAttribute('class') || '').slice(0, 80),
    text: (el.innerText || el.value || '').trim().slice(0, 40),
    visible: vis(el),
  });
  const out = [];
  document.querySelectorAll('input,textarea,select,button,[contenteditable="true"]')
    .forEach(el => out.push(pick(el)));
  return out;
}
"""


def _dump_all_frames(page):
    """상단 문서 + 모든 iframe의 입력칸을 수집. 각 항목에 frame url 태그."""
    out = []
    for fr in page.frames:
        try:
            ctrls = fr.evaluate(_DUMP_JS)
        except Exception:
            continue
        for c in ctrls:
            c["frame"] = fr.url
            out.append(c)
    return out


def _find_file_input(page, skip_ids=()):
    """상단+iframe에서 input[type=file] 로케이터를 찾음(없으면 None).
    skip_ids: 무시할 input id 목록(예: 크라우드픽의 '이미지로 검색' 입력 #images)."""
    for fr in page.frames:
        try:
            inputs = fr.locator('input[type="file"]')
            for i in range(inputs.count()):
                loc = inputs.nth(i)
                try:
                    iid = loc.get_attribute("id") or ""
                except Exception:
                    iid = ""
                if iid in skip_ids:
                    continue
                return loc
        except Exception:
            continue
    return None


def inspect_upload_form(site: str, sample_photo: str = None, wait_login: int = 360,
                        start_url: str = None, log=print) -> str:
    """업로드 폼의 모든 입력칸 구조를 파일로 덤프(셀렉터 조정용). 로그인·업로드 페이지 이동은
    사용자가 직접. 사용자가 어느 페이지로 가든 '파일 입력칸'이 보이면 그 화면(실제 업로드 URL
    포함)을 iframe까지 덤프. 파일 선택 전/후 모두 수집. 반환: 덤프 파일 경로."""
    import json
    from playwright.sync_api import sync_playwright
    cfg = SITES.get(site)
    if not cfg:
        raise ValueError(f"알 수 없는 사이트: {site}")
    out_dir = SCRIPT_DIR / "stock_form_dumps"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"{site}.json"
    result = {"site": site, "upload_url": cfg["upload_url"], "before_file": [],
              "after_file": [], "final_url": "", "frames": [], "body_text": ""}
    start = start_url or cfg.get("home_url") or cfg["upload_url"]

    with sync_playwright() as pw:
        ctx = _launch_ctx(pw)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            try:
                page.goto(start, wait_until="domcontentloaded", timeout=60000)
            except Exception:
                pass
            log(f"   🔑 {site} 브라우저에서 로그인한 뒤 '업로드(작가) 페이지'로 직접 이동해 주세요.")
            log(f"      (파일 올리는 칸이 보이는 화면이면 자동으로 감지·수집합니다 — 최대 {wait_login}s 대기)")
            skip_ids = tuple(cfg.get("skip_file_ids") or ())
            waited, finp = 0, None
            while waited < wait_login:
                finp = _find_file_input(page, skip_ids)
                if finp is not None:
                    break
                time.sleep(3); waited += 3
            result["final_url"] = page.url
            result["frames"] = [fr.url for fr in page.frames]
            try:
                result["body_text"] = page.locator("body").inner_text(timeout=4000)[:600]
            except Exception:
                pass
            result["before_file"] = _dump_all_frames(page)
            log(f"   📋 파일 선택 전 입력칸 {len(result['before_file'])}개(iframe 포함)")

            if finp is None:
                log("      ⚠️ 파일 입력칸을 못 찾음 — 업로드 페이지에 도달했는지 확인 필요"
                    "(로그인 후 그 페이지에 머물러 주세요). 현재 화면 그대로 저장합니다.")
            elif sample_photo and Path(sample_photo).exists():
                try:
                    finp.set_input_files(sample_photo, timeout=15000)
                    log("   📎 샘플 파일 올림 → 폼 변화 6초 대기")
                    time.sleep(6)
                    result["after_file"] = _dump_all_frames(page)
                    log(f"   📋 파일 선택 후 입력칸 {len(result['after_file'])}개(iframe 포함)")
                except Exception as e:
                    log(f"      ⚠️ 샘플 파일 자동 업로드 실패 — 직접 한 장 올려주세요(15초 대기): {e}")
                    time.sleep(15)
                    result["after_file"] = _dump_all_frames(page)
            out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                encoding="utf-8")
            log(f"   ✅ 덤프 저장: {out_path}  (입력칸 {len(result['after_file'] or result['before_file'])}개)")
            return str(out_path)
        finally:
            try: ctx.close()
            except Exception: pass


def fill_upload(site: str, entry: dict, submit: bool = False,
                wait_login: int = 300, keep_open: int = 180, log=print) -> dict:
    """한 사이트에 사진 1장 업로드 폼을 자동 입력. submit=False면 제출 직전에 멈추고
    사용자가 검토·제출하도록 브라우저를 열어 둔다.
    반환: {ok, filled:{file,title,keywords,desc}, submitted, note}."""
    from playwright.sync_api import sync_playwright
    cfg = SITES.get(site)
    if not cfg:
        return {"ok": False, "note": f"알 수 없는 사이트: {site}"}
    photo = entry.get("photo_path", "")
    if not Path(photo).exists():
        return {"ok": False, "note": f"사진 파일 없음: {photo}"}

    title = entry.get("title_en", "")
    desc = entry.get("description_en", "")
    kws = entry.get("keywords_ko" if cfg["kw_field"] == "ko" else "keywords_en", []) \
        or entry.get("keywords_en", [])
    filled = {"file": False, "title": False, "keywords": False, "desc": False}

    with sync_playwright() as pw:
        ctx = _launch_ctx(pw)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        skip_ids = tuple(cfg.get("skip_file_ids") or ())
        try:
            page.goto(cfg.get("upload_url") or cfg.get("home_url"),
                      wait_until="domcontentloaded", timeout=60000)
            log(f"   🔑 {site}: 로그인/업로드 페이지가 아니면 이동해 주세요(파일칸 감지까지 최대 {wait_login}s 대기)")

            # 파일 입력칸이 나타날 때까지 대기(로그인+업로드 페이지 도달). 검색용 입력칸은 제외.
            waited, floc = 0, None
            while waited < wait_login:
                floc = _find_file_input(page, skip_ids)
                if floc is not None:
                    break
                time.sleep(3); waited += 3
            if floc is None:
                log("      ⚠️ 업로드 파일칸을 자동으로 못 찾음 — 창을 열어 둘 테니 직접 올려주세요"
                    "(파일에 XMP 키워드가 이미 들어 있습니다).")
            else:
                # 파일 선택 — XMP 태그가 심어져 있어 Unsplash·Pexels는 태그를 자동으로 읽어들임
                try:
                    floc.set_input_files(photo, timeout=15000)
                    filled["file"] = True
                    log(f"   📎 파일 선택: {Path(photo).name} (XMP 키워드는 사이트가 자동으로 읽습니다)")
                    time.sleep(4)
                except Exception as e:
                    log(f"      ⚠️ 파일 선택 실패: {e}")

            if title and cfg.get("title"):
                t = _first(page, cfg["title"])
                if t:
                    try: t.fill(title); filled["title"] = True
                    except Exception: pass
            if kws and cfg.get("keywords"):
                kloc = _first(page, cfg["keywords"])
                if kloc:
                    filled["keywords"] = _fill_keywords(kloc, kws, cfg.get("kw_sep"), log)
            if desc and cfg.get("desc"):
                dloc = _first(page, cfg["desc"])
                if dloc:
                    try: dloc.fill(desc); filled["desc"] = True
                    except Exception: pass

            log(f"   ✍️ {site} 자동 입력: " +
                ", ".join(k for k, v in filled.items() if v) or "(입력 필드 못 찾음)")

            submitted = False
            if submit:
                sloc = _first(page, cfg["submit"])
                if sloc:
                    sloc.click()
                    submitted = True
                    log(f"   🚀 {site} 제출 클릭")
                    time.sleep(4)
            else:
                log(f"   ⏸ {site}: 검토 후 직접 '제출'하세요. {keep_open}s 후 창을 닫습니다.")
                waited = 0
                while waited < keep_open:
                    time.sleep(5); waited += 5
            return {"ok": True, "filled": filled, "submitted": submitted,
                    "note": "자동 입력 완료" + (" · 제출됨" if submitted else " · 사용자 제출 대기")}
        finally:
            try: ctx.close()
            except Exception: pass
