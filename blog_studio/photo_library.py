# -*- coding: utf-8 -*-
"""
photo_library.py — 내 사진 라이브러리(자동 태깅 + 검색).

폴더를 가리키면 사진을 재귀 스캔해 EXIF(촬영일·GPS·해상도) + 파일명·폴더명 키워드를
SQLite DB에 저장. 글 주제·장소로 매칭해 '내 사진'을 1순위로 추천합니다.

1A 범위(이 파일):
  · DB 스키마 + scan_folder(EXIF·폴더명 자동 추출 + 폴더명 무시 목록)
  · 검색(`search`/`find_for_post`) — 키워드 점수 + 사용 횟수 균형
  · 출처 캡션(`photo_caption`) — 사용자 설정 기본값(© ...)
GUI 연결·역지오코딩·AI 비전은 다음 단계(1B/1C/2/3)에서.
"""

import os
import re
import time
import json
import sqlite3
import urllib.request
import urllib.parse
from contextlib import closing
from datetime import datetime
from pathlib import Path

try:
    import blog_core as _core
    DB_PATH = _core.SCRIPT_DIR / "photo_library.db"
except Exception:
    DB_PATH = Path(__file__).resolve().parent / "photo_library.db"

PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".heic", ".heif"}
# 사진 폴더 트리에서 의미 없는 폴더명(태그로 안 씀). 소문자 비교.
IGNORE_FOLDERS = {
    "raw", "원본", "원본보관", "ok cut", "ok-cut", "okcut",
    "선택", "보정", "수정", "편집", "edited", "selects", "selected", "출력",
    "사진", "photos", "photo", "images", "image",
    "source", "src", "백업", "backup", "임시", "temp", "tmp",
}
# 위 목록은 '오직 그 단어만' 폴더명일 때만 적용. 예: "한국사진"은 무시되지만 "한국사진/경기"
# 안의 "경기"는 유지. "전주 사진" 같은 복합 폴더명은 단어가 같아도 살림(아래 로직).

_SCHEMA = """
CREATE TABLE IF NOT EXISTS photos (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    path          TEXT UNIQUE NOT NULL,
    filename      TEXT,
    folder_tags   TEXT,
    taken_at      TEXT,
    width         INTEGER,
    height        INTEGER,
    gps_lat       REAL,
    gps_lng       REAL,
    place         TEXT,
    region        TEXT,
    user_tags     TEXT,
    auto_caption  TEXT,
    attribution   TEXT,
    added_at      TEXT,
    last_used     TEXT,
    use_count     INTEGER DEFAULT 0,
    xmp_synced_at TEXT       -- 이 시각 이후 user_tags가 사진 파일에 저장됨
);
-- 기존 DB에 컬럼이 없으면 추가(있으면 무시)

CREATE INDEX IF NOT EXISTS idx_photos_place ON photos(place);
CREATE INDEX IF NOT EXISTS idx_photos_taken ON photos(taken_at);
-- FTS5(독립 테이블) — 외부 콘텐츠 모드는 동기화 트리거 누락 시 'malformed' 오류가 나므로 회피.
CREATE VIRTUAL TABLE IF NOT EXISTS photos_fts USING fts5(
    filename, folder_tags, place, region, user_tags, auto_caption,
    tokenize='unicode61'
);
"""


def _conn(path=None):
    c = sqlite3.connect(str(path or DB_PATH))
    c.row_factory = sqlite3.Row
    return c


def init_db(path=None):
    with closing(_conn(path)) as c, c:
        c.executescript(_SCHEMA)
        # 기존 DB에 새 컬럼이 빠져 있으면 추가(이미 있으면 OperationalError 무시)
        try:
            c.execute("ALTER TABLE photos ADD COLUMN xmp_synced_at TEXT")
        except sqlite3.OperationalError:
            pass
    return path or DB_PATH


def _FIELDS_DEFAULT():
    return _FIELDS


