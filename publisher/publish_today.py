"""
Korea Arts & Travel — Blogger Auto-Publisher v3
================================================
실행 순서:
  1. C:\\blogger\\publisher\\ 에서 날짜가 붙은 발행 대기 파일 탐색
     (post_en_YYYY-MM-DD.html / post_ko_YYYY-MM-DD.html / config_YYYY-MM-DD.json)
  2. 각 날짜 포스트 발행 → C:\\blogger\\YYYY-MM-DD\\ 폴더명에 -- 접두사 추가
  3. 오늘의 포스팅 발행 (post_en.html / post_ko.html / post_config.json)
     → C:\\blogger\\YYYY-MM-DD\\ 폴더 완료 표시

Requirements:
  pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client requests pillow
"""

import json, re, sys, time, base64, io, random
from datetime import date, datetime, timedelta
from pathlib import Path

# Windows 콘솔 UTF-8 강제 설정
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if sys.stderr.encoding and sys.stderr.encoding.lower() != 'utf-8':
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# ── 경로 설정 ─────────────────────────────────────────────────────────────────
SCRIPT_DIR   = Path(__file__).parent
CONFIG_FILE  = SCRIPT_DIR / "post_config.json"
EN_HTML      = SCRIPT_DIR / "post_en.html"
KO_HTML      = SCRIPT_DIR / "post_ko.html"
TOKEN_FILE   = SCRIPT_DIR / "token.json"
SECRETS_FILE = SCRIPT_DIR / "client_secrets.json"
BLOGGER_ROOT = SCRIPT_DIR.parent   # publisher/의 부모 — 어디 복사해 두든 자동으로 맞음(2026-07-24)
TODAY_STR    = date.today().strftime("%Y-%m-%d")

SCOPES = ["https://www.googleapis.com/auth/blogger"]

IMGBB_KEY_FILE = SCRIPT_DIR / "imgbb_key.txt"   # https://api.imgbb.com/ 에서 발급

# ── Google 인증 ───────────────────────────────────────────────────────────────

