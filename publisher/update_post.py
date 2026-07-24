"""
update_post.py — 이미 발행된 포스트를 폴더의 모든 사진으로 업데이트합니다.

사용법:
  python update_post.py 2026-06-06       # 날짜로 찾아 업데이트
  python update_post.py                  # 날짜 없으면 목록에서 선택
"""
import sys, os
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.environ.setdefault("PYTHONUTF8", "1")

import re, base64, io, time, json, requests, xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime, timedelta

SCRIPT_DIR   = Path(__file__).resolve().parent
BLOGGER_ROOT = SCRIPT_DIR.parent
TOKEN_FILE   = SCRIPT_DIR / "token.json"
BLOG_ID_FILE = SCRIPT_DIR / "blog_id.txt"
TARGET_BLOG_URL = "k-arts-travel.blogspot.com"

# ── 인증 ──────────────────────────────────────────────────────────────────────
def get_credentials():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    if not TOKEN_FILE.exists():
        sys.exit("token.json 없음 — publish_today.py 로 먼저 인증하세요.")
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE))
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    return creds

def get_blog_id(service):
    if BLOG_ID_FILE.exists():
        saved = BLOG_ID_FILE.read_text().strip().split("\n")
        if len(saved) >= 1:
            return saved[0], saved[1] if len(saved) > 1 else ""
    items = service.blogs().listByUser(userId="self").execute().get("items", [])
    for b in items:
        if TARGET_BLOG_URL in b.get("url", ""):
            BLOG_ID_FILE.write_text(f"{b['id']}\n{b['url']}")
            return b["id"], b["url"]
    sys.exit("블로그를 찾을 수 없습니다.")

# ── 이미지 업로드 ────────────────────────────────────────────────────────────
def compress_image(path: Path, max_width: int = 900, quality: int = 72) -> bytes:
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
        print(f"   ⚠️  이미지 오류 ({path.name}): {e}")
        return path.read_bytes()


def upload_to_blogger_cdn(photos: list, access_token: str = None) -> dict:
    """Blogger 웹 에디터(Playwright)로 사진을 구글 CDN에 업로드해
    {index: url} 딕셔너리로 반환합니다."""
    try:
        from upload_via_browser import upload_images_to_blogger
    except ImportError:
        print("   ⚠️  upload_via_browser 모듈 없음.")
        return {}
    results = upload_images_to_blogger(photos)
    return {i: url for i, (_, url) in enumerate(results)}


# 하위 호환용 — restore_photos_by_keyword.py 에서 import 함 (사용 안 함)
def photo_to_data_uri(path: Path, max_width: int = 800, quality: int = 50) -> str:
    return ""

def has_hangul(text: str) -> bool:
    return any('가' <= ch <= '힣' for ch in text)


def lang_ok(text: str, is_ko: bool) -> bool:
    """alt 텍스트의 언어가 포스트 언어와 일치하는지 확인합니다.
    (영문 포스트 원본을 한국어 포스트에 그대로 복사해 넣는 식의
     콘텐츠 준비 실수가 있어도 잘못된 언어의 설명이 올라가지 않도록 막습니다.)"""
    return has_hangul(text) if is_ko else not has_hangul(text)


def find_photos(folder: Path):
    if not folder or not folder.exists():
        return []
    photos = []
    for ext in ("*.jpg", "*.jpeg", "*.png", "*.webp"):
        photos.extend(folder.rglob(ext))
    return sorted(photos)