# ── EXIF / 파일명 / 폴더명 추출 ────────────────────────────────────────────────
def _exif(path: Path) -> dict:
    """EXIF에서 촬영일·GPS·해상도 추출(없으면 빈 dict)."""
    try:
        from PIL import Image, ExifTags
    except Exception:
        return {}
    try:
        img = Image.open(path)
        w, h = img.size
        raw = img._getexif() or {}
    except Exception:
        return {}
    tags = {ExifTags.TAGS.get(k, k): v for k, v in raw.items()}
    out = {"width": w, "height": h}
    dt = tags.get("DateTimeOriginal") or tags.get("DateTime") or ""
    try:
        out["taken_at"] = datetime.strptime(dt, "%Y:%m:%d %H:%M:%S").isoformat()
    except Exception:
        pass
    gps_raw = tags.get("GPSInfo")
    if gps_raw:
        gps = {ExifTags.GPSTAGS.get(k, k): v for k, v in gps_raw.items()}
        lat, lng = _gps_to_deg(gps)
        if lat is not None:
            out["gps_lat"], out["gps_lng"] = lat, lng
    return out


def _gps_to_deg(gps: dict):
    """EXIF GPS(rational) → 십진수 위·경도."""
    try:
        def to_f(v):
            return float(v[0]) / 60 / 60 + float(v[1]) / 60 + float(v[2])
        lat = to_f(gps["GPSLatitude"])
        lng = to_f(gps["GPSLongitude"])
        if gps.get("GPSLatitudeRef", "N").upper() == "S":
            lat = -lat
        if gps.get("GPSLongitudeRef", "E").upper() == "W":
            lng = -lng
        return lat, lng
    except Exception:
        return None, None


def _split_filename(name: str) -> list:
    """파일명에서 키워드 후보 추출(언더스코어·하이픈·공백·확장자 제거, 숫자만은 제외)."""
    stem = re.sub(r"\.[a-zA-Z0-9]+$", "", name)
    parts = re.split(r"[\s_\-\.]+", stem)
    return [p for p in parts if p and not p.isdigit() and len(p) >= 2]


def _folder_tags(file_path: Path, root: Path) -> list:
    """파일 위치의 상위 폴더명들에서 의미 있는 태그만 골라 반환.
    무시 목록 단어와 '정확히 일치' 하는 단일어 폴더만 제외하고, '전주 사진'처럼
    여러 단어가 섞이면 그 폴더는 통째로 살림(지명·관련어가 들어있을 수 있음).
    ★ '포항' 같은 루트 폴더명도 포함(사용자가 그 지역 폴더를 통째로 선택해 스캔하는
       경우 흔함). 단 의미 없는 'D:\\', '사진' 등은 자동 제외."""
    rel = file_path.relative_to(root) if str(file_path).lower().startswith(str(root).lower()) else file_path
    parts = [root.name] + list(rel.parts[:-1])    # 루트 폴더명도 첫 태그로
    tags = []
    seen = set()
    for p in parts:
        s = p.strip()
        if not s:
            continue
        # 공백·구분자 없는 단일어가 무시 목록에 정확히 일치할 때만 제외
        simple = s.lower().replace("-", "").replace("_", "")
        words = [w for w in s.replace("_", " ").replace("-", " ").split() if w]
        is_multi = len(words) > 1
        if not is_multi and simple in {w.replace(" ", "").replace("-", "") for w in IGNORE_FOLDERS}:
            continue
        # 드라이브 루트(C:, D:, /) 같은 의미 없는 것 제외
        if simple in {"c:", "d:", "e:", "f:", "/", ""}:
            continue
        if s in seen:
            continue
        seen.add(s); tags.append(s)
    return tags


# ── 저장 / 검색 ───────────────────────────────────────────────────────────────
_FIELDS = ("path", "filename", "folder_tags", "taken_at", "width", "height",
           "gps_lat", "gps_lng", "place", "region", "user_tags", "auto_caption",
           "attribution", "added_at", "last_used", "use_count")


