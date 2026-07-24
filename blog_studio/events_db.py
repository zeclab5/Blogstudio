# -*- coding: utf-8 -*-
"""
events_db.py — k-arts-now(시의성 큐레이션)용 이벤트 데이터베이스 (v6 §5·§6).

· SQLite 파일 1개(events.db)로 완결. 표준 라이브러리 sqlite3만 사용(설치 불필요).
· events  : 수집·수동입력된 공연/전시/페스티벌 원본
· publications : 어떤 이벤트가 어느 블로그·카테고리로 발행됐는지(중복 방지·이력)

핵심
  init_db()                          DB·테이블 생성(최초 1회, 호출해도 안전)
  upsert_event(ev)                   이벤트 추가/갱신(id 기준)
  archive_past_events(ref)           끝난 이벤트 자동 아카이브
  due_for_category(cat, ref)         그 시점에 그 카테고리로 발행할 후보(미발행분)
  add_publication(...) / already_published(...)   발행 기록·중복확인

날짜는 'YYYY-MM-DD' 문자열로 저장(ISO라 문자열 비교가 곧 날짜 비교).
이벤트는 블로그 공통이라 DB는 한 곳(SCRIPT_DIR/events.db)에 둡니다.
"""

import sqlite3
import json
from contextlib import closing
from pathlib import Path
from datetime import date, timedelta
from calendar import monthrange

try:
    import blog_core as _core
    DB_PATH = _core.SCRIPT_DIR / "events.db"
except Exception:
    DB_PATH = Path(__file__).resolve().parent / "events.db"

