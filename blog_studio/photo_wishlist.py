# -*- coding: utf-8 -*-
"""
photo_wishlist.py — '촬영 위시리스트' 워크플로우 1단계.

한국 문화 블로그는 무료 이미지·관광 공공데이터로 못 찾는 소재가 많다(예: 나무의 나이테,
전통 매듭, 도자 물레). 운영자가 직접 찍어 채우면 완성도가 크게 오른다. 이 모듈은:

  1) 세 블로그의 '발행된 글'을 스캔해 사진 상태를 판단
     - 내 사진(photo_dir/photo_order) 사용  → 이미 완성(위시 제외)
     - 스톡·검색 사진(found_images) 사용     → 직접 촬영으로 교체하면 완성도↑ (위시 대상)
     - 사진 없음                            → 사진 추가 필요 (위시 대상)
  2) 위시 대상 글마다 '실제로 찍을 수 있는 구체적 소재'를 추출(단어 글은 _visual_queries_for_word,
     일반 글은 photo_plan.generate_shot_list 재사용)
  3) 각 소재에 '권장 파일명'을 붙여 위시리스트 항목으로 만든다. 이 이름으로 저장하면 2단계에서
     정확히 매칭된다(자유 이름은 vision으로 보조 매칭).

위시리스트는 blogs 공통 파일(profiles/_wishlist.json)에 저장. 항목 status: needed/shot/done.
"""
import json
import re
from datetime import datetime
from pathlib import Path

import blog_core as core

WISHLIST_FILE = "photo_wishlist.json"

_BLOG_SHORT = {
    "5372668460061236159": "arts",
    "6831897279794687869": "culture",
    "2481798498122385492": "dance",
}


def _wishlist_path() -> Path:
    return core.PROFILES_DIR / WISHLIST_FILE


