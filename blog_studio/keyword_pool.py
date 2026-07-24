# -*- coding: utf-8 -*-
"""
keyword_pool.py — "황금 키워드" 자동화(제목 생성 이전 0단계).

사용자가 준 스펙(golden-keyword-automation-spec.md)은 네이버 검색광고 API(한국어·
네이버 검색 시장) 기준 검색량·CPC로 스코어링하는 구조였다. 그런데 이 블로그들은
영어권 외국인 독자가 구글로 찾아오는 콘텐츠라(제목도 영어 키워드 우선) 네이버 시장
데이터는 맞지 않는다 — 그래서 데이터 소스만 무료 신호로 교체했다:
  · LLM 관심도 순위(core.research_keywords, 이미 "인기·관심도 높은 순"으로 반환)
  · 구글 자동완성 등장 여부·순번(core.expand_keywords) — 여러 시드 변형에서 자주,
    상위에 나올수록 실제 관심이 높다고 봄
절대 검색량·CPC 대신 이 두 신호를 합친 **상대 점수**를 쓴다. 포화도(경쟁 문서 수)는
공식 무료 API가 없어(스크래핑은 스펙에서도 금지한 방식과 같은 리스크) 이번 버전에는
넣지 않았다 — 필요해지면 유료 SEO API를 붙여 확장.

파이프라인 모양은 스펙과 동일하게 유지: [키워드 수집·스코어링] → [풀(대기열) 저장,
pending/used/rejected 상태 관리] → [시리즈 기획(plan_series)의 theme으로 꺼내 씀].
풀은 블로그마다 따로 저장(현재 활성 블로그의 profiles/<id>/keyword_pool.json).
"""
import json
from datetime import datetime
from pathlib import Path

import blog_core as core

POOL_FILENAME = "keyword_pool.json"

# 저품질/계정 정지 리스크가 높은 카테고리 — 원본 스펙의 블랙리스트를 그대로 채택.
BLACKLIST = ["대출", "도박", "성인", "재난지원금", "주식리딩", "카지노", "복권"]


def _pool_file() -> Path:
    return core.GENERATED_DIR.parent / POOL_FILENAME


def load_pool() -> list:
    f = _pool_file()
    if not f.exists():
        return []
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_pool(pool: list) -> None:
    _pool_file().write_text(
        json.dumps(pool, ensure_ascii=False, indent=2), encoding="utf-8")


def _is_blacklisted(keyword: str) -> bool:
    k = keyword or ""
    return any(b in k for b in BLACKLIST)