def _upsert_in(c: sqlite3.Connection, rec: dict) -> int:
    """주어진 connection 안에서 upsert + FTS 동기화."""
    row = {k: rec.get(k) for k in _FIELDS}
    row.setdefault("added_at", datetime.now().isoformat())
    row["use_count"] = rec.get("use_count") or 0
    existing = c.execute("SELECT id, user_tags, use_count, last_used, place, region "
                         "FROM photos WHERE path=?", (row["path"],)).fetchone()
    if existing:
        row["user_tags"] = row.get("user_tags") or existing["user_tags"]
        row["use_count"] = row.get("use_count") or existing["use_count"]
        row["last_used"] = row.get("last_used") or existing["last_used"]
        row["place"] = row.get("place") or existing["place"]
        row["region"] = row.get("region") or existing["region"]
        sets = ", ".join(f"{k}=?" for k in _FIELDS if k != "path")
        c.execute(f"UPDATE photos SET {sets} WHERE path=?",
                  [row[k] for k in _FIELDS if k != "path"] + [row["path"]])
        pid = existing["id"]
    else:
        cols = ", ".join(_FIELDS); ph = ", ".join("?" for _ in _FIELDS)
        cur = c.execute(f"INSERT INTO photos ({cols}) VALUES ({ph})",
                        [row[k] for k in _FIELDS])
        pid = cur.lastrowid
    # FTS5(독립) 동기화 — rowid=사진 id로 직접 매핑
    c.execute("DELETE FROM photos_fts WHERE rowid=?", (pid,))
    c.execute("INSERT INTO photos_fts (rowid, filename, folder_tags, place, region, user_tags, auto_caption) "
              "VALUES (?,?,?,?,?,?,?)",
              (pid, row.get("filename", "") or "", row.get("folder_tags", "") or "",
               row.get("place", "") or "", row.get("region", "") or "",
               row.get("user_tags", "") or "", row.get("auto_caption", "") or ""))
    return pid


def upsert_photo(rec: dict, path=None) -> int:
    """경로 기준 upsert(외부에서 단일 호출용)."""
    init_db(path)
    with closing(_conn(path)) as c, c:
        return _upsert_in(c, rec)


def scan_folder(root: str, log=print, path=None) -> dict:
    """폴더 재귀 스캔 → 사진 등록. {added, updated, skipped} 반환.
    단일 connection으로 일괄 upsert(파일별 새 연결 X)."""
    root_p = Path(root).expanduser().resolve()
    if not root_p.exists():
        raise FileNotFoundError(root)
    init_db(path)
    added = updated = skipped = 0
    files = [p for p in root_p.rglob("*") if p.suffix.lower() in PHOTO_EXTS]
    log(f"   📚 사진 라이브러리 스캔: {root_p} — 파일 {len(files)}개")
    with closing(_conn(path)) as c, c:
        for i, f in enumerate(files, 1):
            try:
                exists = c.execute("SELECT 1 FROM photos WHERE path=?",
                                   (str(f),)).fetchone()
                ex = _exif(f)
                ftags = _folder_tags(f, root_p)
                kw = _split_filename(f.name)
                rec = {
                    "path": str(f),
                    "filename": " ".join(kw),
                    "folder_tags": " ".join(ftags),
                    "taken_at": ex.get("taken_at"),
                    "width": ex.get("width"), "height": ex.get("height"),
                    "gps_lat": ex.get("gps_lat"), "gps_lng": ex.get("gps_lng"),
                    "place": ftags[-1] if ftags else "",
                    "region": ftags[0] if ftags else "",
                }
                _upsert_in(c, rec)
                if exists: updated += 1
                else: added += 1
                if i % 200 == 0:
                    log(f"     · {i}/{len(files)} 처리 중...")
            except Exception as e:
                skipped += 1
                if skipped <= 3:
                    log(f"     ⚠️ 건너뜀 {f.name}: {e}")
    log(f"   ✅ 스캔 완료 — 신규 {added} / 갱신 {updated} / 실패 {skipped}")
    return {"added": added, "updated": updated, "skipped": skipped}


