# -*- coding: utf-8 -*-
"""
photo_intake.py — '촬영 위시리스트' 워크플로우 2단계: 반입 사진 → 위시 항목 매칭.

운영자가 위시리스트를 보고 사진을 찍어 '반입 폴더'(기본 D:\\Source\\한국사진\\_촬영반입)에
저장하면, 이 모듈이 새 사진을 스캔해 어떤 위시 항목(=어떤 글의 어떤 소재)에 해당하는지
매칭한다. 블로그는 건드리지 않고 '미리보기 목록'만 만든다(실제 반영은 GUI 확인 후 3c).

매칭 우선순위:
  1) 파일명이 위시 항목의 '권장 파일명'과 (정규화 후) 일치           → 'exact'
  2) 파일명 토큰이 블로그+주제+소재와 충분히 겹침                    → 'filename'
  3) 위 둘 다 실패하면 vision 캡션·태그를 위시 소재와 비교(느림)      → 'vision'
  4) 아무 데도 안 맞음                                              → None(미매칭)

이미 반영된(status=done) 위시 항목과 이미 라이브러리로 옮겨진 사진은 대상에서 빠진다.
"""
import re
import sys
import shutil
from pathlib import Path

import blog_core as core
import photo_wishlist as wishlist

# 반입 폴더 기본값 — settings['intake_dir']로 언제든 덮어쓸 수 있음(둘 다 그냥 기본값이라
# 없어도 안전하게 무시됨). 윈도우는 기존 그대로, 맥은 홈 폴더 기준(2026-07-24 맥 이식 대응).
DEFAULT_INTAKE_DIR = (r"D:\Source\한국사진\_촬영반입" if sys.platform == "win32"
                      else str(Path.home() / "한국사진" / "_촬영반입"))
APPLIED_SUBDIR = "_반영완료"          # 반영 끝난 사진을 옮겨 둘 하위 폴더(재스캔 방지)
_IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".tif", ".tiff"}


def intake_dir(settings: dict = None) -> str:
    return ((settings or {}).get("intake_dir") or "").strip() or DEFAULT_INTAKE_DIR


def _norm(s: str) -> str:
    """비교용 정규화 — 소문자, 한글·영숫자만 남기고 나머지(공백·기호·확장자 잔재) 제거."""
    s = (s or "").lower()
    return re.sub(r"[^0-9a-z가-힣]+", "", s)


def _stem_no_seq(name: str) -> str:
    """확장자 + 앞뒤 일련번호 제거한 파일 이름."""
    stem = Path(name).stem
    stem = re.sub(r"^[\s_\-0-9]+", "", stem)      # 앞쪽 번호
    stem = re.sub(r"[\s_\-]*\d+$", "", stem)      # 뒤쪽 번호(IMG_1234 등)
    return stem.strip()


def _tokens(s: str) -> list:
    """한글 2글자+ / 영문 3글자+ 토큰."""
    return [t for t in re.findall(r"[가-힣]{2,}|[a-z]{3,}", (s or "").lower())]


def list_new_photos(settings: dict = None) -> list:
    """반입 폴더의 사진 파일 경로 목록(반영완료 하위 폴더는 제외, 재귀)."""
    root = Path(intake_dir(settings))
    if not root.exists():
        return []
    out = []
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in _IMG_EXT and APPLIED_SUBDIR not in p.parts:
            out.append(str(p))
    return sorted(out)


def _match_by_filename(photo_name: str, items: list):
    """(항목, confidence, 이유) 또는 (None, None, None). exact > filename."""
    pn_norm = _norm(_stem_no_seq(photo_name))
    if not pn_norm:
        return None, None, None
    # 1) 권장 파일명과 정규화 일치(또는 한쪽이 다른 쪽을 포함)
    for it in items:
        rec = _norm(Path(it.get("recommended_filename", "")).stem)
        if rec and (rec == pn_norm or rec in pn_norm or pn_norm in rec):
            return it, "exact", f"파일명이 권장 파일명과 일치({it['recommended_filename']})"
    # 2) 토큰 겹침 — 블로그 short + 주제 + 소재 토큰과 파일명 토큰
    pn_toks = set(_tokens(_stem_no_seq(photo_name)))
    if not pn_toks:
        return None, None, None
    best, best_score = None, 0
    for it in items:
        cand = set(_tokens(f"{it.get('blog','')} {it.get('topic','')} {it.get('heading','')}"))
        overlap = len(pn_toks & cand)
        if overlap > best_score:
            best, best_score = it, overlap
    if best is not None and best_score >= 2:
        return best, "filename", f"파일명 키워드 {best_score}개 일치"
    return None, None, None


