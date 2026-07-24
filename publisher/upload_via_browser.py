"""
upload_via_browser.py
Playwright로 Blogger 웹 에디터에 이미지를 업로드해
구글 CDN(lh3.googleusercontent.com) URL을 가져옵니다.

최초 실행 전 한 번만 설치:
  pip install playwright pillow --break-system-packages
  playwright install chromium
"""
import io, re, time, base64, tempfile, urllib.request
from pathlib import Path
from playwright.sync_api import sync_playwright


# ── File System Access API 오버라이드 ─────────────────────────────────────────
# 새 Blogger 에디터의 '컴퓨터에서 업로드'는 window.showOpenFilePicker(OS 네이티브
# 파일 선택창)를 사용해 Playwright의 filechooser 이벤트로 가로챌 수 없습니다.
# → 페이지 로드 전에 showOpenFilePicker를 '우리가 주입한 파일을 반환하는 함수'로
#   바꿔치기하면 Blogger가 그 파일을 정상 업로드합니다. (2026-06-13 검증 완료)
FILE_PICKER_OVERRIDE = r"""
window.__pwOpenFile = async () => {
  const f = window.__nextUploadFile;
  if (!f) throw new DOMException('No file selected', 'AbortError');
  const bin = atob(f.b64); const arr = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
  const file = new File([arr], f.name, {type: f.type || 'image/jpeg'});
  const handle = { kind: 'file', name: f.name,
    getFile: async () => file,
    queryPermission: async () => 'granted',
    requestPermission: async () => 'granted',
    isSameEntry: async () => false };
  return [handle];
};
try {
  Object.defineProperty(window, 'showOpenFilePicker',
    {configurable: true, writable: true, value: window.__pwOpenFile});
} catch (e) {}
"""


# ── SEO 이미지 압축 ────────────────────────────────────────────────────────────
# Google PageSpeed 권장 기준:
#   - 최대 너비 1200px (대형 디스플레이 대응, 과도한 용량 방지)
#   - JPEG 품질 82 (시각적 손실 거의 없음, 파일 크기 ~60% 감소)
#   - EXIF 제거 (불필요한 메타데이터 제거, 용량 절감)
#   - 방향(Orientation) EXIF만 적용 후 제거

SEO_MAX_WIDTH = 1200
SEO_QUALITY   = 82


def compress_for_seo(path: Path) -> Path:
    """
    원본 이미지를 SEO 최적화 JPEG로 압축해 임시 파일 경로를 반환합니다.
    호출자가 사용 후 .unlink()로 삭제해야 합니다.
    """
    try:
        from PIL import Image, ImageOps
        img = Image.open(path)
        img = ImageOps.exif_transpose(img)   # EXIF 방향 적용
        img = img.convert("RGB")             # EXIF 완전 제거 (convert로 메타 드롭)

        if img.width > SEO_MAX_WIDTH:
            ratio = SEO_MAX_WIDTH / img.width
            img = img.resize(
                (SEO_MAX_WIDTH, int(img.height * ratio)),
                Image.LANCZOS,
            )

        # 임시 파일로 저장 (확장자 .jpg 고정)
        tmp = tempfile.NamedTemporaryFile(
            suffix=".jpg", delete=False,
            dir=path.parent,
        )
        tmp.close()
        tmp_path = Path(tmp.name)
        img.save(tmp_path, format="JPEG", quality=SEO_QUALITY, optimize=True)

        orig_kb = path.stat().st_size // 1024
        comp_kb = tmp_path.stat().st_size // 1024
        print(f"        📐 압축: {orig_kb}KB → {comp_kb}KB "
              f"({int((1 - comp_kb/max(orig_kb,1))*100)}% 감소)", flush=True)
        return tmp_path

    except Exception as e:
        print(f"        ⚠️  압축 실패 ({path.name}), 원본 사용: {e}", flush=True)
        return path  # 압축 실패 시 원본 그대로 사용

SCRIPT_DIR   = Path(__file__).resolve().parent
PROFILE_DIR  = SCRIPT_DIR / "browser_profile"
BLOG_ID_FILE = SCRIPT_DIR / "blog_id.txt"

# 구형 에디터의 filechooser 이벤트 핸들러가 참조하는 '현재 업로드 파일'
_CURRENT_UPLOAD = {"path": None}


def get_blog_id() -> str:
    if BLOG_ID_FILE.exists():
        return BLOG_ID_FILE.read_text().strip().split("\n")[0]
    raise RuntimeError("blog_id.txt 없음 — publish_today.py를 먼저 실행하세요.")


# 업로드된 이미지가 게재되는 구글 CDN 도메인들 (계정 아바타·위젯 이미지는 제외)
_CDN_PATTERNS = [
    r'https://lh3\.googleusercontent\.com/[^\s"\'<>&)]+',
    r'https://blogger\.googleusercontent\.com/[^\s"\'<>&)]+',
    r'https://[\w.-]*bp\.blogspot\.com/[^\s"\'<>&)]+',
]


def _is_avatar_url(u: str) -> bool:
    """프로필 사진(/a/, /a-/)·계정 위젯(/ogw/) URL — 본문 이미지가 아님."""
    return "/a/" in u or "/a-/" in u or "/ogw/" in u