# ── 검색 / 매칭 ───────────────────────────────────────────────────────────────
_FTS_SAFE = re.compile(r"[\s/\\\.\,\-\!\?\"'\(\)\[\]\{\}:;]+")


def _fts_query(q: str) -> str:
    """FTS5에 안전한 OR 쿼리로 변환(특수문자 분해 + 짧은 토큰 제거)."""
    parts = [p for p in _FTS_SAFE.split(q.strip()) if len(p) >= 2]
    return " OR ".join(parts) if parts else ""


def search(query: str, n: int = 8, path=None) -> list:
    """FTS5 매칭 + 사용 횟수 적은 순(반복 사용 방지)."""
    q = _fts_query(query)
    if not q:
        return []
    init_db(path)
    with closing(_conn(path)) as c:
        rows = c.execute(
            "SELECT p.*, photos_fts.rank AS rank FROM photos_fts "
            "JOIN photos p ON p.id = photos_fts.rowid "
            "WHERE photos_fts MATCH ? "
            "ORDER BY p.use_count ASC, photos_fts.rank LIMIT ?",
            (q, n)).fetchall()
    return [dict(r) for r in rows]


def find_for_post(topic: str, location: str = "", n: int = 6,
                  strict_location: bool = False, path=None) -> list:
    """글 주제+장소로 매칭. 장소가 있으면 그걸 우선해 검색.
    strict_location=True이면 장소 지정 시 장소 매칭 사진이 0장이면 주제 검색으로 넘어가지 않음
    (다른 지역 사진이 섞이는 것을 방지)."""
    queries = []
    if location:
        queries.append(location)
    if topic:
        queries.append(topic)
    seen, out = set(), []
    for i, q in enumerate(queries):
        before = len(out)
        for r in search(q, n, path):
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            out.append(r)
        if len(out) >= n:
            break
        # 장소 쿼리가 한 건도 못 찾았고 strict_location이면 주제 검색으로 넘어가지 않음
        if strict_location and i == 0 and location and len(out) == before:
            break
    return out[:n]


def mark_used(photo_id: int, path=None):
    """사진 사용 표시 — 다음 매칭에서 다른 사진 우선되도록."""
    with closing(_conn(path)) as c, c:
        c.execute("UPDATE photos SET use_count=use_count+1, last_used=? WHERE id=?",
                  (datetime.now().isoformat(), photo_id))


def add_user_tags(photo_id: int, tags: str, path=None):
    """사용자가 직접 단 태그(쉼표 구분) 저장 + FTS 갱신.
    태그가 바뀌면 xmp_synced_at을 비워 다음 XMP 동기화 대상이 되게 함."""
    init_db(path)
    with closing(_conn(path)) as c, c:
        c.execute("UPDATE photos SET user_tags=?, xmp_synced_at=NULL WHERE id=?",
                  (tags, photo_id))
        row = c.execute("SELECT filename,folder_tags,place,region,auto_caption "
                        "FROM photos WHERE id=?", (photo_id,)).fetchone()
        c.execute("DELETE FROM photos_fts WHERE rowid=?", (photo_id,))
        c.execute("INSERT INTO photos_fts (rowid, filename, folder_tags, place, region, user_tags, auto_caption) "
                  "VALUES (?,?,?,?,?,?,?)",
                  (photo_id, row["filename"], row["folder_tags"], row["place"],
                   row["region"], tags, row["auto_caption"] or ""))


# ── GPS 역지오코딩 (OSM Nominatim, 키 불필요) ─────────────────────────────────
_NOMINATIM = "https://nominatim.openstreetmap.org/reverse"
_GEOCODE_UA = "blog-studio/1.0 (contact: zeclab5@gmail.com)"


