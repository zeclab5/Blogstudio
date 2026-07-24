"""
발행된 모든 글의 검색 설명(searchDescription)을 일괄 patch합니다.
- blogs.json의 모든 블로그(프로필)를 순회
- 각 프로필의 schedule.json에서 published 항목을 찾아
- generated/{date}/config.json의 en_meta / ko_meta 를 읽어
- Blogger API posts.patch 로 저장
"""

import json, sys, time
from pathlib import Path
from urllib.parse import urlsplit

# UTF-8 콘솔
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SCRIPT_DIR     = Path(__file__).parent
PUBLISHER_DIR  = SCRIPT_DIR.parent / "publisher"
SHARED_SECRETS = PUBLISHER_DIR / "client_secrets.json"
BLOGS_FILE     = SCRIPT_DIR / "blogs.json"
PROFILES_DIR   = SCRIPT_DIR / "profiles"
SCOPES         = ["https://www.googleapis.com/auth/blogger"]


def get_service(token_file: Path):
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_file.write_text(creds.to_json(), encoding="utf-8")
    return build("blogger", "v3", credentials=creds)


def patch_desc(service, blog_id, post_url, meta, label):
    """URL로 포스트를 찾아 searchDescription 을 patch합니다."""
    try:
        path = urlsplit(post_url).path
        post = service.posts().getByPath(blogId=blog_id, path=path).execute()
        post_id = post["id"]
        existing = (post.get("searchDescription") or "").strip()
        if existing:
            print(f"   ✅ {label} 이미 있음 ({len(existing)}자) — 건너뜀")
            return "skip"
        service.posts().patch(
            blogId=blog_id,
            postId=post_id,
            body={"searchDescription": meta[:150]},
        ).execute()
        print(f"   ✅ {label} patch 완료 ({len(meta[:150])}자)")
        return "ok"
    except Exception as e:
        print(f"   ❌ {label} 실패: {e}")
        return "fail"


def process_profile(blog_id: str, blog_info: dict):
    token_file = Path(blog_info["token_file"])
    if not token_file.exists():
        print(f"  ⚠️  토큰 없음: {token_file}")
        return

    profile_dir = PROFILES_DIR / blog_id
    sched_file  = profile_dir / "schedule.json"
    gen_dir     = profile_dir / "generated"

    if not sched_file.exists():
        print(f"  ⚠️  schedule.json 없음: {sched_file}")
        return

    data = json.loads(sched_file.read_text(encoding="utf-8"))
    entries = data.get("entries", {})
    published = [(d, e) for d, e in entries.items() if e.get("status") == "published"]

    print(f"\n블로그: {blog_info.get('name','?')} ({len(published)}개 발행)")

    try:
        service = get_service(token_file)
    except Exception as e:
        print(f"  ❌ 인증 실패: {e}")
        return

    ok = skip = fail = no_meta = 0

    for date_str, entry in sorted(published):
        en_url = entry.get("en_url", "")
        ko_url = entry.get("ko_url", "")
        cfg_file = gen_dir / date_str / "config.json"

        if not cfg_file.exists():
            print(f"  [{date_str}] config.json 없음 — 건너뜀")
            no_meta += 1
            continue

        cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
        en_meta = (cfg.get("en_meta") or "").strip()
        ko_meta = (cfg.get("ko_meta") or "").strip()

        if not en_meta and not ko_meta:
            print(f"  [{date_str}] meta 없음 — 건너뜀")
            no_meta += 1
            continue

        print(f"\n  [{date_str}]")

        if en_meta and en_url:
            r = patch_desc(service, blog_id, en_url, en_meta, "🇺🇸 영문")
            if r == "ok": ok += 1
            elif r == "skip": skip += 1
            else: fail += 1
            time.sleep(0.5)

        if ko_meta and ko_url:
            r = patch_desc(service, blog_id, ko_url, ko_meta, "🇰🇷 한국어")
            if r == "ok": ok += 1
            elif r == "skip": skip += 1
            else: fail += 1
            time.sleep(0.5)

    print(f"\n  완료: patch {ok}건 / 이미있음 {skip}건 / meta없음 {no_meta}건 / 실패 {fail}건")


def main():
    if not BLOGS_FILE.exists():
        print("❌ blogs.json 없음")
        sys.exit(1)

    blogs = json.loads(BLOGS_FILE.read_text(encoding="utf-8")).get("blogs", {})

    print("=" * 60)
    print("발행된 글 검색 설명 일괄 patch")
    print("=" * 60)

    for blog_id, info in blogs.items():
        process_profile(blog_id, info)

    print("\n" + "=" * 60)
    print("전체 완료")
    print("=" * 60)


if __name__ == "__main__":
    main()