def score_candidates(candidates: list) -> list:
    """candidates 각 항목: {keyword, en, note, llm_rank(0-based, 낮을수록 관심 높음),
    autocomplete_hits(자동완성에 등장한 시드 변형 수), autocomplete_rank_avg(평균 등장 순번,
    낮을수록 상위)}. 블랙리스트 항목은 제외하고, 나머지에 score(0~100대) 필드를 추가해 반환."""
    n = max(len(candidates), 1)
    out = []
    for c in candidates:
        kw = (c.get("keyword") or c.get("en") or "").strip()
        if not kw or _is_blacklisted(kw):
            continue
        llm_rank = c.get("llm_rank", n // 2)
        llm_score = 100 * (n - llm_rank) / n                      # 1위=100점에 가깝게
        hits = c.get("autocomplete_hits", 0)
        rank_avg = c.get("autocomplete_rank_avg", 10)
        auto_score = min(hits * (10 - min(rank_avg, 10)) * 5, 100)  # 자주·상위에 나올수록↑
        score = llm_score * 0.6 + auto_score * 0.4
        out.append({**c, "keyword": kw, "score": round(score, 1)})
    return out


def add_candidates(candidates: list, source: str = "manual", log=print) -> int:
    """스코어링 후 풀에 추가(이미 있는 키워드는 대소문자 무시하고 건너뜀).
    반환: 새로 추가된 개수."""
    pool = load_pool()
    existing = {(p.get("keyword") or "").strip().lower() for p in pool}
    scored = score_candidates(candidates)
    added = 0
    now = datetime.now().strftime("%Y-%m-%d")
    for c in scored:
        key = c["keyword"].lower()
        if key in existing:
            continue
        pool.append({
            "keyword": c["keyword"],
            "en": c.get("en", ""),
            "note": c.get("note", ""),
            "score": c.get("score", 0),
            "status": "pending",
            "source": source,
            "collected_at": now,
            "used_at": None,
        })
        existing.add(key)
        added += 1
    if added:
        save_pool(pool)
        log(f"   ➕ 키워드 풀에 {added}개 추가(전체 {len(pool)}개, 대기 중 "
            f"{sum(1 for p in pool if p['status'] == 'pending')}개)")
    else:
        log("   · 새로 추가된 키워드 없음(이미 풀에 있거나 블랙리스트)")
    return added


def collect_for_category(category: str, settings: dict, log=print, n: int = 10) -> int:
    """카테고리 하나에 대해 LLM 관심 키워드 조사 + 구글 자동완성 신호를 합쳐 풀에 추가.
    [🔎 관심 키워드 조사] 결과를 재사용하고 싶으면 score_researched_keywords()를 대신 쓰면
    LLM을 다시 호출하지 않는다(이 함수는 처음부터 새로 조사)."""
    kws = core.research_keywords(category, settings, log=log, n=n)
    return score_researched_keywords(kws, settings, source=f"category:{category}", log=log)


def score_researched_keywords(kws: list, settings: dict, source: str = "research_keywords",
                              log=print) -> int:
    """이미 조사된 core.research_keywords() 결과(kws)에 자동완성 신호를 더해 풀에 추가.
    LLM을 다시 부르지 않으므로 [🔎 관심 키워드 조사] 직후 바로 저장할 때 씀."""
    candidates = []
    for idx, k in enumerate(kws):
        keyword = (k.get("keyword") or "").strip()
        en = (k.get("en") or "").strip()
        if not keyword:
            continue
        hits, rank_sum = 0, 0
        seed = en or keyword
        try:
            exp = core.expand_keywords(seed, log=log)
            auto_list = [a.lower() for a in exp.get("autocomplete", [])]
            for pos, a in enumerate(auto_list):
                if keyword.lower() in a or (en and en.lower() in a):
                    hits += 1
                    rank_sum += pos
        except Exception as e:
            log(f"   ⚠️ 자동완성 신호 조회 생략({keyword}): {e}")
        rank_avg = (rank_sum / hits) if hits else 10
        candidates.append({
            "keyword": keyword, "en": en, "note": k.get("note", ""),
            "llm_rank": idx, "autocomplete_hits": hits, "autocomplete_rank_avg": rank_avg,
        })
    return add_candidates(candidates, source=source, log=log)


def get_next_keywords(n: int = 5, mark_used: bool = True) -> list:
    """대기 중(pending) 키워드 중 점수 상위 n개 반환. mark_used=True면 즉시 used로 표시."""
    pool = load_pool()
    candidates = [p for p in pool if p.get("status") == "pending"]
    candidates.sort(key=lambda x: x.get("score", 0), reverse=True)
    selected = candidates[:n]
    if mark_used and selected:
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        chosen_keys = {s["keyword"] for s in selected}
        for p in pool:
            if p["keyword"] in chosen_keys and p.get("status") == "pending":
                p["status"] = "used"
                p["used_at"] = now
        save_pool(pool)
    return selected


def set_status(keywords: list, status: str) -> int:
    """지정한 키워드들의 상태를 바꾼다(status: pending/used/rejected). 바뀐 개수 반환."""
    pool = load_pool()
    keys = {k.strip().lower() for k in keywords}
    changed = 0
    for p in pool:
        if (p.get("keyword") or "").strip().lower() in keys:
            p["status"] = status
            if status == "used" and not p.get("used_at"):
                p["used_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            changed += 1
    if changed:
        save_pool(pool)
    return changed


def pool_summary() -> dict:
    pool = load_pool()
    return {
        "total": len(pool),
        "pending": sum(1 for p in pool if p.get("status") == "pending"),
        "used": sum(1 for p in pool if p.get("status") == "used"),
        "rejected": sum(1 for p in pool if p.get("status") == "rejected"),
    }
