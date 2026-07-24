# -*- coding: utf-8 -*-
"""
photo_vision.py — 로컬 비전 모델(Ollama VLM)로 사진을 보고 자동 태그·캡션 생성.

기본 모델: Qwen2.5-VL 7B (`qwen2.5vl:7b`) — 한국어 태그 잘 나오고 빠릅니다.
LLaVA도 호환되니 settings.vision_model 로 바꿔 쓸 수 있어요.

핵심
  is_available(settings)              비전 모델 설치돼 있는지(Ollama API show)
  caption(path, settings)             사진 1장 → {caption_ko, tags_ko, tags_en}
  auto_tag(limit, settings)           라이브러리에서 태그 미입력 사진 일괄 자동 태깅
"""

import base64
import json
import time
import urllib.request
import urllib.error
from pathlib import Path

import photo_library as pl


DEFAULT_MODEL = "qwen2.5vl:7b"


def _ollama(settings: dict) -> str:
    return (settings.get("ollama_url") or "http://localhost:11434").rstrip("/")


def _model(settings: dict) -> str:
    return (settings.get("vision_model") or DEFAULT_MODEL).strip()


def is_available(settings: dict) -> bool:
    """비전 모델이 실제로 설치돼 있는지 확인(/api/show)."""
    try:
        req = urllib.request.Request(
            _ollama(settings) + "/api/show",
            data=json.dumps({"name": _model(settings)}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            r.read()
        return True
    except Exception:
        return False


def _preview_bytes(path: str, max_size: int = 1024) -> bytes:
    """비전 모델 입력용으로 축소된 JPEG 바이트(원본 그대로 보내면 느림·메모리).
    PIL이 없으면 원본을 그대로."""
    try:
        from PIL import Image, ImageOps
        import io
        im = Image.open(path); im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((max_size, max_size))
        buf = io.BytesIO(); im.save(buf, format="JPEG", quality=85)
        return buf.getvalue()
    except Exception:
        return Path(path).read_bytes()


_PROMPT_BASE = (
    "이 사진을 보고 블로그용 검색·매칭에 쓸 정보를 한국어 JSON으로만 출력해.\n"
    "다른 설명·코드펜스·꼬리말 금지. 키 외 다른 키 만들지 마.\n"
    '{"caption_ko": "사진 내용 한 문장(20자 내)",'
    ' "tags_ko": ["한국어 태그 5~10개"],'
    ' "tags_en": ["english tags 5~10"]}\n'
    "지침:\n"
    "- 한국 사진이면 한국 맥락(한옥·기와·국립공원·도시 등)을 우선.\n"
    "- 태그는 명사 중심으로(예: '한옥', '기와지붕', '새벽', '안개', '산책로', '카페').\n"
    "- 사진에서 보이는 것만, 추측 금지. 인물 정보·인종·감정 추측 금지."
)


def _build_prompt(hints: dict) -> str:
    """폴더명·지명·파일명 힌트가 있으면 프롬프트에 추가(있는 그대로 반영하도록)."""
    parts = []
    for k in ("folder_tags", "place", "region", "filename"):
        v = (hints or {}).get(k) or ""
        v = str(v).strip()
        if v:
            parts.append(f"  - {k}: {v}")
    if not parts:
        return _PROMPT_BASE
    return (_PROMPT_BASE
            + "\n\n[참고 정보 — 운영자가 폴더·지명에 적은 단어들. 사진 내용과 부합하면 "
              "태그에 그대로 포함시킬 것(특히 지명·장소명).]\n" + "\n".join(parts))


def caption(path: str, settings: dict, timeout: int = 120, log=print,
            hints: dict = None) -> dict:
    """사진 1장 → {caption_ko, tags_ko, tags_en}. hints={folder_tags, place, region, filename}.
    실패 시 빈 dict."""
    if not Path(path).exists():
        return {}
    b64 = base64.b64encode(_preview_bytes(path)).decode("ascii")
    payload = {
        "model": _model(settings),
        "messages": [{"role": "user", "content": _build_prompt(hints), "images": [b64]}],
        "stream": False,
        "options": {"temperature": 0.2, "num_ctx": 4096},
        "keep_alive": "10m",
    }
    try:
        req = urllib.request.Request(
            _ollama(settings) + "/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        log(f"      ⚠️ Ollama 오류 {e.code}: {e.read()[:120]}")
        return {}
    except Exception as e:
        log(f"      ⚠️ 비전 호출 실패: {e}")
        return {}
    content = (out.get("message", {}).get("content") or "").strip()
    # JSON만 추출
    s = content.find("{"); e = content.rfind("}")
    if s == -1 or e == -1:
        return {}
    try:
        d = json.loads(content[s:e + 1])
    except Exception:
        return {}
    return {
        "caption_ko": (d.get("caption_ko") or "").strip(),
        "tags_ko": [t.strip() for t in (d.get("tags_ko") or []) if str(t).strip()],
        "tags_en": [t.strip() for t in (d.get("tags_en") or []) if str(t).strip()],
    }


_OCR_PROMPT = ("이 사진에 적혀 있거나 새겨진 글자(한글 텍스트)를 최대한 그대로, 문장 순서대로 "
               "읽어서 적어주세요. 설명판·비석·안내문·현판의 문장을 빠짐없이. 글자가 전혀 없거나 "
               "읽을 수 없으면 정확히 '(없음)'이라고만 답하세요. 읽은 텍스트만 출력하고 다른 "
               "설명·해석은 붙이지 마세요.")


def ocr(path: str, settings: dict, timeout: int = 180, log=print) -> str:
    """사진 속 텍스트(설명판·비석·안내문 등)를 판독해 반환. 글자가 없거나 실패하면 빈 문자열.
    OCR은 해상도가 중요하므로 캡션(1024)보다 큰 1600px로 보낸다."""
    if not Path(path).exists():
        return ""
    b64 = base64.b64encode(_preview_bytes(path, max_size=1600)).decode("ascii")
    payload = {
        "model": _model(settings),
        "messages": [{"role": "user", "content": _OCR_PROMPT, "images": [b64]}],
        "stream": False,
        "options": {"temperature": 0.1, "num_ctx": 4096},
        "keep_alive": "10m",
    }
    try:
        req = urllib.request.Request(
            _ollama(settings) + "/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        log(f"      ⚠️ OCR 호출 실패: {e}")
        return ""
    text = (out.get("message", {}).get("content") or "").strip()
    # 글자 없음/실패 응답 정리
    if len(text) < 15 or text.startswith("(없음") or "글자가 없" in text[:30]:
        return ""
    return text


def auto_tag(settings: dict, limit: int = 50, only_untagged: bool = True,
             log=print, on_progress=None, path=None) -> dict:
    """라이브러리에서 (기본) 태그가 비어 있는 사진을 골라 비전 모델로 일괄 태깅.
    auto_caption은 따로 저장, user_tags는 비어 있으면 채움. {updated, failed, total}."""
    if not is_available(settings):
        raise RuntimeError(
            f"비전 모델 '{_model(settings)}'이 Ollama에 없습니다.\n"
            "터미널에서 'ollama pull qwen2.5vl:7b' 를 한 번 실행하세요(6GB).")
    pl.init_db(path)
    from contextlib import closing
    with closing(pl._conn(path)) as c:
        if only_untagged:
            rows = c.execute(
                "SELECT id, path, filename, folder_tags, place, region "
                "FROM photos "
                "WHERE (user_tags IS NULL OR user_tags='') "
                "  AND (auto_caption IS NULL OR auto_caption='') "
                "ORDER BY added_at LIMIT ?", (limit,)).fetchall()
        else:
            rows = c.execute(
                "SELECT id, path, filename, folder_tags, place, region "
                "FROM photos ORDER BY added_at LIMIT ?",
                (limit,)).fetchall()
    log(f"   👁 비전 자동 태깅 — 대상 {len(rows)}장 (모델: {_model(settings)})")
    updated = failed = 0
    for i, r in enumerate(rows, 1):
        t0 = time.time()
        hints = {"folder_tags": r["folder_tags"], "place": r["place"],
                 "region": r["region"], "filename": r["filename"]}
        d = caption(r["path"], settings, log=log, hints=hints)
        dt = time.time() - t0
        if not d.get("tags_ko") and not d.get("tags_en"):
            failed += 1
            log(f"     · {i}/{len(rows)} 실패 ({Path(r['path']).name})")
            # 실패 마커 — 다음 자동 태깅 실행 때 같은 사진을 무한 재시도하지 않도록.
            # user_tags는 비워 두므로 "태깅 안 된 것만" 필터에는 계속 보임(수동 확인 가능).
            with closing(pl._conn(path)) as c, c:
                c.execute("UPDATE photos SET auto_caption=? WHERE id=?",
                          ("(AI 인식 실패 — 태그를 직접 입력해 주세요)", r["id"]))
        else:
            with closing(pl._conn(path)) as c, c:
                row2 = c.execute("SELECT user_tags, filename, folder_tags, place, region "
                                 "FROM photos WHERE id=?", (r["id"],)).fetchone()
                # AI 태그 + 폴더에서 뽑은 단어(지명·관련어) 같이 병합
                folder_words = [w for w in (row2["folder_tags"] or "").split() if len(w) >= 2]
                place_words = [w for w in (row2["place"] or "").split() if len(w) >= 2]
                merged = d.get("tags_ko", []) + d.get("tags_en", []) + folder_words + place_words
                new_tags = pl.merge_tags(row2["user_tags"], merged)
                c.execute("UPDATE photos SET auto_caption=?, user_tags=?, xmp_synced_at=NULL "
                          "WHERE id=?",
                          (d.get("caption_ko", ""), new_tags, r["id"]))
                c.execute("DELETE FROM photos_fts WHERE rowid=?", (r["id"],))
                c.execute("INSERT INTO photos_fts (rowid, filename, folder_tags, place, region, user_tags, auto_caption) "
                          "VALUES (?,?,?,?,?,?,?)",
                          (r["id"], row2["filename"] or "", row2["folder_tags"] or "",
                           row2["place"] or "", row2["region"] or "",
                           new_tags, d.get("caption_ko", "")))
            updated += 1
            log(f"     ✓ {i}/{len(rows)} {dt:4.1f}s  {Path(r['path']).name}  →  "
                f"{d.get('caption_ko','')[:30]}  [{','.join(d.get('tags_ko',[])[:4])}]")
        if on_progress:
            try: on_progress(i, len(rows))
            except Exception: pass
    log(f"   ✅ 자동 태깅 완료 — 성공 {updated} / 실패 {failed}")
    return {"updated": updated, "failed": failed, "total": len(rows)}
