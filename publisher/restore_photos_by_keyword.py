"""
restore_photos_by_keyword.py — 제목에 특정 키워드가 포함된 포스트만 정확히 찾아
지정한 폴더의 사진으로 안전하게 업데이트합니다.

날짜 범위 검색 대신 "제목 키워드 포함 여부"로 매칭하므로,
같은 시기에 발행된 다른 포스트를 잘못 건드릴 위험이 없습니다.

사용법:
  python restore_photos_by_keyword.py "황토,맨발,Hwangto,barefoot" "2026/06/20260606/태화강황토맨발길"

  - 첫 번째 인자: 쉼표로 구분된 제목 키워드 목록 (하나라도 포함되면 매칭)
  - 두 번째 인자: BLOGGER_ROOT(C:\\blogger) 기준 사진 폴더 상대경로
"""
import sys, os
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.environ.setdefault("PYTHONUTF8", "1")

import re, time
from pathlib import Path

SCRIPT_DIR   = Path(__file__).resolve().parent
BLOGGER_ROOT = SCRIPT_DIR.parent
TOKEN_FILE   = SCRIPT_DIR / "token.json"
BLOG_ID_FILE = SCRIPT_DIR / "blog_id.txt"

# update_post.py에 정의된 함수들을 재사용합니다.
sys.path.insert(0, str(SCRIPT_DIR))
from update_post import (
    get_credentials, get_blog_id, photo_to_data_uri, find_photos,
    has_hangul, strip_images, make_img_tag, list_recent_posts,
)


def find_posts_by_title_keywords(service, blog_id, keywords, max_results=50):
    """제목에 keywords 중 하나라도 포함된 포스트만 찾습니다 (대소문자 무시)."""
    posts = list_recent_posts(service, blog_id, max_results=max_results)
    matches = []
    for p in posts:
        title = p.get("title", "")
        if any(kw.lower() in title.lower() for kw in keywords):
            full = service.posts().get(
                blogId=blog_id, postId=p["id"],
                fields="id,title,content,labels,url"
            ).execute()
            matches.append(full)
    return matches


def main():
    if len(sys.argv) < 3:
        sys.exit("사용법: python restore_photos_by_keyword.py \"키워드1,키워드2\" \"사진폴더상대경로\"")

    keywords = [k.strip() for k in sys.argv[1].split(",") if k.strip()]
    folder   = BLOGGER_ROOT / sys.argv[2]

    print("\n" + "="*60)
    print("  키워드 기반 포스트 사진 복구")
    print("="*60)
    print(f"🔑 검색 키워드: {keywords}")
    print(f"📁 사진 폴더: {folder}")

    if not folder.exists():
        sys.exit(f"❌ 폴더를 찾을 수 없습니다: {folder}")
    photos = find_photos(folder)
    if not photos:
        sys.exit(f"❌ {folder} 에 사진이 없습니다.")
    print(f"🖼  사진 {len(photos)}장 발견")

    creds   = get_credentials()
    from googleapiclient.discovery import build
    service = build("blogger", "v3", credentials=creds)
    blog_id, blog_url = get_blog_id(service)
    print(f"📝 블로그: {blog_url}")

    matches = find_posts_by_title_keywords(service, blog_id, keywords)
    if not matches:
        sys.exit("❌ 제목에 키워드가 포함된 포스트를 찾지 못했습니다.")

    print(f"\n📌 매칭된 포스트 {len(matches)}개:")
    for p in matches:
        print(f"  - {p.get('title','')}  ({p.get('url','')})")

    confirm = input("\n위 포스트(들)에 사진을 적용할까요? (y/n): ").strip().lower()
    if confirm != "y":
        print("취소되었습니다.")
        return

    for post in matches:
        pid   = post["id"]
        title = post["title"]
        labels = post.get("labels", [])
        html  = post.get("content", "")
        is_ko = has_hangul(title)
        topic = title

        print(f"\n{'─'*50}")
        print(f"📝 업데이트: {title}  (언어: {'한국어' if is_ko else '영어'})")

        html = strip_images(html)

        gallery = '\n<hr>\n<h2>📷 Photos</h2>\n'
        for i, p in enumerate(photos):
            alt = f"{topic} 사진 {i+1}" if is_ko else f"{topic} — photo {i+1}"
            print(f"   🖼  ({i+1}/{len(photos)}) {p.name} → alt: {alt[:50]}", flush=True)
            gallery += make_img_tag(photo_to_data_uri(p), alt) + '\n'
        html += gallery

        service.posts().update(
            blogId=blog_id, postId=pid,
            body={"title": title, "content": html, "labels": labels},
        ).execute()
        print("   ✅ 업데이트 완료")
        time.sleep(1)

    print("\n" + "="*60)
    print("✅ 모든 포스트 업데이트 완료!")
    print("="*60 + "\n")


if __name__ == "__main__":
    main()