def load_wishlist() -> list:
    f = _wishlist_path()
    if not f.exists():
        return []
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_wishlist(items: list) -> None:
    _wishlist_path().write_text(
        json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def _safe_filename(*parts) -> str:
    """권장 파일명 — 한글 유지, 경로에 안전하지 않은 문자만 제거."""
    s = "_".join(p for p in parts if p)
    s = re.sub(r'[\\/:*?"<>|]+', "", s)
    s = re.sub(r"\s+", "", s).strip("_")
    return (s[:60] or "photo") + ".jpg"


def _photo_status(cfg: dict) -> str:
    """글의 사진 상태: 'mine'(내 사진) / 'stock'(스톡·검색) / 'none'(없음)."""
    if not cfg:
        return "none"
    if cfg.get("photo_order") or (cfg.get("photo_dir") or "").strip():
        return "mine"
    if cfg.get("found_images"):
        return "stock"
    return "none"


# 사진으로 찍을 수 없는 '글 구성 요소'(맺음말·FAQ·전체대표컷·여행팁 등) — 폴백 필터용
_META_HEADINGS = (
    "맺음말", "관련 글", "관련글", "자주 묻는", "faq", "여행 꿀팁", "실용 정보",
    "전체 주제", "히어로", "hero", "들어가는 말", "마치며", "요약", "핵심 정리",
    "추천 콘텐츠", "추천 글", "다음 편", "다음 여정", "다음 이야기", "예고",
    "closing", "q&a", "참고 자료", "출처", "방문 가이드",
)


def _is_photographable(heading: str) -> bool:
    """찍을 수 있는 구체 소재인지 — 글 구성 메타 섹션이면 False."""
    hl = (heading or "").strip().lower()
    if not hl:
        return False
    return not any(k in hl for k in _META_HEADINGS)


def _subjects_for_post(cfg: dict, settings: dict, log=print) -> list:
    """이 글에 '무료 스톡으로는 못 찾는, 직접 찍을 구체 실물 소재' → [{slot, heading, guide, search_en}].
    본문 기반 추출(단어·일반 글 공통). 본문이 없을 때만 촬영목록으로 폴백(메타 섹션은 제외)."""
    subs = core.wishlist_subjects(cfg, settings, log)
    if subs:
        return [{"slot": "🇰🇷소재" if s.get("korea") else "소재",
                 "heading": s.get("ko", ""), "guide": s.get("guide", ""),
                 "search_en": s.get("search_en", "")} for s in subs]
    # 폴백: 본문 캐시가 없는 옛 글 — 촬영목록에서 메타 섹션 제외하고 사용
    try:
        import photo_plan as pplan
        shots = pplan.generate_shot_list(cfg, settings, log)
        return [{"slot": s.get("slot", ""), "heading": s.get("heading", ""),
                 "guide": s.get("description_ko", ""), "search_en": s.get("search_en", "")}
                for s in shots if _is_photographable(s.get("heading", ""))]
    except Exception as e:
        log(f"      ⚠️ 촬영 소재 생성 실패: {e}")
        return []


def build_wishlist(blog_ids=None, include_stock=True, include_none=True,
                   settings=None, log=print, on_progress=None) -> list:
    """세 블로그(기본) 발행글을 스캔해 위시리스트를 만든다. 이미 위시에 있는 (url, heading)은
    유지(중복·상태 보존)하고 새 항목만 추가. 반환: 전체 위시리스트."""
    blog_ids = blog_ids or list(_BLOG_SHORT.keys())
    existing = load_wishlist()
    seen_keys = {(it["url"], it["heading"]) for it in existing}
    done_urls = {it["url"] for it in existing}   # 이미 처리한 글 → 이어하기 시 건너뜀
    out = list(existing)
    on_progress = on_progress or (lambda *a, **k: None)

    # 대상 글 먼저 모으기(진행률 계산용)
    targets = []
    for bid in blog_ids:
        try:
            core.set_active_blog(bid, persist=False)
        except Exception:
            continue
        data = core.load_schedule()
        s = settings or data["settings"]
        for key, e in data["entries"].items():
            if e.get("status") != core.ST_PUBLISHED:
                continue
            url = (e.get("ko_url") or e.get("en_url") or "").strip()
            if not url:
                continue
            cfg = core.load_generated(core.post_date(key))
            st = _photo_status(cfg)
            if st == "mine":
                continue                      # 이미 내 사진 → 완성
            if st == "stock" and not include_stock:
                continue
            if st == "none" and not include_none:
                continue
            targets.append((bid, key, e, cfg, st, url, s))

    total = max(len(targets), 1)
    for idx, (bid, key, e, cfg, st, url, s) in enumerate(targets):
        topic = (cfg or {}).get("ko_title") or e.get("topic", "") or key
        on_progress(100.0 * idx / total, f"위시 분석 ({idx + 1}/{len(targets)}): {topic[:24]}")
        if url in done_urls:
            continue                          # 이전 실행에서 이미 처리한 글(이어하기)
        done_urls.add(url)
        if cfg is None:
            # 생성 캐시가 없으면 소재 추출 불가 — 글 단위 1항목만(대표 사진 필요)
            subs = [{"slot": "대표", "heading": e.get("topic", "") or key,
                     "guide": "이 글을 대표하는 사진", "search_en": ""}]
        else:
            subs = _subjects_for_post(cfg, s, log) or \
                [{"slot": "대표", "heading": (cfg.get("ko_title") or topic),
                  "guide": "이 글을 대표하는 사진", "search_en": ""}]
        short = _BLOG_SHORT.get(bid, "blog")
        for si, sub in enumerate(subs):
            heading = (sub.get("heading") or "").strip()
            if not heading or not _is_photographable(heading):
                continue
            if (url, heading) in seen_keys:
                continue
            seen_keys.add((url, heading))
            out.append({
                "blog": short, "blog_id": bid, "date": core.post_date(key),
                "url": url, "topic": topic, "photo_status": st,
                "slot": sub.get("slot", ""), "heading": heading,
                "guide": sub.get("guide", ""), "search_en": sub.get("search_en", ""),
                "recommended_filename": _safe_filename(short, topic[:20], heading[:20]),
                "status": "needed",            # needed → shot(찍음) → done(반영됨)
                "added": datetime.now().strftime("%Y-%m-%d"),
                "matched_photo": "",
            })
        save_wishlist(out)                    # 글마다 저장 — 중단돼도 진행분 보존(이어하기)
    on_progress(100.0, "위시리스트 완성")
    save_wishlist(out)
    log(f"   📝 위시리스트 {len(out)}개 항목(신규 {len(out) - len(existing)}개)")
    return out


def wishlist_summary(items=None) -> dict:
    items = items if items is not None else load_wishlist()
    return {
        "total": len(items),
        "needed": sum(1 for i in items if i.get("status") == "needed"),
        "shot": sum(1 for i in items if i.get("status") == "shot"),
        "done": sum(1 for i in items if i.get("status") == "done"),
        "blogs": sorted({i.get("blog", "") for i in items}),
    }


def export_wishlist_markdown(items=None) -> str:
    items = items if items is not None else load_wishlist()
    items = [i for i in items if i.get("status") == "needed"]
    lines = ["# 📸 촬영 위시리스트", "",
             "직접 찍어 채우면 좋은 사진 목록입니다. **권장 파일명**으로 저장하면 자동 매칭됩니다.", ""]
    by_url = {}
    for it in items:
        by_url.setdefault((it["blog"], it["topic"], it["url"]), []).append(it)
    for (blog, topic, url), subs in by_url.items():
        lines.append(f"## [{blog}] {topic}")
        lines.append(f"<{url}>")
        lines.append("")
        lines.append("| 소재 | 촬영 가이드 | 권장 파일명 |")
        lines.append("|------|-------------|-------------|")
        for s in subs:
            g = (s.get("guide") or "").replace("|", "/")
            lines.append(f"| {s['heading'].replace('|','/')} | {g} | `{s['recommended_filename']}` |")
        lines.append("")
    return "\n".join(lines)