def _cdn_urls_in_page(page) -> set:
    """에디터 전체 프레임에서 업로드 이미지 CDN URL 집합 추출 (아바타 제외)."""
    urls = set()
    frames = list(page.frames)
    for frame in frames:
        try:
            html = frame.content()
        except Exception:
            continue
        for pat in _CDN_PATTERNS:
            urls |= set(re.findall(pat, html))
    return {u for u in urls if not _is_avatar_url(u)}


_TARGET_SIZE = "s1600"   # 본문 게재용 통일 사이즈(긴 변 기준 최대 1600px)


def _normalize_size(url: str) -> str:
    """CDN URL의 사이즈 토큰(/s320/, /s1200/, /w800-h600/ 등)을 /s1600/ 으로 통일.
    일부 사진이 작은 사이즈로 들어가 갤러리에서 작게 보이는 문제를 방지합니다."""
    if not url:
        return url
    # /s320/  /s1200-c/  형태
    new = re.sub(r"/s\d+(-[a-z]+)?/", f"/{_TARGET_SIZE}/", url)
    if new != url:
        return new
    # /w800-h600/  형태
    new = re.sub(r"/w\d+-h\d+(-[a-z]+)?/", f"/{_TARGET_SIZE}/", url)
    return new


def _pick_largest(urls) -> str:
    """여러 사이즈(/s320/, /s1200/ 등) 중 가장 큰 이미지 URL을 고른 뒤,
    사이즈를 /s1600/ 으로 통일해 모든 사진이 같은 해상도 기준으로 게재되게 합니다."""
    if not urls:
        return None

    def _size(u):
        m = re.search(r"/s(\d+)(?:-[a-z]+)?/", u)
        return int(m.group(1)) if m else 0
    best = max(urls, key=lambda u: (_size(u), len(u)))
    return _normalize_size(best)