def get_credentials():
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request

    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not SECRETS_FILE.exists():
                print(f"❌ {SECRETS_FILE.name} 없음.")
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(str(SECRETS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())
    return creds


TARGET_BLOG_URL = "k-arts-travel.blogspot.com"   # 자동 선택할 블로그 URL 키워드
BLOG_ID_FILE   = SCRIPT_DIR / "blog_id.txt"


def get_blog_id(service):
    # 저장된 blog_id가 있으면 바로 사용
    if BLOG_ID_FILE.exists():
        saved = BLOG_ID_FILE.read_text().strip().split("\n")
        if len(saved) == 2:
            print(f"   블로그: {saved[1]}")
            return saved[0], saved[1]

    items = service.blogs().listByUser(userId="self").execute().get("items", [])
    if not items:
        print("❌ Blogger 블로그를 찾을 수 없습니다.")
        sys.exit(1)

    # TARGET_BLOG_URL 키워드로 자동 선택
    for b in items:
        if TARGET_BLOG_URL in b.get("url", ""):
            print(f"   자동 선택: {b['name']} ({b['url']})")
            BLOG_ID_FILE.write_text(f"{b['id']}\n{b['url']}")
            return b["id"], b["url"]

    # 자동 선택 실패 시 목록 출력 후 종료 안내
    print("❌ 대상 블로그를 찾을 수 없습니다. blog_id.txt를 직접 만들어 주세요.")
    print("   형식: 첫 줄에 Blog ID, 둘째 줄에 Blog URL")
    for b in items:
        print(f"   - {b['name']}: ID={b['id']} / {b['url']}")
    sys.exit(1)

# ── 사진 유틸 ─────────────────────────────────────────────────────────────────

def find_date_folder(date_str: str) -> Path:
    """
    날짜 문자열(YYYY-MM-DD 또는 YYYYMMDD)로 C:\\blogger 하위 폴더를 찾습니다.
    실제 폴더 구조: C:\\blogger\\YYYY\\MM\\YYYYMMDD\\ (하이픈 없는 형식)
    또는 C:\\blogger\\YYYY-MM-DD\\ 형식도 지원합니다.
    """
    compact = date_str.replace("-", "")          # YYYYMMDD
    year    = compact[:4]
    month   = compact[4:6]

    candidates = [
        BLOGGER_ROOT / year / month / compact,             # C:\blogger\2026\06\20260606
        BLOGGER_ROOT / year / month / f"--{compact}",      # C:\blogger\2026\06\--20260606
        BLOGGER_ROOT / year / month / f"----{compact}",    # C:\blogger\2026\06\----20260606
        BLOGGER_ROOT / date_str,                            # C:\blogger\2026-06-06
        BLOGGER_ROOT / f"--{date_str}",                     # C:\blogger\--2026-06-06
        BLOGGER_ROOT / year / date_str,                     # C:\blogger\2026\2026-06-06
        BLOGGER_ROOT / year / month / f"--{date_str}",     # C:\blogger\2026\06\--2026-06-06
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def find_photos(folder: Path):
    if not folder or not folder.exists():
        return []
    photos = []
    for ext in ("*.jpg", "*.jpeg", "*.png", "*.webp"):
        photos.extend(folder.rglob(ext))
    return sorted(photos)


def compress_image(path: Path, max_width: int = 900, quality: int = 72) -> bytes:
    """이미지를 최대 900px JPEG로 압축해 bytes 반환."""
    try:
        from PIL import Image, ImageOps
        img = Image.open(path)
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")
        if img.width > max_width:
            ratio = max_width / img.width
            img = img.resize((max_width, int(img.height * ratio)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue()
    except Exception as e:
        print(f"   ⚠️  PIL 오류 ({path.name}): {e}", flush=True)
        return path.read_bytes()


def preload_photos(photos: list, access_token: str = None) -> list:
    """Blogger 웹 에디터(Playwright)로 사진을 구글 CDN에 업로드해
    (path, url) 목록으로 반환합니다.
    최초 실행 시 브라우저가 열리면 구글 계정 로그인 후 자동 진행됩니다."""
    if not photos:
        return []
    try:
        from upload_via_browser import upload_images_to_blogger
    except ImportError:
        print("   ⚠️  upload_via_browser 모듈 없음.", flush=True)
        print("      publisher 폴더에 upload_via_browser.py가 있는지 확인하세요.", flush=True)
        return []
    print(f"   🌐 Blogger 에디터로 이미지 {len(photos)}장 업로드...", flush=True)
    return upload_images_to_blogger(photos)


class QuotaExceededError(RuntimeError):
    """Blogger 쓰기 할당량 소진(429 rateLimitExceeded).
    읽기와 달리 쓰기(글 발행·수정·초안 생성)는 할당량이 훨씬 빡빡하고, 소진되면 몇 초~몇 분
    재시도해도 소용없다(보통 태평양 자정 = 한국시간 오후 4시경 리셋). 그래서 이 오류는
    재시도하지 않고 즉시 위로 올려 발행 작업 전체를 멈추는 데 쓴다."""
    pass


def is_quota_error(e) -> bool:
    """예외가 Blogger 할당량/속도 제한(429)인지 판별.
    ① 우리가 던진 QuotaExceededError는 메시지 언어와 무관하게 타입으로 판별,
    ② 구글 API가 던지는 원시 HttpError는 메시지 문자열로 판별."""
    if isinstance(e, QuotaExceededError):
        return True
    s = str(e).lower()
    return ("429" in s or "ratelimitexceeded" in s or "rate limit" in s
            or "resource has been exhausted" in s or "quota" in s)


def make_img_tag(src: str, alt: str) -> str:
    # width:100% + height:auto 로 모든 사진을 본문 폭에 균일하게 맞춥니다.
    # (max-width만 쓰면 작은 사이즈로 올라온 사진이 더 작게 보이는 문제가 생김)
    # ★HTML width="100%" 속성도 함께 박음(2026-07-10): Blogger 웹 에디터에서 사진 순서·
    #   위치를 바꾸면 CSS style의 width:100%만 있을 때 에디터가 원본 픽셀로 되돌려버려
    #   그 사진만 작아지는 문제가 있었음 — HTML width 속성이 남아 있으면 순서를 바꿔도
    #   크기가 잘 유지됨(완전 보장은 아니지만 대부분 방지).
    return (f'<figure style="margin:1.5em 0;text-align:center;">'
            f'<img src="{src}" alt="{alt}" width="100%" '
            f'style="width:100%;height:auto;border-radius:6px;display:block;" loading="lazy">'
            f'<figcaption style="font-size:12px;color:#888;margin-top:4px;">{alt}</figcaption>'
            f'</figure>')


def inject_photos(html: str, photo_uris: list, lang: str = "en") -> str:
    """
    photo_uris: preload_photos()가 반환한 (Path, uri) 리스트.
    lang: "en" 또는 "ko" — 갤러리 제목과 alt 텍스트 언어 결정.
    1. <!-- IMAGE_N alt="..." --> 플레이스홀더를 해당 사진으로 교체
    2. 나머지 사진 전부를 본문 마지막 갤러리로 추가
    """
    if not photo_uris:
        return html

    used = set()

    def replacer(m):
        idx = int(m.group(1)) - 1
        alt = m.group(2)
        if idx < len(photo_uris):
            used.add(idx)
            _, uri = photo_uris[idx]
            return make_img_tag(uri, alt)
        return ''

    html = re.sub(r'<!--\s*IMAGE_(\d+)\s+alt="([^"]*?)"\s*-->', replacer, html)

    remaining = [i for i in range(len(photo_uris)) if i not in used]
    if remaining:
        gallery_title = "📷 Photos" if lang == "en" else "📷 사진"
        gallery = f'\n<hr>\n<h2>{gallery_title}</h2>\n'
        for i in remaining:
            p, uri = photo_uris[i]
            # 영문 페이지: "Photo N", 한국어 페이지: 한국어 파일명 그대로
            alt = f"Photo {i + 1}" if lang == "en" else p.stem.replace('_', ' ')
            gallery += make_img_tag(uri, alt) + '\n'
        html += gallery

    return html

# ── 발행 ──────────────────────────────────────────────────────────────────────


def _rfc3339_kst(dt):
    """datetime → Blogger published 용 RFC3339(한국 +09:00) 문자열."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S+09:00")


def _post_body(title, body, labels, meta=None, published=None):
    """Blogger posts().insert/update 용 body 딕셔너리를 만듭니다.
    meta가 있으면 searchDescription, published가 있으면 발행 시각(RFC3339)도 설정."""
    b = {"title": title, "content": body, "labels": labels}
    if meta:
        b["searchDescription"] = meta[:150]   # Blogger 검색 설명 최대 150자
    if published:
        b["published"] = published            # 발행 표시 시각 지정
    return b


def _verify_search_description(service, blog_id, post_id, expected_meta, label):
    """발행 직후 라이브 포스트를 다시 읽어 searchDescription이 실제로
    저장됐는지 확인합니다. (2026-06-08 버그 — API에 보냈다고 믿었지만
    실제로는 비어 있던 채로 몇 주간 방치됐던 사고의 재발 방지용 안전장치.
    이 점검이 없으면 같은 실수가 다시 생겨도 한참 뒤에야 발견하게 됩니다.)"""
    if not expected_meta:
        return
    # Blogger API는 쓰기 직후 읽으면 캐시 지연으로 빈 값이 올 수 있어
    # 3초 간격으로 최대 3회 재시도합니다.
    for attempt in range(3):
        try:
            if attempt > 0:
                time.sleep(3)
            # 일부 블로그에서 fields="searchDescription" 가 400(invalidParameter)을 내므로
            # 필드 제한 없이 전체 포스트를 받아 searchDescription만 읽습니다.
            live = service.posts().get(blogId=blog_id, postId=post_id).execute()
            live_desc = (live.get("searchDescription") or "").strip()
            if live_desc:
                print(f"   ✅ {label} 검색 설명 저장 확인 ({len(live_desc)}/150자)")
                return
        except Exception as e:
            print(f"   ⚠️  {label} 검색 설명 확인 중 오류: {e}")
            return
    print(f"   ⚠️  경고: {label} 검색 설명이 비어 있습니다! "
          f"(보낸 값: \"{expected_meta[:40]}...\") "
          f"Blogger 대시보드에서 직접 확인/입력하세요.")


def _url_matches_slug(post_url: str, slug: str) -> bool:
    """발행된 URL이 의도한 슬러그와 '실질적으로' 일치하는지.
    Blogger는 긴 슬러그를 잘라내므로, 잘린 접두사도 일치로 인정합니다.
    (잘림을 불일치로 오판해 삭제·재발행하면 죽은 URL과 _숫자 접미사가 생겨
    Search Console 색인 오류의 원인이 됩니다.)"""
    m = re.search(r"/([^/]+?)(?:\.html?)?$", post_url or "")
    if not m:
        return False
    fname = m.group(1)
    if fname == slug or slug in fname:
        return True
    # Blogger가 긴 슬러그를 잘라낸 경우: URL 파일명이 슬러그의 접두사면 OK
    if len(fname) >= 15 and slug.startswith(fname):
        return True
    return False


def _slug_filename(post_url: str) -> str:
    m = re.search(r"/([^/]+?)(?:\.html?)?$", post_url or "")
    return m.group(1) if m else ""


def _is_slug_collision(post_url: str, slug: str) -> bool:
    """Blogger가 슬러그 충돌 시 붙이는 '_숫자' 접미사가 URL에 생겼는지.
    (우리 슬러그는 영소문자·하이픈만이라 '_숫자'는 항상 Blogger의 충돌 표식.
     단순 잘림은 _숫자 없이 짧아질 뿐이므로 충돌로 보지 않음.)"""
    fname = _slug_filename(post_url)
    return bool(re.search(r"_\d+$", fname)) and fname != slug


def _differentiate_slug(slug: str, n: int) -> str:
    """언어 접미사(-en/-ko)는 끝에 유지하면서 그 앞에 -n 구분자를 넣는다.
    예) busan-guide-en → busan-guide-2-en"""
    m = re.match(r"^(.*?)(-(?:en|ko))$", slug or "")
    if m:
        return f"{m.group(1)}-{n}{m.group(2)}"
    return f"{slug}-{n}"


def publish_with_slug(service, blog_id, real_title, slug, body, labels, meta=None, published=None):
    """slug를 제목으로 발행해 URL을 슬러그 기반으로 고정하고 (url, id)를 반환. 딱 1회만 발행.
    ★2026-07-14 재작성: 예전엔 URL 끝의 '_숫자'(이미 같은 슬러그 글이 있을 때 Blogger가 붙이는
    고유번호)를 '충돌'로 보고 그 글을 삭제→구분자(-2,-3)로 최대 4회 재발행했다. 그런데
    ① 같은 주제를 재발행할 때마다 옛 글이 안 지워지면(getByPath가 _숫자 URL을 못 찾아 404)
    모든 슬러그가 이미 점유돼 매 시도가 또 _숫자가 붙어 4회를 다 소모, ② 마지막 시도에서
    글을 만들고 삭제한 뒤 그 '삭제된 id'를 반환해 호출부 update가 404로 크래시, ③ 그 크래시로
    '발행 완료'가 저장되지 않아 자동 발행이 같은 글을 무한 반복하며 중복글을 쌓고 하루 쓰기
    한도를 순식간에 태워 429가 났다(글 하나당 쓰기 최대 18회).
    → 이제 1회만 발행하고 Blogger가 준 실제 URL('_숫자' 포함)을 그대로 저장한다. 실제 URL을
    저장하니 다음 재발행 때 '기존 글 삭제'(getByPath)도 정확히 맞아 중복이 안 쌓인다.
    (_숫자 접미사는 URL로 정상 동작하며 SEO에도 무해)."""
    print(f"   → 발행 중 (슬러그: {slug}) ...", flush=True)
    post = service.posts().insert(
        blogId=blog_id,
        body=_post_body(slug, body, labels, meta, published),
        isDraft=False,
    ).execute()
    post_id, post_url = post.get("id"), post.get("url")
    print(f"   → 발행 완료: {post_url}", flush=True)
    return post_url, post_id


def publish_post(service, blog_id, title, body, labels, meta=None, published=None):
    print(f"   → 발행 중: {title[:40]} ...", flush=True)
    post = service.posts().insert(
        blogId=blog_id,
        body=_post_body(title, body, labels, meta, published),
        isDraft=False,
    ).execute()
    return post.get("url", ""), post.get("id", "")


def strip_template_lang_links(html: str) -> str:
    """HTML 템플릿에 포함된 언어 전환 링크를 제거합니다.
    플레이스홀더([포스트 URL]) 형태와 실제 URL 형태 모두 제거합니다."""
    # 플레이스홀더 형태: href="[...포스트 URL...]"
    html = re.sub(
        r'<p[^>]*>\s*[🇰🇷🇺🇸]?\s*<a\s+href="\[[^\]]*(?:포스트\s*URL|POST_URL)[^\]]*\]"[^>]*>.*?</a>\s*</p>\s*',
        '', html, flags=re.DOTALL
    )
    html = re.sub(r'[^\n]*href="\[[^\]]*(?:포스트\s*URL|POST_URL)[^\]]*\]"[^\n]*\n?', '', html)
    # 실제 URL 형태: 언어 전환 텍스트(한국어로 읽기 / Read in English)가 있는 링크 라인
    html = re.sub(
        r'<p[^>]*>\s*[🇰🇷🇺🇸]\s*<a\s[^>]*>(?:한국어로 읽기|Read in English)</a>\s*</p>\s*',
        '', html, flags=re.DOTALL
    )
    return html


def publish_pair(service, blog_id, cfg, en_body, ko_body, base_dt=None):
    """영문·한국어 쌍 발행 + 언어 전환 링크 삽입. 두 포스트 모두 slug 기반 URL 적용.
    base_dt: 발행 표시 기준 시각(예약된 날짜·시각). None이면 '지금'."""
    # 템플릿에 이미 있는 플레이스홀더 언어 링크 제거 (중복 방지)
    en_body = strip_template_lang_links(en_body)
    ko_body = strip_template_lang_links(ko_body)

    en_meta = cfg.get("en_meta", "").strip()
    ko_meta = cfg.get("ko_meta", "").strip()

    # 발행 표시 시각: 예약된 날짜·시각(base_dt)을 기준으로. 없으면 지금.
    # 미래로 예약된 글을 수동 발행하면 미래 예약 대신 '지금'으로 찍습니다.
    now_real = datetime.now()
    base = base_dt or now_real
    if base > now_real:
        base = now_real
    gap = random.randint(60, 120)   # 분 (1시간 ~ 2시간)
    en_published = _rfc3339_kst(base)
    ko_published = _rfc3339_kst(base - timedelta(minutes=gap))
    print(f"   ⏱ 발행 표시 시각: {base.strftime('%Y-%m-%d %H:%M')} "
          f"(🇰🇷 한국어를 🇺🇸 영문보다 {gap // 60}시간 {gap % 60}분 이르게)")

    # 영문 포스트 — en_slug 있으면 slug 방식, 없으면 제목 그대로 (영문은 자동으로 잘 됨)
    en_slug = cfg.get("en_slug", "").strip()
    if en_slug:
        en_url, en_id = publish_with_slug(service, blog_id,
                                           cfg["en_title"], en_slug,
                                           en_body, cfg["en_labels"], en_meta, en_published)
    else:
        en_url, en_id = publish_post(service, blog_id,
                                      cfg["en_title"], en_body, cfg["en_labels"], en_meta, en_published)
    print(f"   🇺🇸 {en_url}")

    # 한국어 포스트 — ko_slug 필수 적용
    ko_slug = cfg.get("ko_slug", "").strip()
    if ko_slug:
        ko_url, ko_id = publish_with_slug(service, blog_id,
                                           cfg["ko_title"], ko_slug,
                                           ko_body, cfg["ko_labels"], ko_meta, ko_published)
    else:
        ko_url, ko_id = publish_post(service, blog_id,
                                      cfg["ko_title"], ko_body, cfg["ko_labels"], ko_meta, ko_published)
    print(f"   🇰🇷 {ko_url}")

    # 실제 URL로 언어 전환 링크 추가 (포스트 맨 위)
    def with_lang_link(body, lang):
        if lang == "en":
            hdr = (f'<p style="text-align:right;font-size:14px;">'
                   f'🇰🇷 <a href="{ko_url}">한국어로 읽기</a></p>\n')
        else:
            hdr = (f'<p style="text-align:right;font-size:14px;">'
                   f'🇺🇸 <a href="{en_url}">Read in English</a></p>\n')
        return hdr + body

    time.sleep(2)   # 발행 직후 URL 안정화 대기(슬러그→실제 제목 update를 이 단계로 합침)
    service.posts().update(
        blogId=blog_id, postId=en_id,
        body=_post_body(cfg["en_title"], with_lang_link(en_body, "en"),
                        cfg["en_labels"], en_meta, en_published),
    ).execute()
    service.posts().update(
        blogId=blog_id, postId=ko_id,
        body=_post_body(cfg["ko_title"], with_lang_link(ko_body, "ko"),
                        cfg["ko_labels"], ko_meta, ko_published),
    ).execute()
    print(f"   🔗 언어 링크 삽입 완료")

    # Blogger API posts.update 는 searchDescription 을 update body 에 넣어도
    # 조용히 무시하는 버그가 있어, patch 로 별도 저장합니다.
    if en_meta:
        service.posts().patch(
            blogId=blog_id, postId=en_id,
            body={"searchDescription": en_meta[:150]},
        ).execute()
    if ko_meta:
        service.posts().patch(
            blogId=blog_id, postId=ko_id,
            body={"searchDescription": ko_meta[:150]},
        ).execute()
    print(f"   🔍 검색 설명 patch 저장 완료")

    return en_url, ko_url


def post_id_by_url(service, blog_id, url):
    """발행된 포스트 URL로 post id를 조회(getByPath)."""
    try:
        from urllib.parse import urlsplit
        path = urlsplit(url).path
        post = service.posts().getByPath(blogId=blog_id, path=path).execute()
        return post.get("id")
    except Exception as e:
        print(f"   ⚠️ 포스트 조회 실패({url}): {e}", flush=True)
        return None


def patch_post_content(service, blog_id, post_id, content):
    """발행된 포스트의 본문만 교체(제목·라벨·발행일은 그대로 유지)."""
    service.posts().patch(
        blogId=blog_id, postId=post_id, body={"content": content}).execute()


def mark_done(date_str: str):
    """날짜 폴더를 찾아 이름 앞에 -- 를 붙여 완료 표시합니다.
    이미 --로 시작하면 중복 접두사를 붙이지 않습니다."""
    folder = find_date_folder(date_str)
    if folder and folder.exists():
        if folder.name.startswith("--"):
            print(f"   📁 이미 완료 표시됨: {folder.name}")
            return
        done = folder.parent / f"--{folder.name}"
        folder.rename(done)
        print(f"   📁 폴더 완료 표시: --{folder.name}")

# ── 대기 파일 탐색 ────────────────────────────────────────────────────────────

def find_pending_dated_posts():
    """
    publisher 폴더에서 날짜가 붙은 파일 세트를 찾습니다.
    Claude 스케줄 태스크가 과거 날짜 포스트를 생성하면
    config_YYYY-MM-DD.json / post_en_YYYY-MM-DD.html / post_ko_YYYY-MM-DD.html 형태로 저장합니다.
    """
    pattern = re.compile(r"^config_(\d{4}-\d{2}-\d{2})\.json$")
    results = []
    for f in sorted(SCRIPT_DIR.glob("config_*.json")):
        m = pattern.match(f.name)
        if not m:
            continue
        date_str = m.group(1)
        if date_str >= TODAY_STR:
            continue
        en_f = SCRIPT_DIR / f"post_en_{date_str}.html"
        ko_f = SCRIPT_DIR / f"post_ko_{date_str}.html"
        if en_f.exists() and ko_f.exists():
            results.append((date_str, f, en_f, ko_f))
    return results

# ── 메인 ──────────────────────────────────────────────────────────────────────

def main():
    print("\n" + "="*60)
    print("  Korea Arts & Travel — 자동 발행 시작")
    print(f"  오늘: {TODAY_STR}")
    print("="*60)

    print("\n🔐 Google 인증 확인…")
    creds = get_credentials()
    token = creds.token  # Picasa CDN 업로드에 사용
    from googleapiclient.discovery import build
    service = build("blogger", "v3", credentials=creds)
    blog_id, blog_url = get_blog_id(service)
    print(f"📝 블로그: {blog_url}\n")

    # ── Phase 1: 날짜별 대기 포스트 발행 ─────────────────────────────────────
    pending = find_pending_dated_posts()
    if pending:
        print(f"📂 대기 중인 과거 포스트 {len(pending)}개:")
        for date_str, cfg_f, en_f, ko_f in pending:
            print(f"\n{'─'*50}")
            print(f"📅 발행: {date_str}")
            cfg        = json.loads(cfg_f.read_text(encoding="utf-8"))
            photos     = find_photos(find_date_folder(date_str))
            print(f"   🖼  사진 {len(photos)}장 — CDN 업로드 시작 (한 번만 처리)")
            photo_uris = preload_photos(photos)
            en_body    = inject_photos(en_f.read_text(encoding="utf-8"), photo_uris, lang="en")
            ko_body    = inject_photos(ko_f.read_text(encoding="utf-8"), photo_uris, lang="ko")
            publish_pair(service, blog_id, cfg, en_body, ko_body)
            mark_done(date_str)
            # 사용한 파일 정리
            cfg_f.unlink(missing_ok=True)
            en_f.unlink(missing_ok=True)
            ko_f.unlink(missing_ok=True)
    else:
        print("📂 대기 중인 과거 포스트 없음.")

    # ── Phase 2: 오늘의 포스팅 발행 ─────────────────────────────────────────
    print(f"\n{'─'*50}")
    print(f"📅 오늘의 포스팅: {TODAY_STR}")

    if not CONFIG_FILE.exists() or not EN_HTML.exists() or not KO_HTML.exists():
        print("⚠️  오늘 포스트 파일 없음 — Claude 스케줄 태스크 완료 후 다시 실행하세요.")
    else:
        cfg          = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        today_folder = find_date_folder(TODAY_STR)
        photos       = find_photos(today_folder)
        if photos:
            print(f"   🖼  사진 {len(photos)}장 발견 — CDN 업로드 시작 (한 번만 처리)")
            photo_uris = preload_photos(photos)
        else:
            photo_uris = []
        en_body = inject_photos(EN_HTML.read_text(encoding="utf-8"), photo_uris, lang="en")
        ko_body = inject_photos(KO_HTML.read_text(encoding="utf-8"), photo_uris, lang="ko")
        en_url, ko_url = publish_pair(service, blog_id, cfg, en_body, ko_body)
        mark_done(TODAY_STR)
        print(f"\n   📌 {cfg.get('category','')}")
        print(f"   🇺🇸 {en_url}")
        print(f"   🇰🇷 {ko_url}")

    print("\n" + "="*60)
    print("✅ 발행 완료!")
    print("="*60 + "\n")


if __name__ == "__main__":
    main()