def _match_by_vision(cap: dict, items: list):
    """vision 캡션·태그와 위시 소재 heading/search_en 토큰 비교."""
    text = (cap.get("caption_ko", "") + " " + " ".join(cap.get("tags_ko", []))
            + " " + " ".join(cap.get("tags_en", [])))
    cap_toks = set(_tokens(text))
    if not cap_toks:
        return None, None, None
    best, best_score = None, 0
    for it in items:
        cand = set(_tokens(f"{it.get('heading','')} {it.get('search_en','')}"))
        overlap = len(cap_toks & cand)
        if overlap > best_score:
            best, best_score = it, overlap
    if best is not None and best_score >= 2:
        return best, "vision", f"사진 내용이 소재와 {best_score}개 일치"
    return None, None, None


def scan_intake(settings: dict, wishlist_items: list = None, use_vision: bool = True,
                log=print, on_progress=None) -> list:
    """반입 폴더 사진을 위시 항목과 매칭. 블로그·파일 변경 없음(미리보기 데이터만).
    반환: [{photo_path, photo_name, item, blog, topic, heading, recommended_filename,
            confidence, reason, caption}]  — confidence 없으면 미매칭."""
    on_progress = on_progress or (lambda *a, **k: None)
    items = wishlist_items if wishlist_items is not None else wishlist.load_wishlist()
    items = [it for it in items if it.get("status") == "needed"]
    photos = list_new_photos(settings)
    total = max(len(photos), 1)
    results = []
    for i, path in enumerate(photos):
        name = Path(path).name
        on_progress(100.0 * i / total, f"매칭 ({i + 1}/{len(photos)}): {name}")
        item, conf, reason = _match_by_filename(name, items)
        cap = {}
        if item is None and use_vision:
            try:
                import photo_vision
                cap = photo_vision.caption(path, settings, log=log,
                                           hints={"filename": name})
            except Exception as e:
                log(f"      ⚠️ 비전 매칭 실패({name}): {e}")
                cap = {}
            if cap:
                item, conf, reason = _match_by_vision(cap, items)
        results.append({
            "photo_path": path, "photo_name": name,
            "item": item,
            "blog": (item or {}).get("blog", ""),
            "topic": (item or {}).get("topic", ""),
            "heading": (item or {}).get("heading", ""),
            "recommended_filename": (item or {}).get("recommended_filename", ""),
            "url": (item or {}).get("url", ""),
            "confidence": conf, "reason": reason or "매칭되는 위시 항목 없음",
            "caption": cap.get("caption_ko", ""),
        })
    on_progress(100.0, "매칭 완료")
    matched = sum(1 for r in results if r["confidence"])
    log(f"   🔗 반입 {len(results)}장 중 {matched}장 매칭")
    return results


# ── 2c: 확인된 매칭을 실제 블로그 글에 반영 ────────────────────────────────
def applied_root(settings: dict = None) -> Path:
    """반영된 사진을 글별로 모아 두는 라이브러리 루트(기본: 반입 폴더 옆 _블로그반영)."""
    d = ((settings or {}).get("applied_dir") or "").strip()
    if d:
        return Path(d)
    return Path(intake_dir(settings)).parent / "_블로그반영"


def _post_folder(settings: dict, item: dict) -> Path:
    """한 글의 사용자 사진을 모아 두는 폴더(글마다 하나) — 누적 재배치의 근거."""
    slug = wishlist._safe_filename(item.get("blog", ""), item.get("topic", "")[:24])
    slug = Path(slug).stem
    p = applied_root(settings) / slug
    p.mkdir(parents=True, exist_ok=True)
    return p