# 시기별 카테고리 키(발행 트리거용) — v6 §4.2 / §6.2
CATEGORY_KEYS = [
    "monthly_preview", "coming_up", "decision_time",
    "this_week", "weekend_picks", "festival_watch",
]
CATEGORY_LABEL = {
    "monthly_preview": "Monthly Preview (D-30)",
    "coming_up": "Coming Up (D-21)",
    "decision_time": "Decision Time (D-14)",
    "this_week": "This Week (D-7)",
    "weekend_picks": "Weekend Picks (D-2)",
    "festival_watch": "Festival Watch (장기)",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    title_ko TEXT NOT NULL,
    title_en TEXT,
    type TEXT NOT NULL,               -- performance/exhibition/festival
    category TEXT,                    -- dance/music/theater/art
    start_date DATE NOT NULL,
    end_date DATE,
    venue TEXT,
    region TEXT,
    price TEXT,                       -- free/paid/mixed
    booking_open_date DATE,
    booking_url TEXT,
    source TEXT,                      -- KOPIS/ARKO/manual/etc
    english_available INTEGER,
    importance INTEGER,               -- 1(low) ~ 5(high)
    image_url TEXT,
    description TEXT,
    collected_at TIMESTAMP,
    last_published TEXT,              -- JSON(발행 이력 요약)
    archived INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS publications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT,
    blog TEXT,                        -- arts-now/arts-travel/culture-dict
    category TEXT,                    -- monthly_preview/coming_up/...
    post_url_ko TEXT,
    post_url_en TEXT,
    published_at TIMESTAMP,
    FOREIGN KEY (event_id) REFERENCES events(id)
);
CREATE INDEX IF NOT EXISTS idx_events_start_date ON events(start_date);
CREATE INDEX IF NOT EXISTS idx_events_archived ON events(archived);
CREATE INDEX IF NOT EXISTS idx_pub_event_cat ON publications(event_id, category);
"""

_EVENT_FIELDS = [
    "id", "title_ko", "title_en", "type", "category", "start_date", "end_date",
    "venue", "region", "price", "booking_open_date", "booking_url", "source",
    "english_available", "importance", "image_url", "description",
    "collected_at", "last_published", "archived",
]


def _conn(path=None):
    c = sqlite3.connect(str(path or DB_PATH))
    c.row_factory = sqlite3.Row
    return c


def init_db(path=None):
    with closing(_conn(path)) as c, c:
        c.executescript(_SCHEMA)
    return path or DB_PATH


# ── 이벤트 입출력 ─────────────────────────────────────────────────────────────
def upsert_event(ev: dict, path=None) -> str:
    """이벤트 추가/갱신(id 기준). 필수: id, title_ko, type, start_date."""
    init_db(path)
    row = {k: ev.get(k) for k in _EVENT_FIELDS}
    if not row.get("id") or not row.get("title_ko") or not row.get("start_date"):
        raise ValueError("이벤트에는 최소한 id·title_ko·start_date 가 필요합니다.")
    row.setdefault("type", "performance")
    if row.get("collected_at") is None:
        row["collected_at"] = date.today().isoformat()
    if row.get("archived") is None:          # NULL이면 WHERE archived=0 에 안 걸림 → 0으로
        row["archived"] = 0
    if isinstance(row.get("last_published"), (dict, list)):
        row["last_published"] = json.dumps(row["last_published"], ensure_ascii=False)
    cols = ", ".join(_EVENT_FIELDS)
    ph = ", ".join("?" for _ in _EVENT_FIELDS)
    with closing(_conn(path)) as c, c:
        c.execute(f"INSERT OR REPLACE INTO events ({cols}) VALUES ({ph})",
                  [row.get(k) for k in _EVENT_FIELDS])
    return row["id"]


def get_event(event_id: str, path=None):
    with closing(_conn(path)) as c:
        r = c.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    return dict(r) if r else None


def all_events(include_archived=False, path=None) -> list:
    init_db(path)
    q = "SELECT * FROM events" + ("" if include_archived else " WHERE archived=0")
    with closing(_conn(path)) as c:
        return [dict(r) for r in c.execute(q + " ORDER BY start_date").fetchall()]


def archive_past_events(ref: date = None, path=None) -> int:
    """종료일이 지난 이벤트를 아카이브(end_date 없으면 start_date 기준). 처리 건수 반환."""
    ref = ref or date.today()
    init_db(path)
    with closing(_conn(path)) as c, c:
        cur = c.execute(
            "UPDATE events SET archived=1 WHERE archived=0 AND "
            "COALESCE(end_date, start_date) < ?", (ref.isoformat(),))
        return cur.rowcount


# ── 발행 이력 / 중복 방지 ─────────────────────────────────────────────────────
def already_published(event_id: str, category: str, path=None) -> bool:
    with closing(_conn(path)) as c:
        r = c.execute(
            "SELECT 1 FROM publications WHERE event_id=? AND category=? LIMIT 1",
            (event_id, category)).fetchone()
    return r is not None


def add_publication(event_id, blog, category, post_url_ko="", post_url_en="",
                    when=None, path=None):
    init_db(path)
    when = when or date.today().isoformat()
    with closing(_conn(path)) as c, c:
        c.execute(
            "INSERT INTO publications (event_id, blog, category, post_url_ko, "
            "post_url_en, published_at) VALUES (?,?,?,?,?,?)",
            (event_id, blog, category, post_url_ko, post_url_en, when))


# ── 날짜 구간 유틸 ────────────────────────────────────────────────────────────
def _month_bounds(ref: date, offset: int = 1):
    """ref 기준 offset달(1=다음달) 1일·말일."""
    y, m = ref.year, ref.month + offset
    while m > 12:
        m -= 12; y += 1
    while m < 1:
        m += 12; y -= 1
    return date(y, m, 1), date(y, m, monthrange(y, m)[1])


def _week_bounds(ref: date):
    """ref가 속한 주의 월요일·일요일."""
    mon = ref - timedelta(days=ref.weekday())
    return mon, mon + timedelta(days=6)


# ── 시기별 발행 후보 쿼리 (v6 §6.2) ───────────────────────────────────────────
def _window_for(category: str, ref: date):
    """(추가 WHERE조건, 파라미터) — start_date/end_date 기준 구간."""
    if category == "monthly_preview":
        a, b = _month_bounds(ref, 1)
        return "start_date BETWEEN ? AND ?", [a.isoformat(), b.isoformat()]
    if category == "coming_up":
        a, b = ref + timedelta(days=21), ref + timedelta(days=28)
        return "start_date BETWEEN ? AND ?", [a.isoformat(), b.isoformat()]
    if category == "decision_time":
        a, b = ref + timedelta(days=14), ref + timedelta(days=21)
        return "start_date BETWEEN ? AND ?", [a.isoformat(), b.isoformat()]
    if category == "this_week":
        a, b = _week_bounds(ref)
        return "start_date BETWEEN ? AND ?", [a.isoformat(), b.isoformat()]
    if category == "weekend_picks":
        a, b = _week_bounds(ref)            # 이번 주말에 '진행 중'인 것
        sat, sun = a + timedelta(days=5), b
        return ("start_date <= ? AND COALESCE(end_date, start_date) >= ?",
                [sun.isoformat(), sat.isoformat()])
    if category == "festival_watch":
        a, b = ref + timedelta(days=30), ref + timedelta(days=180)
        return ("type='festival' AND start_date BETWEEN ? AND ?",
                [a.isoformat(), b.isoformat()])
    raise ValueError(f"알 수 없는 카테고리: {category}")


def due_for_category(category: str, ref: date = None, limit: int = 20, path=None) -> list:
    """그 시점(ref)에 해당 카테고리로 발행할 후보 이벤트(미아카이브·미발행) 목록.
    중요도(importance) 높은 순으로 정렬."""
    ref = ref or date.today()
    init_db(path)
    cond, params = _window_for(category, ref)
    q = (f"SELECT * FROM events WHERE archived=0 AND ({cond}) "
         f"AND id NOT IN (SELECT event_id FROM publications WHERE category=?) "
         f"ORDER BY COALESCE(importance,0) DESC, start_date ASC LIMIT ?")
    with closing(_conn(path)) as c:
        rows = c.execute(q, params + [category, limit]).fetchall()
    return [dict(r) for r in rows]


def scan_all_due(ref: date = None, path=None) -> dict:
    """모든 시기별 카테고리의 발행 후보를 한 번에 — {category: [events...]}."""
    ref = ref or date.today()
    return {cat: due_for_category(cat, ref, path=path) for cat in CATEGORY_KEYS}