def _verify_uploaded_url(url: str, expected_bytes: int, timeout: int = 15) -> bool:
    """후보 URL이 '방금 우리가 올린 그 사진'이 맞는지 실제로 내려받아 확인.
    ★2026-07-08 버그: before/after URL 차집합만 믿고 반환했더니, 페이지에 이미
    떠 있던(우리가 올린 적 없는) 블로그 기본 로고 이미지(PNG, 58KB)가 '새 URL'로
    오탐되어 실사진(JPEG, 수백KB) 대신 통째로 올라간 사고가 있었음(10편 전부 오염).
    Content-Type이 image/jpeg인지(우리는 항상 JPEG로 업로드) + 실제 바이트 크기가
    업로드한 파일과 같은 자릿수인지(50% 이상)를 둘 다 확인해야 '진짜'로 인정."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            data = resp.read(2_000_000)   # 확인용이라 최대 2MB만 읽고 끊음
            clen = resp.headers.get("Content-Length")
            size = int(clen) if clen else len(data)
        if "jpeg" not in ctype and "jpg" not in ctype:
            return False
        if expected_bytes and size < expected_bytes * 0.5:
            return False
        return True
    except Exception:
        return False


def _uploaded_img_urls(page) -> set:
    """실제 '업로드된 이미지' URL 집합.
    ① 모든 프레임의 HTML 소스 정규식 + ② 모든 프레임의 DOM(document.images)을 합쳐,
    구글 CDN(googleusercontent/bp.blogspot) + 업로드 경로(/img/)만 남깁니다.
    (프로필·위젯·테마·로고 이미지는 _is_avatar_url 및 도메인/경로 필터로 제외)"""
    urls = set(_cdn_urls_in_page(page))   # ① HTML 소스 정규식
    for fr in list(page.frames):          # ② 실제 렌더된 img src
        try:
            srcs = fr.evaluate("() => Array.from(document.images).map(i => i.src)")
        except Exception:
            continue
        for s in srcs or []:
            if ("googleusercontent.com" in s or "bp.blogspot.com" in s) and not _is_avatar_url(s):
                urls.add(s)
    return {u for u in urls if "/img/" in u}


def _first_visible(page, sel, limit: int = 8):
    """셀렉터 매치 중 '실제로 보이는' 첫 요소를 반환(없으면 None).
    ★2026-07-08: Blogger 에디터 개편(AI 수정 도구 추가) 후 도구막대가 DOM에 두 벌
    렌더링되는데(한 벌은 숨김), .first만 확인하면 숨겨진 쪽에 걸려 '버튼 없음'으로
    오판된다 — 매치를 순회하며 보이는 것을 찾아야 함."""
    try:
        els = page.locator(sel)
        n = min(els.count(), limit)
        for i in range(n):
            el = els.nth(i)
            try:
                if el.is_visible():
                    return el
            except Exception:
                continue
    except Exception:
        pass
    return None


def _click_image_button(page) -> bool:
    """에디터 툴바의 이미지 삽입 버튼 클릭. 성공하면 True."""
    selectors = [
        'button[aria-label="Insert image"]',
        'button[aria-label="이미지 삽입"]',
        'button[data-tooltip="Insert image"]',
        'button[title*="image" i]',
        '[aria-label*="이미지" i]',
        '[aria-label*="image" i]',
        # Blogger 새 에디터 범용
        'button.goog-toolbar-button[title*="image" i]',
    ]
    for sel in selectors:
        el = _first_visible(page, sel)
        if el is not None:
            try:
                el.click()
                return True
            except Exception:
                continue
    return False


def _confirm_layout_dialog(page, timeout: int = 30) -> bool:
    """파일 선택 후 Blogger가 띄우는 '레이아웃 선택'(크기/정렬) 확인 대화상자에서
    '확인'을 클릭한다(2026-07 개편으로 추가된 단계 — 이 확인을 안 누르면 업로드된
    사진이 대화상자 안에 멈춰 있을 뿐 실제 본문에는 절대 삽입되지 않는다).
    업로드 처리 자체에 시간이 걸려 대화상자가 늦게(수 초~10여 초) 뜰 수 있으므로
    최대 timeout초까지 기다린다. 대화상자가 끝내 안 뜨면(구형 에디터 등) 조용히 반환."""
    keys = ("확인", "ok", "done", "insert", "select")
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.5)
        try:
            cands = page.locator('button, [role="button"]').all()
        except Exception:
            continue
        for el in cands:
            try:
                if not el.is_visible(timeout=60):
                    continue
                t = (el.inner_text(timeout=80) or "").strip().lower()
                if t in keys:
                    el.click(timeout=2000)
                    time.sleep(0.5)
                    return True
            except Exception:
                continue
    return False


def _upload_one(page, photo: Path) -> str:
    """이미지 한 장을 SEO 압축 후 에디터에 업로드하고 CDN URL 반환"""
    # ── SEO 압축 ──────────────────────────────────────────────────────────────
    tmp_path = compress_for_seo(photo)
    is_tmp   = (tmp_path != photo)   # 압축 임시 파일 여부
    _CURRENT_UPLOAD["path"] = str(tmp_path)   # 구형 에디터 filechooser 핸들러용

    try:
        # 업로드 전 이미 본문에 있는 이미지 URL(다른 사진) — 차집합으로 새 것만 식별
        before = _uploaded_img_urls(page)

        # 0) 업로드할 파일을 페이지에 주입 — showOpenFilePicker 오버라이드가
        #    이 파일을 Blogger에 반환합니다 (FILE_PICKER_OVERRIDE 참고)
        #    ★파일명은 반드시 ASCII + .jpg 확장자 (한글명이 빈 이름이 되면 업로드 거부됨)
        safe = re.sub(r"[^A-Za-z0-9_-]+", "", photo.stem)[:40].strip("_-")
        fname = f"{safe or 'photo'}_{int(time.time() * 1000) % 100000}.jpg"
        b64 = base64.b64encode(tmp_path.read_bytes()).decode("ascii")
        page.evaluate("(d) => { window.__nextUploadFile = d; }",
                      {"name": fname, "b64": b64, "type": "image/jpeg"})

        # 1) 이미지 버튼 클릭 (앞 업로드 잔여 메뉴를 닫고, 실패 시 본문 포커스 후 재시도)
        try:
            page.keyboard.press("Escape")
            time.sleep(0.4)
        except Exception:
            pass
        if not _click_image_button(page):
            try:
                page.mouse.click(450, 430)   # 본문 영역 클릭 → 도구막대 활성화
                time.sleep(0.8)
            except Exception:
                pass
            if not _click_image_button(page):
                raise RuntimeError("이미지 삽입 버튼을 찾을 수 없음")
        time.sleep(2.0)   # 메뉴(컴퓨터에서 업로드 등) 렌더 대기

        # 2) "컴퓨터에서 업로드" 메뉴 항목 클릭
        #    (항목은 <span role="menuitem">, 텍스트는 'cloud_upload\n컴퓨터에서 업로드' 형태)
        #    신형 에디터: 클릭 → showOpenFilePicker 오버라이드가 파일 반환 → 즉시 업로드
        #    구형 에디터: 클릭 → filechooser 이벤트 → upload_images_to_blogger의
        #                page.on("filechooser") 핸들러가 파일 지정
        item = None
        keys = ("컴퓨터에서 업로드", "upload from computer")
        try:
            els = page.locator('[role="menuitem"], [role="option"], li').all()
        except Exception:
            els = []
        for el in els[:80]:
            try:
                if not el.is_visible(timeout=80):
                    continue
                t = (el.inner_text(timeout=100) or "").lower()
                if any(k in t for k in keys):
                    item = el
                    break
            except Exception:
                continue

        if item is not None:
            # 신형 에디터: 메뉴 클릭 → "이미지 추가" 다이얼로그가 뜨는데, 최근(2026-07) 개편으로
            # 이 다이얼로그의 '컴퓨터에서 업로드' 탭이 Google Picker(docs.google.com/picker/...)
            # 라는 별도 iframe으로 렌더링된다. 그 iframe 안에 진짜 <input type="file">이 있으므로
            # (Playwright로 직접 채울 수 있음) — 메인 페이지에서 '찾아보기' 버튼을 찾던 예전 방식은
            # 이 iframe 밖을 뒤지는 셈이라 항상 실패했다(2026-07-08 버그).
            item.click(timeout=4000)

            # Picker iframe은 지도·위젯 등 다른 리소스 로딩과 겹치면 뜨는 데 20초 넘게
            # 걸리기도 해서(실측 최대 25초 가까이) 넉넉히 최대 45초까지 기다린다.
            # 프레임이 잡혀도 그 안의 <input type="file">이 아직 안 붙어 있을 수 있어
            # 입력창이 실제로 나타날 때까지 별도로 재확인한다.
            picker_frame = None
            inp = None
            deadline = time.time() + 45
            while time.time() < deadline:
                time.sleep(0.4)
                for fr in list(page.frames):
                    if "picker" in (fr.url or "").lower():
                        picker_frame = fr
                        break
                if picker_frame is not None:
                    try:
                        cand = picker_frame.locator('input[type="file"]')
                        if cand.count() > 0:
                            inp = cand
                            break
                    except Exception:
                        pass

            filled = False
            if inp is not None:
                try:
                    inp.first.set_input_files(str(tmp_path), timeout=4000)
                    filled = True
                except Exception:
                    pass

            if filled:
                # 업로드가 끝나면(Picker iframe이 스스로 닫힘) Blogger가 "레이아웃 선택"
                # (크기/정렬) 확인 대화상자를 새로 띄운다(2026-07 개편으로 추가된 단계) —
                # 여기서 '확인'을 눌러야 실제로 본문에 삽입된다. 안 누르면 사진이 영영
                # 대화상자 안에 멈춰 있어 '새 이미지 URL'이 본문에 나타나지 않는다.
                _confirm_layout_dialog(page)

            if not filled:
                # 폴백 ①: Picker가 아닌 다른 변형 — 메인 페이지의 '찾아보기' 버튼 클릭
                browse = None
                try:
                    cands = page.locator('button, [role="button"]').all()
                except Exception:
                    cands = []
                for el in cands[:120]:
                    try:
                        if not el.is_visible(timeout=60):
                            continue
                        t = (el.inner_text(timeout=80) or "").strip().lower()
                        if t in ("찾아보기", "browse", "select from computer", "사진 선택") \
                                or "찾아보기" in t or "browse" in t:
                            browse = el
                            break
                    except Exception:
                        continue
                if browse is not None:
                    browse.click(timeout=4000)
                time.sleep(1.5)
                # 폴백 ②: showOpenFilePicker가 아니라 input[type=file]을 쓰는 경우 직접 채움
                # (모든 프레임 재확인 — Picker iframe이 이 시점에야 뒤늦게 뜨는 경우 포함)
                try:
                    if not page.evaluate("() => window.__nextUploadFile === undefined"):
                        for fr in list(page.frames):
                            try:
                                inp = fr.locator('input[type="file"]')
                                if inp.count() > 0:
                                    inp.first.set_input_files(str(tmp_path), timeout=4000)
                                    _confirm_layout_dialog(page)
                                    break
                            except Exception:
                                continue
                except Exception:
                    pass
        else:
            # 폴백: 메뉴 없이 input[type=file]이 바로 있는 구형 에디터
            try:
                page.locator('input[type="file"]').first.set_input_files(
                    str(tmp_path), timeout=3000)
                _confirm_layout_dialog(page)
            except Exception:
                raise RuntimeError("업로드 메뉴 항목을 찾을 수 없음 — 셀렉터 확인 필요")

        # 3) 새 업로드 이미지 URL이 본문에 나타날 때까지 대기(최대 60초).
        #    ★차집합에 새 URL이 잡혀도 그게 '진짜 우리 사진'인지 다운로드해서 검증
        #    (_verify_uploaded_url) — 페이지에 이미 있던 로고 등 무관한 이미지가
        #    '새 URL'로 오탐되는 사고를 막는다(2026-07-08). 검증 통과 못 하면 계속 대기.
        expected_bytes = tmp_path.stat().st_size
        checked_bad = set()
        last = set()
        for _ in range(60):
            time.sleep(1)
            last = _uploaded_img_urls(page)
            new_urls = (last - before) - checked_bad
            for cand in sorted(new_urls, key=len, reverse=True):
                if _verify_uploaded_url(cand, expected_bytes):
                    return _pick_largest([cand])
                checked_bad.add(cand)
        # 차집합에서 끝내 검증 통과한 게 없으면, 전체 후보(last) 중에서라도
        # 검증되는 게 있는지 마지막으로 한 번 확인(그래도 없으면 실패로 처리 —
        # 예전처럼 '아무거나' 반환하지 않음).
        for cand in sorted(last - checked_bad, key=len, reverse=True):
            if _verify_uploaded_url(cand, expected_bytes):
                return _pick_largest([cand])

        raise RuntimeError(
            "업로드한 사진과 일치하는 이미지를 60초 내에 찾지 못함 — "
            "업로드 실패 또는 페이지의 다른 이미지가 오탐됨(셀렉터 확인 필요)")

    finally:
        # 임시 압축 파일 삭제 (원본은 보존)
        if is_tmp:
            try:
                tmp_path.unlink()
            except Exception:
                pass


def _editor_ready(page) -> bool:
    """이미지 삽입 버튼(에디터 도구막대)이 보이면 True — 에디터가 쓸 수 있는 상태.
    (.first가 아니라 보이는 매치를 찾음 — _first_visible 주석 참고)"""
    sels = [
        'button[aria-label="Insert image"]', 'button[aria-label="이미지 삽입"]',
        'button[title*="image" i]', '[aria-label*="이미지" i]', '[aria-label*="image" i]',
    ]
    for sel in sels:
        if _first_visible(page, sel) is not None:
            return True
    return False


def _needs_login(page) -> bool:
    try:
        if "accounts.google.com" in page.url or "servicelogin" in page.url.lower():
            return True
        for sel in ('input[type="email"]', 'input[type="password"]'):
            if page.locator(sel).first.is_visible(timeout=400):
                return True
    except Exception:
        pass
    return False


def _wait_editor_ready(page, blog_id: str, total_wait: int = 300) -> bool:
    """에디터 도구막대가 나타날 때까지 대기. 로그인이 필요하면 사용자 로그인을 기다림.
    ★로그인(구글) 화면에서는 절대 새로고침/페이지 이동을 하지 않습니다 — 입력이 초기화되지 않게."""
    waited = 0
    announced = False
    while waited < total_wait:
        if _editor_ready(page):
            return True
        if _needs_login(page) or "accounts.google" in (page.url or ""):
            if not announced:
                print("   🔑 이 블로그 계정으로 '브라우저' 로그인이 필요합니다.", flush=True)
                print("      열린 브라우저 창에서 천천히 로그인하세요 — 페이지를 새로고침하지 않고 기다립니다.", flush=True)
                announced = True
            # 로그인 화면에서는 아무 것도 건드리지 않고 조용히 대기
            time.sleep(3); waited += 3
            continue
        # 로그인 화면도 아니고 에디터도 아직 — 로그인 직후이거나 로딩 중
        if "edit/new" not in (page.url or ""):
            # 로그인 직후 다른 페이지(블로거 홈 등)면 에디터로 '한 번만' 이동
            try:
                page.goto(f"https://www.blogger.com/blog/post/edit/new/{blog_id}",
                          wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass
            time.sleep(2); waited += 2
            continue
        # 이미 에디터 URL — 도구막대 로딩만 기다림(새로고침하지 않음)
        time.sleep(2); waited += 2
    return _editor_ready(page)


def _open_new_post_editor(page, blog_id: str):
    """Blogger 새 글 에디터를 안전하게 엽니다.
    홈 → 대시보드 → 에디터 순으로 진입해 403·봇 감지를 우회합니다."""

    # 1) Blogger 홈 (세션 웜업)
    print("   🌐 Blogger 접속 중...", flush=True)
    page.goto("https://www.blogger.com/", wait_until="domcontentloaded", timeout=40000)
    time.sleep(1.5)

    # 2) 로그인 필요 시 대기
    if "accounts.google.com" in page.url or "servicelogin" in page.url.lower():
        print("   🔑 구글 계정 로그인이 필요합니다 — 브라우저에서 로그인하면 이어집니다...", flush=True)
        page.wait_for_url("**/blogger.com/**", timeout=180000)
        time.sleep(2)

    # 3) 블로그 대시보드
    page.goto(f"https://www.blogger.com/blog/posts/{blog_id}",
              wait_until="domcontentloaded", timeout=30000)
    time.sleep(1.5)

    # 4) 오류 페이지 감지 시 reload
    title = page.title()
    if "403" in title or "오류" in title or "error" in title.lower():
        print("   ⚠️  페이지 오류 감지 — 재시도...", flush=True)
        time.sleep(3)
        page.reload(wait_until="domcontentloaded", timeout=30000)
        time.sleep(2)

    # 5) 새 글 에디터
    page.goto(f"https://www.blogger.com/blog/post/edit/new/{blog_id}",
              wait_until="domcontentloaded", timeout=30000)
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    time.sleep(2)

    # 6) 도구막대(이미지 버튼)가 실제로 준비될 때까지 대기(필요시 로그인 대기)
    if _wait_editor_ready(page, blog_id):
        print("   ✅ 에디터 준비 완료", flush=True)
    else:
        print("   ⚠️  에디터의 이미지 도구막대를 찾지 못했습니다. "
              "브라우저 로그인 상태/네트워크를 확인하세요(이미지 업로드 건너뜀).", flush=True)


def _launch_ctx(p, profile_dir):
    """자동화 배너 숨김 + 시스템 Chrome 우선으로 영구 컨텍스트를 엽니다.
    프로필이 다른 창에서 사용 중이면 친절한 오류를 던집니다."""
    launch_kwargs = dict(
        headless=False,
        slow_mo=120,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
        ],
        # '자동화된 테스트 소프트웨어로 제어되고 있습니다' 배너 제거
        # (구글 로그인 차단 가능성도 함께 낮춤)
        ignore_default_args=["--enable-automation"],
        viewport={"width": 1280, "height": 900},
    )
    try:
        try:
            ctx = p.chromium.launch_persistent_context(
                str(profile_dir), channel="chrome", **launch_kwargs)
            print("   🌐 시스템 Chrome 사용", flush=True)
        except Exception as e1:
            if "Target page, context or browser has been closed" in str(e1):
                raise
            ctx = p.chromium.launch_persistent_context(
                str(profile_dir), **launch_kwargs)
            print("   🌐 Chromium 사용", flush=True)
        return ctx
    except Exception as e:
        if "Target page, context or browser has been closed" in str(e) \
                or "ProcessSingleton" in str(e):
            raise RuntimeError(
                "이 로그인 세션을 쓰는 다른 자동화 브라우저 창이 이미 열려 있습니다. "
                "그 창을 닫고 다시 시도하세요.")
        raise


def _detect_account_email(page) -> str:
    """현재 세션의 기본 구글 계정 이메일을 감지(못 찾으면 빈 문자열)."""
    try:
        page.goto("https://myaccount.google.com/",
                  wait_until="domcontentloaded", timeout=40000)
        time.sleep(3)
        if "accounts.google.com" in page.url:   # 로그인 안 됨
            return ""
        body = page.locator("body").inner_text(timeout=5000)
        m = re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", body)
        # gmail 우선, 없으면 첫 매치
        for e in m:
            if e.lower().endswith("gmail.com"):
                return e
        return m[0] if m else ""
    except Exception:
        return ""


def login_and_verify(profile_dir, blog_id: str, wait_login: int = 300) -> dict:
    """브라우저를 열어 ①로그인 상태 확인(필요시 사용자 로그인 대기, 새로고침 안 함)
    ②로그인 계정 이메일 감지 ③해당 블로그 에디터 접근(이미지 도구막대) 테스트.
    반환: {"email":..., "editor_ok":bool, "status":int|str, "logged_in":bool}"""
    res = {"email": "", "editor_ok": False, "status": "", "logged_in": False}
    with sync_playwright() as p:
        ctx = _launch_ctx(p, profile_dir)
        try:
            page = ctx.new_page()
            page.goto("https://www.blogger.com/",
                      wait_until="domcontentloaded", timeout=40000)
            time.sleep(2)
            waited = 0
            announced = False
            while _needs_login(page) and waited < wait_login:
                if not announced:
                    print("   🔑 이 창에서 이 블로그를 편집할 수 있는 계정으로 로그인하세요.", flush=True)
                    print("      (새로고침하지 않고 기다립니다 — 천천히 진행하세요)", flush=True)
                    announced = True
                time.sleep(3); waited += 3
            if _needs_login(page):
                print("   ⚠️  로그인이 완료되지 않았습니다.", flush=True)
                return res
            res["logged_in"] = True

            res["email"] = _detect_account_email(page)
            print(f"   👤 로그인 계정: {res['email'] or '(감지 실패)'}", flush=True)

            # 이 세션의 블로거 홈에 해당 블로그가 '편집 가능 목록'으로 잡히는지
            try:
                page.goto("https://www.blogger.com/", wait_until="domcontentloaded",
                          timeout=40000)
                time.sleep(3)
                html = page.content()
                res["in_blog_list"] = (blog_id in html)
                print(f"   📋 이 세션의 블로그 목록에 포함: "
                      f"{'✅ 예' if res['in_blog_list'] else '❌ 아니오'}", flush=True)
            except Exception:
                res["in_blog_list"] = None

            # 실제 업로드와 같은 경로(임시 초안 편집)로 에디터 접근을 테스트
            draft_id, service = _create_temp_draft(blog_id)
            if draft_id:
                url = f"https://www.blogger.com/blog/post/edit/{blog_id}/{draft_id}"
            else:
                url = f"https://www.blogger.com/blog/post/edit/new/{blog_id}"
            r = page.goto(url, wait_until="domcontentloaded", timeout=40000)
            res["status"] = r.status if r else "?"
            time.sleep(3)
            res["editor_ok"] = _wait_editor_ready(page, blog_id, total_wait=40)
            # 임시 초안은 재사용을 위해 남겨 둔다(삭제하면 다음에 새로 만들어야 해서
            # Blogger '하루 50개 글 생성' 한도를 소모 — 2026-07-14).
            print(f"   📝 에디터 접근: {'✅ 가능' if res['editor_ok'] else '❌ 불가'} "
                  f"(HTTP {res['status']})", flush=True)
            return res
        finally:
            try:
                ctx.close()
            except Exception:
                pass


# 사진 업로드용 임시 초안의 고정 제목(고유 마커) — 청소 시 이 제목만 지운다.
_TEMP_DRAFT_TITLE = "(임시) 이미지 업로드"


def _find_temp_drafts(service, blog_id: str) -> list:
    """블로그에 남아 있는 '(임시) 이미지 업로드' 초안 id 목록(제목이 정확히 이 마커인 것만).
    읽기 전용이라 쓰기 할당량을 쓰지 않는다."""
    ids = []
    try:
        req = service.posts().list(
            blogId=blog_id, status="DRAFT", maxResults=500, view="ADMIN",
            fields="items(id,title),nextPageToken")
        while req is not None:
            resp = req.execute()
            for p in resp.get("items", []):
                if (p.get("title") or "").strip() == _TEMP_DRAFT_TITLE:
                    ids.append(p["id"])
            token = resp.get("nextPageToken")
            req = service.posts().list(
                blogId=blog_id, status="DRAFT", maxResults=500, view="ADMIN",
                pageToken=token, fields="items(id,title),nextPageToken") if token else None
    except Exception:
        pass
    return ids


def _cleanup_stale_temp_drafts(service, blog_id: str, keep: int = 1) -> int:
    """임시 초안이 여러 개 쌓였으면 keep개만 남기고 삭제(잔류 방지).
    ★ 예전엔 전부 지우고 매번 새로 만들었는데, Blogger는 '하루 50개 글 생성'이라는
    스팸방지 한도가 있고 **초안 생성도 이 한도를 소모**한다. 업로드마다 초안을 만들면
    사진 올릴 때마다 하루 한도를 깎아먹어 429가 났다(2026-07-14) → 이제 1개를 남겨
    계속 재사용한다."""
    ids = _find_temp_drafts(service, blog_id)
    removed = 0
    for pid in ids[keep:]:
        try:
            service.posts().delete(blogId=blog_id, postId=pid).execute()
            removed += 1
        except Exception:
            pass
    if removed:
        print(f"   🧹 중복된 임시 초안 {removed}개 정리(1개는 재사용).", flush=True)
    return removed


def _create_temp_draft(blog_id: str):
    """이미지 업로드용 임시 초안의 (draft_id, service) 반환.
    ★ 재사용 우선(2026-07-14): Blogger는 '블로그당 하루 50개 글 생성' 스팸방지 한도가 있고
    **초안 생성도 이 한도를 소모**한다. 예전엔 업로드 때마다 초안을 새로 만들고 지워서
    사진 올릴 때마다 하루 한도를 깎아먹었고, 재발행을 반복하니 429로 막혔다.
    이제 기존 '(임시) 이미지 업로드' 초안이 있으면 **그대로 재사용**(쓰기 0회)하고,
    없을 때만 하나 만든다. 업로드가 끝나도 지우지 않고 남겨 다음에 다시 쓴다."""
    try:
        import publish_today as pub
        from googleapiclient.discovery import build
        creds = pub.get_credentials()
        service = build("blogger", "v3", credentials=creds)
    except Exception as e:
        print(f"   ⚠️  인증 실패({e}) — 새 글 에디터로 진행합니다.", flush=True)
        return None, None
    # ① 이미 있는 임시 초안 재사용(쓰기 할당량 0) — 여러 개면 1개만 남기고 정리
    existing = _find_temp_drafts(service, blog_id)
    if existing:
        if len(existing) > 1:
            _cleanup_stale_temp_drafts(service, blog_id, keep=1)
        print("   ♻️  기존 임시 초안 재사용(쓰기 할당량 절약)", flush=True)
        return existing[0], service
    # ② 없을 때만 새로 생성
    try:
        d = service.posts().insert(
            blogId=blog_id,
            body={"title": _TEMP_DRAFT_TITLE, "content": "<p>temp</p>"},
            isDraft=True).execute()
        return d["id"], service
    except Exception as e:
        # 쓰기 할당량 소진(429)은 재시도해도 소용없다(리셋 전까지 계속 실패) → 즉시 위로 올려
        # 발행 작업 전체를 멈춘다. 예전엔 20/40/60초씩 재시도하다 '새 글 에디터'로 폴백해
        # 글마다 몇 분씩 헛돌았다(2026-07-14 수정).
        if pub.is_quota_error(e):
            raise pub.QuotaExceededError(
                "Blogger 쓰기 할당량 소진(429) — 이미지 업로드용 임시 초안을 만들 수 없습니다.") from e
        print(f"   ⚠️  임시 초안 생성 실패({e}) — 새 글 에디터로 진행합니다.", flush=True)
        return None, None


def _open_draft_editor(page, blog_id: str, draft_id: str):
    """임시 초안의 '편집' 페이지를 직접 엽니다 (새 글 URL 리다이렉트 금지).
    로그인이 필요하면 그 화면에서 대기."""
    editor_url = f"https://www.blogger.com/blog/post/edit/{blog_id}/{draft_id}"
    print("   🌐 Blogger 에디터(초안) 여는 중...", flush=True)
    page.goto(editor_url, wait_until="domcontentloaded", timeout=40000)
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    time.sleep(2)
    # 도구막대가 뜰 때까지 대기 (이 URL을 유지 — 새 글 URL로 보내지 않음)
    waited = 0
    announced = False
    while waited < 180:
        if _editor_ready(page):
            print("   ✅ 에디터(초안) 준비 완료", flush=True)
            return
        if _needs_login(page) or "accounts.google" in (page.url or ""):
            if not announced:
                print("   🔑 이 블로그를 편집할 수 있는 계정으로 로그인하세요 "
                      "(새로고침 없이 대기).", flush=True)
                announced = True
            time.sleep(3); waited += 3
            continue
        # 로그인 직후 등으로 에디터 URL을 벗어났으면 초안 편집 URL로 복귀
        if "/edit/" not in (page.url or ""):
            try:
                page.goto(editor_url, wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass
        time.sleep(2); waited += 2
    print("   ⚠️  에디터 도구막대를 찾지 못했습니다(로그인/권한 확인 필요).", flush=True)


def upload_images_to_blogger(photos: list) -> list:
    """
    photos: Path 목록
    반환:   [(Path, cdn_url), ...]  — 실패 항목은 제외

    동작: API로 임시 초안 생성 → 그 초안의 편집 페이지에서 이미지 업로드
          → CDN URL 수집 → 임시 초안 삭제. ('새 글' 딥링크 403 회피)
    최초 실행 시 브라우저가 열리면 구글 계정으로 로그인하세요.
    """
    if not photos:
        return []

    blog_id = get_blog_id()
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    results = []

    draft_id, service = _create_temp_draft(blog_id)

    with sync_playwright() as p:
        try:
            ctx = _launch_ctx(p, PROFILE_DIR)
            # 새 에디터의 OS 파일선택창(File System Access API)을 가로채는 오버라이드
            ctx.add_init_script(FILE_PICKER_OVERRIDE)
        except RuntimeError as e:
            print(f"   ❌ {e}", flush=True)
            # 임시 초안은 재사용을 위해 남겨 둔다(삭제하면 다음에 새로 만들어야 해서
            # Blogger '하루 50개 글 생성' 한도를 소모 — 2026-07-14).
            return []

        page = ctx.new_page()
        if draft_id:
            _open_draft_editor(page, blog_id, draft_id)
        else:
            _open_new_post_editor(page, blog_id)

        # 에디터가 안 열렸으면(권한/로그인 문제) 장수만큼 실패를 반복하지 않고 즉시 중단
        if not _editor_ready(page):
            print("   ❌ 에디터에 접근할 수 없어 이미지 업로드를 중단합니다.", flush=True)
            print("      → 블로그 스튜디오의 [🔐 로그인 관리]에서 '로그인·권한 확인'을 실행해", flush=True)
            print("        어떤 계정으로 로그인돼 있는지/편집 권한이 있는지 확인하세요.", flush=True)
            try:
                ctx.close()
            except Exception:
                pass
            # 임시 초안은 재사용을 위해 남겨 둔다(삭제하면 다음에 새로 만들어야 해서
            # Blogger '하루 50개 글 생성' 한도를 소모 — 2026-07-14).
            return []

        total = len(photos)
        for i, photo in enumerate(photos):
            print(f"   🖼  ({i+1}/{total}) {photo.name}", flush=True)
            try:
                url = _upload_one(page, photo)
                results.append((photo, url))
                print(f"        ✅ {url[:80]}", flush=True)
            except Exception as e:
                print(f"        ⚠️  실패: {e}", flush=True)
                # 실패로 메뉴/다이얼로그가 열린 채 남았을 수 있으니 닫고 다음 사진으로
                try:
                    page.keyboard.press("Escape")
                    time.sleep(0.6)
                except Exception:
                    pass

        # 초안 저장 없이 닫기 (새 글 에디터 폴백 경로에서만 의미 있음)
        if not draft_id:
            try:
                for discard_text in ["Discard", "삭제", "취소"]:
                    btn = page.locator(f'button:has-text("{discard_text}")').first
                    if btn.is_visible(timeout=800):
                        btn.click()
                        break
            except Exception:
                pass

        ctx.close()

    # 임시 초안은 '지우지 않고' 내용만 비워 다음 업로드에 재사용한다(2026-07-14).
    # 지웠다가 다시 만들면 초안 생성이 Blogger의 '하루 50개 글 생성' 한도를 소모해
    # 사진을 여러 번 올리는 날 429로 막힌다. patch(내용 비우기)는 '글 생성'이 아니라 안전.
    # (업로드된 이미지의 CDN URL은 초안 내용을 비워도 그대로 유지됨)
    if draft_id:
        try:
            service.posts().patch(blogId=blog_id, postId=draft_id,
                                  body={"content": "<p>temp</p>"}).execute()
            print("   ♻️  임시 초안 비움(다음 업로드에 재사용)", flush=True)
        except Exception as e:
            print(f"   ⚠️  임시 초안 비우기 실패(다음에 재사용은 계속 가능): {e}", flush=True)

    return results


# ── 단독 실행 (테스트용) ────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("사용법: python upload_via_browser.py <사진폴더경로>")
        sys.exit(1)
    folder = Path(sys.argv[1])
    photos = sorted(
        p for ext in ("*.jpg", "*.jpeg", "*.png", "*.webp")
        for p in folder.rglob(ext)
    )
    if not photos:
        print(f"사진 없음: {folder}")
        sys.exit(1)
    print(f"사진 {len(photos)}장 발견")
    results = upload_images_to_blogger(photos)
    print(f"\n업로드 완료: {len(results)}/{len(photos)}")
    for path, url in results:
        print(f"  {path.name:40s} → {url[:60]}")
