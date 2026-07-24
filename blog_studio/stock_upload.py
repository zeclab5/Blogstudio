# -*- coding: utf-8 -*-
"""
stock_upload.py — '촬영 위시리스트' 워크플로우 3단계: 촬영본을 스톡 사이트에 업로드.

반입·반영(2단계)까지 끝난 촬영 사진을, 등록된 스톡 사이트(크라우드픽·Unsplash·Pexels)에
올리기 위한 준비를 한다. 이 모듈이 하는 일:

  1) 사진마다 판매용 메타데이터(영문 제목·설명·키워드 20~30개, 국문 키워드)를 생성
     - 비전(사진 내용) + 위시 항목(무슨 소재였는지)을 근거로 LLM이 작성
  2) 그 키워드를 사진 파일에 XMP/IPTC로 써 넣음(사이트가 자동으로 읽어가기도 함)
  3) 업로드 큐(profiles/stock_queue.json)에 쌓고 사이트별 상태를 관리
     - status: pending → uploaded / skipped / failed (사이트별)

실제 업로드(브라우저 자동화)는 stock_sites.py(3b)가 담당한다. 외부 사이트에 '공개 게시'하는
행위이므로, 자동 입력까지만 하고 최종 '제출'은 사용자가 확인하는 것을 기본으로 한다.
"""
import json
import re
from datetime import datetime
from pathlib import Path

import blog_core as core

QUEUE_FILE = "stock_queue.json"
SITES = ["크라우드픽", "Unsplash", "Pexels"]      # 업로드 순서


def _queue_path() -> Path:
    return core.PROFILES_DIR / QUEUE_FILE


