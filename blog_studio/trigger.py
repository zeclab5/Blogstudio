# -*- coding: utf-8 -*-
"""
trigger.py — k-culture-now 시기별 자동 발행 트리거 (v6 §6·§9).

정해진 시점에 '그 시기 카테고리'의 발행 후보(events.db)를 골라 큐레이션 글을 만들고,
(블로그 등록 시) k-culture-now에 발행 + publications 기록으로 중복을 막습니다.

스케줄(v6 §9):
  · This Week     — 매주 월요일
  · Weekend Picks — 매주 금요일
  · Coming Up / Decision Time — 격주 수요일(주차 짝/홀로 교대)
  · Monthly Preview — 매월 마지막 주 월요일
  · Festival Watch — 분기(1·4·7·10월) 첫 월요일

핵심
  due_categories(ref)         그 날 실행 예정인 카테고리 키 목록
  run_due(settings, ref, ...)  예정 카테고리들을 생성(필요시 발행)
"""

from datetime import date, timedelta
from calendar import monthrange

import events_db as db
import curator

BLOG_TAG = "k-culture-now"


def _is_last_monday(ref: date) -> bool:
    return ref.weekday() == 0 and (ref + timedelta(days=7)).month != ref.month


def due_categories(ref: date = None) -> list:
    """그 날(ref) 실행 예정인 시기별 카테고리 키 목록."""
    ref = ref or date.today()
    wd = ref.weekday()                     # 월=0 ... 일=6
    out = []
    if wd == 0:                            # 월요일
        out.append("this_week")
        if _is_last_monday(ref):
            out.append("monthly_preview")
        if ref.month in (1, 4, 7, 10) and ref.day <= 7:
            out.append("festival_watch")
    if wd == 2:                            # 수요일 — 격주 교대
        even = (ref.isocalendar()[1] % 2 == 0)
        out.append("coming_up" if even else "decision_time")
    if wd == 4:                            # 금요일
        out.append("weekend_picks")
    return out


def next_due(ref: date = None, horizon: int = 31) -> list:
    """앞으로 horizon일 안의 (날짜, 카테고리들) 목록 — 다음 예정 안내용."""
    ref = ref or date.today()
    out = []
    for i in range(horizon + 1):
        d = ref + timedelta(days=i)
        cats = due_categories(d)
        if cats:
            out.append((d.isoformat(), cats))
    return out


def run_due(settings: dict, ref: date = None, publish_fn=None,
            blog: str = BLOG_TAG, log=print, path=None) -> list:
    """그 날 예정 카테고리들을 처리.
      · publish_fn(cfg, log) 가 주어지면 발행하고 그 결과(url)로 publications 기록(중복방지).
      · 없으면 글만 생성(미리보기용).
    반환: [{category, status, cfg?, en_url?, ko_url?, count}]
    """
    ref = ref or date.today()
    cats = due_categories(ref)
    if not cats:
        log(f"   · {ref} — 예정된 트리거 없음")
        return []
    results = []
    for cat in cats:
        events = db.due_for_category(cat, ref, path=path)
        if not events:
            log(f"   · {db.CATEGORY_LABEL[cat]} — 후보 이벤트 없음(건너뜀)")
            results.append({"category": cat, "status": "no_events", "count": 0})
            continue
        log(f"   ✍ {db.CATEGORY_LABEL[cat]} — 이벤트 {len(events)}건으로 글 생성")
        try:
            cfg = curator.generate_curation_post(cat, events, settings, ref.isoformat(), log)
        except Exception as e:
            log(f"   ⚠️ {cat} 생성 실패: {e}")
            results.append({"category": cat, "status": "error", "count": len(events)})
            continue
        rec = {"category": cat, "cfg": cfg, "count": len(events)}
        if publish_fn:
            res = publish_fn(cfg, log) or {}
            for e in events:                # 발행한 이벤트는 그 카테고리로 기록(중복방지)
                db.add_publication(e["id"], blog, cat,
                                   res.get("ko_url", ""), res.get("en_url", ""), path=path)
            rec.update(status="published", en_url=res.get("en_url", ""), ko_url=res.get("ko_url", ""))
            log(f"   ✅ {db.CATEGORY_LABEL[cat]} 발행 완료")
        else:
            rec["status"] = "generated"
        results.append(rec)
    return results