def _find_date_for_url(data: dict, url: str) -> str:
    for key, e in data["entries"].items():
        if url and (e.get("ko_url") == url or e.get("en_url") == url):
            return core.post_date(key)
    return ""


def apply_matches(matches: list, settings: dict, log=print, on_progress=None) -> dict:
    """확인된 매칭(각 {photo_path, item}) 을 실제 글에 반영.
    글별로: ① 새 사진을 그 글의 라이브러리 폴더로 복사(권장 파일명으로) →
    ② add_photos_to_published로 그 글의 사용자 사진 '전체'를 같은 URL에 재배치(patch) →
    ③ 위시 항목 status=done·matched_photo 기록 → ④ 원본을 _반영완료로 이동 + 라이브러리 등록.
    반환: {'posts': n, 'photos': n, 'errors': [(name, reason)]}."""
    on_progress = on_progress or (lambda *a, **k: None)
    import photo_library as photolib

    # 글(blog_id, url) 기준으로 그룹
    groups = {}
    for m in matches:
        it = m.get("item")
        if not it:
            continue
        key = (it.get("blog_id", ""), it.get("url", ""))
        groups.setdefault(key, []).append(m)

    posts_done, photos_done, errors = 0, 0, []
    total = max(len(groups), 1)
    for gi, ((blog_id, url), ms) in enumerate(groups.items()):
        item0 = ms[0]["item"]
        topic = item0.get("topic", "")
        on_progress(100.0 * gi / total, f"반영 ({gi + 1}/{len(groups)}): {topic[:20]}")
        try:
            core.set_active_blog(blog_id, persist=False)
            data = core.load_schedule()
            date_str = item0.get("date") or _find_date_for_url(data, url)
            if not date_str or date_str not in data["entries"]:
                errors.append((topic[:24], "발행 글의 날짜를 찾지 못함"))
                continue

            # ① 새 사진을 글 폴더로 복사(권장 파일명 사용 → 배치·캡션 안정)
            folder = _post_folder(settings, item0)
            for m in ms:
                src = Path(m["photo_path"])
                if not src.exists():
                    continue
                dst = folder / (m["item"].get("recommended_filename") or src.name)
                if not dst.exists():
                    shutil.copy2(src, dst)

            # ② 그 글의 사용자 사진 전체를 같은 URL에 재배치(patch)
            core.add_photos_to_published(date_str, str(folder), data, settings, log=log)
            posts_done += 1

            # ③ 위시 항목 done 표시 + ④ 원본 이동·등록
            wl_items = wishlist.load_wishlist()
            by_key = {(it.get("url"), it.get("heading")): it for it in wl_items}
            for m in ms:
                it = m["item"]
                tgt = by_key.get((it.get("url"), it.get("heading")))
                if tgt:
                    tgt["status"] = "done"
                    tgt["matched_photo"] = m["photo_name"]
                photos_done += 1
                _archive_original(m["photo_path"], settings, photolib, log)
            wishlist.save_wishlist(wl_items)
        except Exception as e:
            errors.append((topic[:24], str(e)))
            log(f"   ❌ 반영 실패({topic[:20]}): {e}")

    on_progress(100.0, "반영 완료")
    log(f"   ✅ 반영: 글 {posts_done}개 · 사진 {photos_done}장"
        + (f" · 실패 {len(errors)}" if errors else ""))
    return {"posts": posts_done, "photos": photos_done, "errors": errors}


def _archive_original(photo_path: str, settings: dict, photolib, log=print) -> None:
    """반영 끝난 원본을 반입 폴더의 _반영완료로 옮기고 라이브러리 DB에 등록(재스캔 방지)."""
    src = Path(photo_path)
    if not src.exists():
        return
    done_dir = Path(intake_dir(settings)) / APPLIED_SUBDIR
    done_dir.mkdir(parents=True, exist_ok=True)
    dst = done_dir / src.name
    if dst.exists():
        dst = done_dir / f"{src.stem}_{int(src.stat().st_mtime)}{src.suffix}"
    try:
        shutil.move(str(src), str(dst))
    except Exception as e:
        log(f"      ⚠️ 원본 이동 실패({src.name}): {e}")
        return
    try:
        photolib.register_file(str(dst))
    except Exception:
        pass