# ── 폴더 검색 ─────────────────────────────────────────────────────────────────
def find_date_folder(date_str: str) -> Path:
    compact = date_str.replace("-", "")
    year    = compact[:4]
    month   = compact[4:6]
    # 완료 표시(--) 포함, 이중 완료 표시(----) 포함 검색
    candidates = [
        BLOGGER_ROOT / year / month / compact,
        BLOGGER_ROOT / year / month / f"--{compact}",
        BLOGGER_ROOT / year / month / f"----{compact}",
        BLOGGER_ROOT / date_str,
        BLOGGER_ROOT / year / date_str,
        BLOGGER_ROOT / year / month / f"--{date_str}",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None

# ── 기존 포스트에서 이미지 태그 제거 ──────────────────────────────────────────
def strip_images(html: str) -> str:
    """기존 base64 이미지와 figure 태그를 제거합니다."""
    html = re.sub(r'<figure[^>]*>.*?</figure>', '', html, flags=re.DOTALL)
    html = re.sub(r'<img[^>]*src="data:image[^"]*"[^>]*>', '', html)
    html = re.sub(r'<hr>\s*<h2>📷 Photos</h2>', '', html)
    html = re.sub(r'\n{3,}', '\n\n', html)
    return html

def make_img_tag(src: str, alt: str) -> str:
    if not src:
        return ''
    return (f'<figure style="margin:1.5em 0;text-align:center;">'
            f'<img src="{src}" alt="{alt}" '
            f'style="max-width:100%;border-radius:6px;" loading="lazy">'
            f'<figcaption style="font-size:12px;color:#888;margin-top:4px;">{alt}</figcaption>'
            f'</figure>')

# ── 포스트 목록 검색 ──────────────────────────────────────────────────────────
def get_posts_around_date(service, blog_id, date_str):
    """날짜 전후 10일 범위의 포스트를 가져옵니다."""
    d = datetime.strptime(date_str, "%Y-%m-%d")
    start = (d - timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")
    end   = (d + timedelta(days=1)).strftime("%Y-%m-%dT23:59:59Z")
    result = service.posts().list(
        blogId=blog_id,
        startDate=start,
        endDate=end,
        maxResults=20,
        fetchBodies=True,
        fields="items(id,title,url,content,labels,searchDescription)",
    ).execute()
    return result.get("items", [])

def find_exact_posts_by_title(service, blog_id, titles, max_results=50):
    """제목이 정확히 일치하는 포스트만 찾습니다.
    (날짜 ±1일 범위 검색은 같은 기간에 발행된 다른 포스트까지 잡아
     사진을 덮어쓰는 사고를 일으키므로 사용하지 않습니다.)"""
    posts = list_recent_posts(service, blog_id, max_results=max_results)
    matches = []
    for p in posts:
        if p.get("title") in titles:
            full = service.posts().get(
                blogId=blog_id, postId=p["id"],
                fields="id,title,content,labels,url,searchDescription"
            ).execute()
            matches.append(full)
    return matches


def load_alt_map_from_source(title, cfg):
    """발행 당시 원본 post_en.html / post_ko.html에서 IMAGE_N → alt 설명 매핑을 복원합니다.
    (사진을 못 찾고 발행되어 라이브 포스트의 placeholder가 이미 사라진 경우를 위한 백업 경로)"""
    if not cfg:
        return {}
    src = None
    if title == cfg.get("en_title"):
        src = SCRIPT_DIR / "post_en.html"
    elif title == cfg.get("ko_title"):
        src = SCRIPT_DIR / "post_ko.html"
    if not src or not src.exists():
        return {}
    html = src.read_text(encoding="utf-8")
    mapping = {}
    for m in re.finditer(r'<!--\s*IMAGE_(\d+)\s+alt="([^"]*?)"\s*-->', html):
        mapping[int(m.group(1)) - 1] = m.group(2)
    return mapping


def list_recent_posts(service, blog_id, max_results=20):
    result = service.posts().list(
        blogId=blog_id,
        maxResults=max_results,
        fetchBodies=False,
        fields="items(id,title,url,published)",
    ).execute()
    return result.get("items", [])

# ── 메인 ──────────────────────────────────────────────────────────────────────
def main():
    print("\n" + "="*60)
    print("  Korea Arts & Travel — 기존 포스트 사진 업데이트")
    print("="*60)

    creds   = get_credentials()
    from googleapiclient.discovery import build
    service = build("blogger", "v3", credentials=creds)
    blog_id, blog_url = get_blog_id(service)
    print(f"📝 블로그: {blog_url}\n")

    # post_config.json이 있으면 정확한 제목으로 매칭합니다.
    # (날짜 ±1일 범위 검색은 같은 기간에 발행된 다른 포스트까지 잡아
    #  엉뚱한 포스트의 사진을 덮어쓰는 사고로 이어지므로, 가능하면 피합니다.)
    cfg = None
    cfg_path = SCRIPT_DIR / "post_config.json"
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            cfg = None

    # 날짜 결정
    date_str = sys.argv[1].strip() if len(sys.argv) > 1 else None
    if not date_str:
        print("최근 포스트 목록:")
        posts = list_recent_posts(service, blog_id)
        for i, p in enumerate(posts):
            print(f"  [{i}] {p.get('published','')[:10]}  {p.get('title','')}")
        idx = int(input("\n번호 선택: ").strip())
        chosen = posts[idx]
        post_id    = chosen["id"]
        post_title = chosen["title"]
        pub_date   = chosen.get("published", "")[:10]
        date_str   = pub_date
        print(f"\n선택: {post_title}")
        # 전체 컨텐츠 가져오기
        full = service.posts().get(blogId=blog_id, postId=post_id, fields="id,title,content,labels,url").execute()
        posts_to_update = [full]
    else:
        posts_to_update = []
        if cfg and cfg.get("en_title") and cfg.get("ko_title"):
            titles = {cfg["en_title"], cfg["ko_title"]}
            print(f"🎯 정확한 제목으로 검색 중...")
            posts_to_update = find_exact_posts_by_title(service, blog_id, titles)
            if posts_to_update:
                print(f"📌 제목이 일치하는 포스트 {len(posts_to_update)}개 발견 (다른 포스트는 건드리지 않습니다):")
                for p in posts_to_update:
                    print(f"  - {p.get('title','')}")
        if not posts_to_update:
            print("⚠️  제목 매칭 실패 — 날짜 ±1일 범위로 재검색합니다 (다른 포스트가 섞일 수 있으니 결과를 꼭 확인하세요).")
            posts_to_update = get_posts_around_date(service, blog_id, date_str)
            if not posts_to_update:
                print(f"⚠️  {date_str} 근처 포스트를 찾지 못했습니다.")
                sys.exit(1)
            print(f"📅 {date_str} 근처 포스트 {len(posts_to_update)}개 발견:")
            for p in posts_to_update:
                print(f"  - {p.get('title','')}")

    # 사진 폴더 찾기
    folder = find_date_folder(date_str)
    if not folder:
        print(f"⚠️  C:\\blogger 에서 {date_str} 폴더를 찾지 못했습니다.")
        sys.exit(1)
    photos = find_photos(folder)
    if not photos:
        print(f"⚠️  {folder} 에 사진이 없습니다.")
        sys.exit(1)
    print(f"\n🖼  사진 {len(photos)}장 발견: {folder}")

    # 사진을 구글 CDN에 미리 업로드 (Blogger 웹 에디터 사용, 한 번만)
    print(f"\n🖼  사진 {len(photos)}장 구글 CDN 업로드 중...")
    photo_urls = upload_to_blogger_cdn(photos)

    # 각 포스트 업데이트
    for post in posts_to_update:
        pid    = post["id"]
        title  = post["title"]
        labels = post.get("labels", [])
        html   = post.get("content", "")
        url    = post.get("url", "")
        search_desc = post.get("searchDescription", "")
        print(f"\n{'─'*50}")
        print(f"📝 업데이트: {title}")
        print(f"   URL: {url}")

        alt_map = load_alt_map_from_source(title, cfg)
        is_ko   = bool(cfg and title == cfg.get("ko_title"))
        topic   = (cfg.get("ko_title") if is_ko else cfg.get("en_title")) if cfg else title

        # 기존 이미지 제거
        html = strip_images(html)

        # IMAGE_N 플레이스홀더 교체
        used = set()

        def replacer(m):
            idx = int(m.group(1)) - 1
            alt = m.group(2)
            if idx in photo_urls:
                used.add(idx)
                if not lang_ok(alt, is_ko) and topic:
                    alt = f"{topic} 사진 {idx+1}" if is_ko else f"{topic} — photo {idx+1}"
                print(f"   🖼  IMAGE_{idx+1}: {photos[idx].name} → alt: {alt[:50]}", flush=True)
                return make_img_tag(photo_urls[idx], alt)
            return ''

        html = re.sub(r'<!--\s*IMAGE_(\d+)\s+alt="([^"]*?)"\s*-->', replacer, html)

        # 나머지 사진 갤러리
        remaining = [i for i in range(len(photos)) if i not in used and i in photo_urls]
        if remaining:
            gallery_title = "📷 사진" if is_ko else "📷 Photos"
            gallery = f'\n<hr>\n<h2>{gallery_title}</h2>\n'
            for i in remaining:
                p = photos[i]
                if i in alt_map and lang_ok(alt_map[i], is_ko):
                    alt = alt_map[i]
                elif topic:
                    alt = f"{topic} 사진 {i+1}" if is_ko else f"{topic} — photo {i+1}"
                else:
                    alt = p.stem.replace('_', ' ') if is_ko else f"Photo {i+1}"
                print(f"   🖼  갤러리 {i+1}: {p.name} → alt: {alt[:50]}", flush=True)
                gallery += make_img_tag(photo_urls[i], alt) + '\n'
            html += gallery

        # 검색 설명: 라이브에 이미 있으면 그대로 유지, 없으면 cfg의 메타 설명으로 보완
        meta = search_desc or (cfg.get("ko_meta" if is_ko else "en_meta", "") if cfg else "")
        body = {"title": title, "content": html, "labels": labels}

        service.posts().update(
            blogId=blog_id, postId=pid,
            body=body,
        ).execute()
        # Blogger API posts.update 는 searchDescription 을 무시하므로 patch 로 별도 저장
        if meta:
            service.posts().patch(
                blogId=blog_id, postId=pid,
                body={"searchDescription": meta[:150]},
            ).execute()
            print(f"   ✅ 업데이트 완료 (검색 설명 patch 저장)")
        else:
            print(f"   ✅ 업데이트 완료")
        time.sleep(1)

    print("\n" + "="*60)
    print("✅ 모든 포스트 업데이트 완료!")
    print("="*60 + "\n")

if __name__ == "__main__":
    main()