def load_queue() -> list:
    f = _queue_path()
    if not f.exists():
        return []
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_queue(items: list) -> None:
    _queue_path().write_text(
        json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


# ── 메타데이터 생성 ──────────────────────────────────────────────────────
_META_SYSTEM = ("당신은 스톡 사진 판매 전문가입니다. 사진을 잘 팔리게 하는 영어 제목·설명·"
                "키워드와 한국어 키워드를 지정한 JSON 하나로만 출력하세요.")


def _meta_prompt(subject_ko: str, search_en: str, vision_caption: str,
                 vision_tags) -> str:
    tags = ", ".join(vision_tags or [])
    return f'''촬영한 사진 1장을 스톡 사이트(크라우드픽·Unsplash·Pexels)에 올립니다.
- 사진 소재(한국어): {subject_ko}
- 참고 영어 표현: {search_en}
- 사진 내용(비전 분석): {vision_caption}
- 비전 태그: {tags}

이 사진이 검색으로 잘 팔리도록:
- title_en: 자연스러운 영어 제목(간결, 8단어 이내)
- description_en: 한 문장 영어 설명
- keywords_en: 영어 키워드 20~30개(구체적 사물·질감·색·분위기·용도. 중복·모호어 금지)
- keywords_ko: 한국어 키워드 10~15개(크라우드픽용)
한국 고유 소재면 'Korea/Korean/한국' 관련 키워드를 반드시 포함.

아래 JSON만 출력(설명·코드펜스 없이):
{{"title_en":"...","description_en":"...","keywords_en":["..."],"keywords_ko":["..."]}}'''


def build_metadata(photo_path: str, item: dict, settings: dict, log=print) -> dict:
    """사진 + 위시 항목 → 스톡 판매용 메타데이터.
    반환: {title_en, description_en, keywords_en[], keywords_ko[]} (실패 시 최소 폴백)."""
    subject_ko = (item or {}).get("heading", "") or Path(photo_path).stem
    search_en = (item or {}).get("search_en", "")
    cap, tags = "", []
    try:
        import photo_vision
        v = photo_vision.caption(photo_path, settings, log=log,
                                 hints={"filename": Path(photo_path).name})
        cap = v.get("caption_ko", "")
        tags = (v.get("tags_en") or []) + (v.get("tags_ko") or [])
    except Exception as e:
        log(f"      ⚠️ 비전 분석 생략({Path(photo_path).name}): {e}")

    prompt = _meta_prompt(subject_ko, search_en, cap, tags)
    for attempt in range(2):
        try:
            d = core._extract_json(core._complete(settings, prompt, log, _META_SYSTEM))
            kw_en = _clean_keywords(d.get("keywords_en"))
            kw_ko = _clean_keywords(d.get("keywords_ko"))
            title = (d.get("title_en") or "").strip()
            if title and kw_en:
                return {
                    "title_en": title,
                    "description_en": (d.get("description_en") or "").strip(),
                    "keywords_en": kw_en, "keywords_ko": kw_ko,
                }
        except Exception as e:
            log(f"      ↻ 메타데이터 재시도({attempt + 1}/2): {e}")
    # 폴백 — 비전/위시에서 최소 구성
    return {
        "title_en": (search_en or subject_ko).title(),
        "description_en": cap or subject_ko,
        "keywords_en": _clean_keywords(tags) or _clean_keywords(search_en.split()),
        "keywords_ko": _clean_keywords([subject_ko]),
    }


def _clean_keywords(raw) -> list:
    """리스트/문자열 → 정리된 키워드 리스트(공백·중복·빈값 제거, 순서 유지)."""
    if isinstance(raw, str):
        raw = re.split(r"[,\n;]+", raw)
    out, seen = [], set()
    for k in (raw or []):
        k = str(k).strip().strip("#").strip()
        kl = k.lower()
        if k and kl not in seen:
            seen.add(kl)
            out.append(k)
    return out


# ── 큐 관리 ──────────────────────────────────────────────────────────────
def add_to_queue(photo_path: str, meta: dict, item: dict = None,
                 sites: list = None, write_xmp: bool = True, log=print) -> dict:
    """사진 1장을 업로드 큐에 추가(이미 있으면 메타 갱신). write_xmp면 파일에 키워드 기록.
    반환: 추가/갱신된 큐 항목."""
    sites = sites or list(SITES)
    q = load_queue()
    by_path = {it["photo_path"]: it for it in q}
    entry = by_path.get(photo_path)
    if entry is None:
        entry = {"photo_path": photo_path, "photo_name": Path(photo_path).name,
                 "added": datetime.now().strftime("%Y-%m-%d"),
                 "sites": {}}
        q.append(entry)
    entry["title_en"] = meta.get("title_en", "")
    entry["description_en"] = meta.get("description_en", "")
    entry["keywords_en"] = meta.get("keywords_en", [])
    entry["keywords_ko"] = meta.get("keywords_ko", [])
    entry["blog"] = (item or {}).get("blog", entry.get("blog", ""))
    entry["topic"] = (item or {}).get("topic", entry.get("topic", ""))
    for s in sites:
        entry["sites"].setdefault(s, "pending")

    if write_xmp:
        try:
            import photo_xmp
            kws = list(dict.fromkeys((meta.get("keywords_en") or [])
                                     + (meta.get("keywords_ko") or [])))
            photo_xmp.write_keywords(photo_path, kws,
                                     caption=meta.get("description_en", ""), log=log)
        except Exception as e:
            log(f"      ⚠️ XMP 기록 실패({Path(photo_path).name}): {e}")

    save_queue(q)
    return entry


def set_status(photo_path: str, site: str, status: str) -> None:
    q = load_queue()
    for it in q:
        if it["photo_path"] == photo_path:
            it.setdefault("sites", {})[site] = status
            break
    save_queue(q)


def pending_uploads(site: str = None) -> list:
    """아직 안 올린(pending) 큐 항목. site 지정 시 그 사이트가 pending인 것만."""
    out = []
    for it in load_queue():
        st = it.get("sites", {})
        if site:
            if st.get(site) == "pending":
                out.append(it)
        elif any(v == "pending" for v in st.values()):
            out.append(it)
    return out


def queue_summary() -> dict:
    q = load_queue()
    per_site = {s: {"pending": 0, "uploaded": 0, "failed": 0, "skipped": 0} for s in SITES}
    for it in q:
        for s, v in it.get("sites", {}).items():
            per_site.setdefault(s, {"pending": 0, "uploaded": 0, "failed": 0, "skipped": 0})
            per_site[s][v] = per_site[s].get(v, 0) + 1
    return {"total": len(q), "per_site": per_site}


def enqueue_applied_photos(settings: dict, sites: list = None, write_xmp: bool = True,
                           log=print, on_progress=None) -> int:
    """2단계에서 반영 완료(status=done)된 위시 항목의 사진을 찾아 큐에 넣는다(메타 생성 포함).
    반환: 새로 큐에 넣은 사진 수."""
    import photo_wishlist as wishlist
    import photo_intake as intake
    on_progress = on_progress or (lambda *a, **k: None)
    done = [it for it in wishlist.load_wishlist() if it.get("status") == "done"]
    queued_paths = {it["photo_path"] for it in load_queue()}
    # 반영된 사진은 글 라이브러리 폴더에 권장 파일명으로 저장돼 있음
    added = 0
    total = max(len(done), 1)
    for i, it in enumerate(done):
        on_progress(100.0 * i / total, f"메타 생성 ({i + 1}/{len(done)})")
        folder = intake._post_folder(settings, it)
        photo = folder / (it.get("recommended_filename") or "")
        if not photo.exists() or str(photo) in queued_paths:
            continue
        meta = build_metadata(str(photo), it, settings, log)
        add_to_queue(str(photo), meta, item=it, sites=sites, write_xmp=write_xmp, log=log)
        queued_paths.add(str(photo))
        added += 1
    on_progress(100.0, "메타 생성 완료")
    log(f"   📤 업로드 큐에 {added}장 추가")
    return added