def reverse_geocode(lat: float, lng: float, timeout: int = 10) -> dict:
    """좌표 → 한국어 행정구역. {place, region, district, road}. 실패 시 빈 dict.
    OSM Nominatim 약관: 1초당 1건 이하 — 일괄 처리는 enrich_places가 처리."""
    try:
        url = _NOMINATIM + "?" + urllib.parse.urlencode({
            "lat": lat, "lon": lng, "format": "json",
            "accept-language": "ko", "zoom": "14"})
        req = urllib.request.Request(url, headers={"User-Agent": _GEOCODE_UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8"))
    except Exception:
        return {}
    a = d.get("address", {}) or {}
    # 한국 행정구역: state(도/광역시) → city/county(시/군) → suburb/quarter(구·동)
    region = a.get("state") or a.get("province") or ""
    city = a.get("city") or a.get("county") or a.get("town") or a.get("village") or ""
    sub = a.get("quarter") or a.get("suburb") or a.get("neighbourhood") or a.get("district") or ""
    place = " ".join(filter(None, [city, sub])).strip()
    return {"place": place or city, "region": region, "district": sub,
            "road": a.get("road", "")}


def enrich_places(limit: int = 100, rate: float = 1.1, log=print, path=None) -> dict:
    """GPS가 있지만 지명이 폴더 추측인 사진들에 한해 역지오코딩으로 갱신.
    Nominatim 약관 준수: 호출 사이 rate초 대기. {updated, skipped, failed} 반환."""
    init_db(path)
    with closing(_conn(path)) as c:
        rows = c.execute(
            "SELECT id, gps_lat, gps_lng, place, region FROM photos "
            "WHERE gps_lat IS NOT NULL AND gps_lng IS NOT NULL "
            "ORDER BY (CASE WHEN place IS NULL OR place='' THEN 0 ELSE 1 END), added_at "
            "LIMIT ?", (limit,)).fetchall()
    updated = failed = 0
    log(f"   🌐 GPS→지명 변환 대상 {len(rows)}건 (약 {len(rows) * rate:.0f}초)")
    for i, r in enumerate(rows, 1):
        info = reverse_geocode(r["gps_lat"], r["gps_lng"])
        if not info.get("place") and not info.get("region"):
            failed += 1
        else:
            with closing(_conn(path)) as c, c:
                # 폴더 추출보다 정확하므로 덮어쓰기
                c.execute("UPDATE photos SET place=COALESCE(NULLIF(?, ''), place), "
                          "region=COALESCE(NULLIF(?, ''), region) WHERE id=?",
                          (info.get("place", ""), info.get("region", ""), r["id"]))
                # FTS 동기화 — 일반 모드라 통째로 재삽입
                row2 = c.execute(
                    "SELECT filename, folder_tags, place, region, user_tags, auto_caption "
                    "FROM photos WHERE id=?", (r["id"],)).fetchone()
                c.execute("DELETE FROM photos_fts WHERE rowid=?", (r["id"],))
                c.execute("INSERT INTO photos_fts (rowid, filename, folder_tags, place, region, user_tags, auto_caption) "
                          "VALUES (?,?,?,?,?,?,?)",
                          (r["id"], row2["filename"] or "", row2["folder_tags"] or "",
                           row2["place"] or "", row2["region"] or "",
                           row2["user_tags"] or "", row2["auto_caption"] or ""))
            updated += 1
        if i % 20 == 0:
            log(f"     · {i}/{len(rows)} (갱신 {updated} / 실패 {failed})")
        time.sleep(rate)
    log(f"   ✅ GPS→지명 완료 — 갱신 {updated} / 실패 {failed}")
    return {"updated": updated, "failed": failed, "total": len(rows)}


def collect_for_post(topic: str, location: str = "", n: int = 6,
                     strict_location: bool = False, path=None) -> list:
    """글 주제+장소로 매칭된 사진 경로 리스트 반환(존재하는 것만, use_count++).
    strict_location=True이면 location 지정 시 해당 지역 사진 없으면 빈 리스트 반환.
    글 생성에 그대로 photos 인자로 쓸 수 있습니다."""
    found = find_for_post(topic, location, n, strict_location, path)
    out = []
    for r in found:
        p = Path(r["path"])
        if p.exists():
            out.append(p)
            try:
                mark_used(r["id"], path)
            except Exception:
                pass
    return out


def stats(path=None) -> dict:
    init_db(path)
    with closing(_conn(path)) as c:
        n = c.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
        n_gps = c.execute("SELECT COUNT(*) FROM photos WHERE gps_lat IS NOT NULL").fetchone()[0]
        n_tag = c.execute("SELECT COUNT(*) FROM photos WHERE user_tags IS NOT NULL AND user_tags!=''").fetchone()[0]
    return {"total": n, "with_gps": n_gps, "with_user_tags": n_tag}


def folder_tree(path=None) -> list:
    """등록된 사진들의 폴더 구조를 트리로 반환.
    각 노드: {"name", "path", "count"(이 폴더+하위 전체 사진 수), "children":[...]}."""
    init_db(path)
    with closing(_conn(path)) as c:
        rows = c.execute("SELECT path FROM photos").fetchall()
    own_count = {}
    for (p,) in rows:
        d = str(Path(p).parent)
        own_count[d] = own_count.get(d, 0) + 1

    root = {"children": {}}

    def ensure(dirpath):
        parts = Path(dirpath).parts
        node = root
        cur_path = ""
        for part in parts:
            cur_path = str(Path(cur_path) / part) if cur_path else part
            child = node["children"].setdefault(
                part, {"name": part, "path": cur_path, "own": 0, "children": {}})
            node = child
        return node

    for d, n in own_count.items():
        ensure(d)["own"] = n

    def convert(node):
        children = sorted((convert(c) for c in node["children"].values()),
                          key=lambda x: x["name"])
        total = node["own"] + sum(c["count"] for c in children)
        return {"name": node["name"], "path": node["path"], "count": total,
                "children": children}

    return sorted((convert(c) for c in root["children"].values()), key=lambda x: x["name"])


_SKIP_DIRS = {"$recycle.bin", "system volume information", "found.000",
             ".git", "tools", "node_modules", "__pycache__"}


def _skip_dir(name: str) -> bool:
    return name.lower() in _SKIP_DIRS or name.startswith("$") or name.startswith(".")


def list_subdirs(folder: str) -> list:
    """folder의 바로 아래 하위 폴더 + 그 폴더 바로 안의 사진 수(재귀 아님, 빠름).
    아직 스캔(등록)하지 않은 폴더도 디스크에서 직접 읽어 보여줌(탐색용)."""
    out = []
    try:
        with os.scandir(folder) as it:
            entries = list(it)
    except Exception:
        return out
    for e in entries:
        try:
            if not e.is_dir(follow_symlinks=False) or _skip_dir(e.name):
                continue
        except Exception:
            continue
        own = 0
        has_sub = False
        try:
            with os.scandir(e.path) as it2:
                for e2 in it2:
                    if e2.is_dir(follow_symlinks=False):
                        if not _skip_dir(e2.name):
                            has_sub = True
                    elif e2.is_file() and Path(e2.name).suffix.lower() in PHOTO_EXTS:
                        own += 1
        except Exception:
            pass
        out.append({"name": e.name, "path": e.path, "own": own, "has_sub": has_sub})
    out.sort(key=lambda x: x["name"].lower())
    return out


def list_image_files(folder: str, recursive: bool = True, limit: int = 2000) -> list:
    """folder 안의 실제 사진 파일 경로 목록(디스크 직접 조회, 등록 여부와 무관)."""
    out = []
    if recursive:
        for dirpath, dirnames, filenames in os.walk(folder):
            dirnames[:] = [d for d in dirnames if not _skip_dir(d)]
            for fn in filenames:
                if Path(fn).suffix.lower() in PHOTO_EXTS:
                    out.append(str(Path(dirpath) / fn))
                    if len(out) >= limit:
                        return out
    else:
        try:
            with os.scandir(folder) as it:
                for e in it:
                    if e.is_file() and Path(e.name).suffix.lower() in PHOTO_EXTS:
                        out.append(e.path)
                        if len(out) >= limit:
                            break
        except Exception:
            pass
    return out


def registered_dirs(path=None) -> set:
    """이미 라이브러리에 등록된(스캔된) 폴더 경로 집합(소문자)."""
    init_db(path)
    with closing(_conn(path)) as c:
        rows = c.execute("SELECT DISTINCT path FROM photos").fetchall()
    return {str(Path(p).parent).lower().rstrip("\\/") for (p,) in rows}


def photos_by_paths(paths: list, path=None) -> dict:
    """주어진 경로들 중 이미 등록된 사진을 path(소문자)→row dict로 반환."""
    if not paths:
        return {}
    init_db(path)
    out = {}
    with closing(_conn(path)) as c:
        for i in range(0, len(paths), 400):
            chunk = paths[i:i + 400]
            qmarks = ",".join("?" * len(chunk))
            rows = c.execute(f"SELECT * FROM photos WHERE path IN ({qmarks})", chunk).fetchall()
            for r in rows:
                d = dict(r)
                out[d["path"].lower()] = d
    return out


def register_file(path: str, db_path=None) -> int:
    """스캔 안 된 폴더의 사진 1장을 라이브러리에 즉석 등록(사용자가 직접 태그를 달 때).
    새/기존 photo id 반환."""
    f = Path(path)
    init_db(db_path)
    root = f.parent.parent if f.parent.parent != f.parent else f.parent
    ex = _exif(f)
    ftags = _folder_tags(f, root)
    kw = _split_filename(f.name)
    rec = {
        "path": str(f),
        "filename": " ".join(kw),
        "folder_tags": " ".join(ftags),
        "taken_at": ex.get("taken_at"),
        "width": ex.get("width"), "height": ex.get("height"),
        "gps_lat": ex.get("gps_lat"), "gps_lng": ex.get("gps_lng"),
        "place": ftags[-1] if ftags else "",
        "region": ftags[0] if ftags else "",
    }
    return upsert_photo(rec, db_path)


def merge_tags(existing: str, new_tags) -> str:
    """기존 태그(쉼표 구분 문자열)에 새 태그 목록을 중복 없이 병합."""
    cur = [x.strip() for x in (existing or "").split(",") if x.strip()]
    seen = {x.lower() for x in cur}
    for t in new_tags:
        t = (t or "").strip()
        if t and t.lower() not in seen:
            cur.append(t); seen.add(t.lower())
    return ", ".join(cur)


def count_untagged(path=None) -> int:
    """아직 AI 태깅을 시도하지 않은(태그·캡션 모두 빈) 사진 수."""
    init_db(path)
    with closing(_conn(path)) as c:
        return c.execute(
            "SELECT COUNT(*) FROM photos "
            "WHERE (user_tags IS NULL OR user_tags='') "
            "  AND (auto_caption IS NULL OR auto_caption='')").fetchone()[0]


def photo_caption(photo: dict, settings: dict, lang: str = "ko") -> str:
    """라이브러리 사진 출처 캡션. 기본값=설정의 photo_credit. 외부 라이선스가 있으면 그걸 사용."""
    attr = (photo.get("attribution") or "").strip()
    if attr:
        return attr
    credit = (settings.get("photo_credit") or "").strip()
    if not credit:
        return ""
    return f"© {credit}" if not credit.lower().startswith(("©", "(c)")) else credit
