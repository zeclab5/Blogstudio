"""
Blog Studio — 코어 모듈
========================
역할:
  - schedule.json (캘린더 데이터 + 설정) 읽기/쓰기
  - 로컬 LLM(gemma4, Ollama) 또는 Claude(API)로 한/영 블로그 글 생성
  - 기존 publisher\\publish_today.py 의 발행 로직을 그대로 재사용해
    특정 날짜 글을 Blogger에 발행
  - "다음 발행 시각" 계산 (스케줄러용)

GUI(studio_gui.py)와 콘솔 양쪽에서 import 해서 씁니다.
중복 구현 금지 — 발행/이미지 업로드는 publisher 폴더 코드를 빌려 씁니다.
"""

import os
import sys
import json
import re
import time
import shutil
import tempfile
import urllib.request
import urllib.error
import urllib.parse
import subprocess
from pathlib import Path
from datetime import date, datetime, timedelta


# ── pythonw(콘솔 없음) 대응 ───────────────────────────────────────────────────
# pythonw로 실행하면 sys.stdout/sys.stderr 가 None 입니다. 일부 모듈
# (publish_today 등)이 sys.stderr.encoding 을 검사하다 터지므로 더미로 교체.
class _NullWriter:
    encoding = "utf-8"
    errors = "replace"
    def write(self, s):
        return len(s) if s else 0
    def flush(self):
        pass
    def reconfigure(self, *args, **kwargs):
        pass
    def isatty(self):
        return False

if sys.stdout is None:
    sys.stdout = _NullWriter()
if sys.stderr is None:
    sys.stderr = _NullWriter()

# ── 콘솔 UTF-8 ────────────────────────────────────────────────────────────────
if sys.stdout and sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ── 경로 ──────────────────────────────────────────────────────────────────────
# frozen(PyInstaller) 환경에서도 동작하도록 실행 파일 위치 기준으로 잡습니다.
if getattr(sys, "frozen", False):
    SCRIPT_DIR = Path(sys.executable).resolve().parent
else:
    SCRIPT_DIR = Path(__file__).resolve().parent

# blog_studio와 publisher는 항상 같은 부모 폴더 아래의 형제 폴더 — 어느 OS·어느 경로에
# 복사해 두든 자동으로 맞도록 SCRIPT_DIR(=blog_studio 위치) 기준 상대 경로로 계산한다.
# (예전엔 Path(r"C:\blogger")로 하드코딩돼 있어 폴더를 옮기거나 맥에서 실행하면 publisher를
# 못 찾아 깨졌다 — 2026-07-24 수정, 크로스플랫폼/이식 대응)
BLOGGER_ROOT  = SCRIPT_DIR.parent
PUBLISHER_DIR = BLOGGER_ROOT / "publisher"
SCHEDULE_FILE = SCRIPT_DIR / "schedule.json"
GENERATED_DIR = SCRIPT_DIR / "generated"
GENERATED_DIR.mkdir(parents=True, exist_ok=True)

# 발행 대상 블로그 정보(첫 줄 ID, 둘째 줄 URL)는 publisher와 공유
BLOG_ID_FILE = PUBLISHER_DIR / "blog_id.txt"
DEFAULT_BLOG_URL = "k-arts-travel.blogspot.com"

# ── 멀티 블로그(프로필) ───────────────────────────────────────────────────────
# 블로그마다 자체 폴더(profiles/<id>/)에 schedule/generated/token/세션을 둡니다.
# SCHEDULE_FILE / GENERATED_DIR 는 활성 블로그에 따라 set_active_blog()가 바꿉니다.
PROFILES_DIR = SCRIPT_DIR / "profiles"
PROFILES_DIR.mkdir(parents=True, exist_ok=True)
BLOGS_FILE = SCRIPT_DIR / "blogs.json"
SHARED_SECRETS = PUBLISHER_DIR / "client_secrets.json"   # OAuth 앱(계정 무관) — 공유

# 기존 단일 블로그(마이그레이션 기준)
LEGACY_BLOG = {
    "id": "5372668460061236159",
    "url": "https://k-arts-travel.blogspot.com/",
    "name": "Korea Arts & Travel",
}

# publisher 폴더를 import 경로에 추가 (발행 로직 재사용)
if str(PUBLISHER_DIR) not in sys.path:
    sys.path.insert(0, str(PUBLISHER_DIR))

# publish_today 를 지금(실제 stdout 상태에서) 미리 import 해 둡니다.
# 작업 스레드에서 stdout이 QueueWriter로 바뀐 뒤 처음 import되면
# 모듈 최상단의 sys.stdout.encoding 검사에서 오류가 나기 때문입니다.
try:
    import publish_today as _pub_preload  # noqa: F401
except Exception:
    _pub_preload = None

# ── 기본 설정 ─────────────────────────────────────────────────────────────────
DEFAULT_SETTINGS = {
    "publish_time": "09:00",          # 매일 자동 발행 시각 (HH:MM, 24시간)
    "llm": "gemma4",                  # "gemma4" | "claude"
    "ollama_url": "http://localhost:11434",
    "ollama_model": "gemma4:26b",
    "claude_api_key": "",             # sk-ant-...
    "claude_model": "claude-opus-4-8",
    "auto_publish": True,             # 프로그램 켜져 있을 때 자동 발행
    "blog_hint": "한국 여행과 전통예술을 다루는 한/영 이중언어 블로그",
    # 이 블로그만의 '색깔(정체성)'. 기획·생성이 이 틀 안에서만 작동하도록 반영됩니다.
    # (LLM이 안정적으로 처리하도록 한 단락으로 간결하게 — 너무 길면 출력이 불안정해집니다.)
    "blog_identity": (
        "Korea Arts & Travel — 한국의 전통예술·공예·장인·무형문화유산을 외국인 눈높이로 깊이 있게 "
        "다루는 한/영 블로그. 민화·탈춤·한지·도자기(청자·분청)·국악·단청·나전·매듭·보자기·서예 같은 "
        "주제를 우선하고, 유명 관광지의 뻔한 정보나 행정정보·흔한 먹거리 나열은 피한다."
    ),
    "sections": 5,                    # 글을 몇 개의 소주제로 나눠 깊이 있게 쓸지
    "blog_url": "",                   # 발행 대상 블로그(표시용)
    "series_count": 5,                # 시리즈 기획 시 기본 편수
    # 이 블로그 색깔에 맞는, 덜 포화됐지만 문화에 관심 있는 외국인이 찾는 키워드(시리즈 기획 근거).
    "seed_keywords": [
        "Korean folk painting minhwa", "Korean mask dance talchum", "hanji Korean paper craft",
        "Korean traditional music gugak", "Jongmyo royal ancestral rite", "Korean celadon buncheong pottery",
        "dancheong temple painting", "najeon mother-of-pearl lacquerware", "maedeup Korean knot art",
        "bojagi Korean wrapping cloth", "Korean traditional dance", "Andong Hahoe mask village",
        "Korean intangible cultural heritage", "Korean royal court music", "Korean tea ceremony darye",
        "Jeonju hanji village", "Tongyeong traditional crafts", "Gangneung Danoje festival",
        "Korean temple art and architecture", "Korean traditional weaving hanbok", "Korean folk village experience",
        "Korean calligraphy seoye", "buncheong ceramics workshop", "Korean lacquer ottchil craft",
    ],
    # 요일별 주간 템플릿: "0"(월)~"6"(일) → {enabled, topic, refs, time}
    #   topic: 그 요일에 기본으로 발행할 주제
    #   refs : 참고 사이트/작성 방향 (LLM 프롬프트에 그대로 전달)
    #   time : 그 요일 발행 시각(HH:MM). 비우면 위의 publish_time 사용
    "weekly": {},
    # 시리즈 기획 카테고리: 요일별 주제 + 여기에 직접 추가한 커스텀 주제(블로그마다 다름).
    "series_categories": [],
    # 이미지 찾기 — 무료 사진 사이트 API 키(선택). 비우면 키 없는 Openverse·Wikimedia 등만 사용.
    "pexels_key": "",
    "pixabay_key": "",
    "unsplash_key": "",
    # 위키미디어 공식 통합 API(api.wikimedia.org) 개인 API 토큰 — Commons 검색 보강.
    "wikimedia_token": "",
    # 한국관광공사 TourAPI 서비스키(data.go.kr) — 한국 관광지·문화재 이미지 최우선.
    "tourapi_key": "",
    # 공유마당(한국저작권위원회 공공누리 자유이용 저작물) 키 — gongu.copyright.or.kr/data.go.kr.
    "gongu_key": "",
    # 국가유산청은 khs.go.kr에서 키 없이 바로 동작(아래 image_finder.search_heritage).
    # 내 사진 라이브러리 — 사용자가 찍은 사진의 출처 표기(© ...). 빈 값이면 무표기.
    "photo_credit": "",
    # 사진 폴더 미지정 시, 내 사진 라이브러리(photo_library.db)에서 자동 매칭할지.
    "use_photo_library": True,
    # 비전 모델 — 사진 자동 캡션·태그 (Ollama VLM). Qwen2.5-VL 7B 권장.
    "vision_model": "qwen2.5vl:7b",
    # 이벤트 수집 — 문화체육관광부 문화예술공연(통합) OpenAPI 서비스키(data.go.kr/culture.go.kr).
    "culture_api_key": "",
    # 사진 폴더를 직접 지정하지 않은 글에 대해, 날짜로 폴더를 자동 탐색해 사진을 넣을지.
    # 기본 False — 옛 날짜 폴더의 무관한 사진이 끼어드는 문제 방지(직접 지정한 폴더만 사용).
    "auto_date_photos": False,
    # 사실 검증(그라운딩) — 네이버 검색 API 키(developers.naver.com, 무료). 비우면 그라운딩 생략.
    "naver_client_id": "",
    "naver_client_secret": "",
    "dark_mode": True,                # 다크 모드 UI (모던 다크 우선)
    # 로컬 이미지 생성(ComfyUI) — 촬영목록 기반 AI 이미지. 비우면 기본 주소/자동 체크포인트.
    "comfy_url": "http://127.0.0.1:8188",
    "comfy_ckpt": "",
    "comfy_path": "",                 # ComfyUI 설치 폴더(비우면 자동 감지)
}

# 요일 인덱스(date.weekday(): 월=0 ... 일=6) → 한글 라벨
WEEKDAY_KO = ["월", "화", "수", "목", "금", "토", "일"]

# 발행 상태
ST_PENDING   = "pending"     # 주제만 있음 (미생성/미발행)
ST_GENERATED = "generated"   # 글 생성 완료, 발행 전
ST_PUBLISHED = "published"   # 발행 완료
ST_ERROR     = "error"       # 발행/생성 실패


class StopRequested(RuntimeError):
    """사용자가 진행 중인 작업(생성·발행·시리즈 기획)을 중단 요청했을 때 올리는 전용 예외.
    RuntimeError를 상속해 기존 except RuntimeError 처리와도 호환되지만, '다단계 생성 실패 →
    단일 생성 폴백' 같은 다른 except 블록이 이걸 일반 실패로 오인해 폴백/재시도하지 않도록
    별도 타입으로 구분한다(폴백 지점에서 except StopRequested: raise 로 먼저 걸러냄)."""
    pass


# ══════════════════════════════════════════════════════════════════════════════
#  schedule.json 입출력
# ══════════════════════════════════════════════════════════════════════════════

def load_schedule() -> dict:
    if SCHEDULE_FILE.exists():
        try:
            data = json.loads(SCHEDULE_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    else:
        data = {}
    settings = dict(DEFAULT_SETTINGS)
    settings.update(data.get("settings", {}))
    data["settings"] = settings
    data.setdefault("entries", {})
    return data


def save_schedule(data: dict):
    SCHEDULE_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ── 블로그 레지스트리 / 프로필 전환 ───────────────────────────────────────────

def load_registry() -> dict:
    if BLOGS_FILE.exists():
        try:
            reg = json.loads(BLOGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            reg = {}
    else:
        reg = {}
    reg.setdefault("blogs", {})
    reg.setdefault("active", "")
    return reg


def save_registry(reg: dict):
    BLOGS_FILE.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")


def profile_dir(blog_id: str) -> Path:
    d = PROFILES_DIR / blog_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def set_active_blog(blog_id: str, persist: bool = True):
    """활성 블로그 전환 — 데이터(SCHEDULE_FILE/GENERATED_DIR)와 인증(토큰/세션) 경로를
    해당 블로그 프로필로 바꿉니다. 이후 load_schedule/발행이 그 블로그 기준으로 동작.
    persist=False면 레지스트리의 active를 저장하지 않음(스케줄러의 일시 스캔용)."""
    global SCHEDULE_FILE, GENERATED_DIR, BLOG_ID_FILE
    reg = load_registry()
    b = reg["blogs"].get(blog_id)
    if not b:
        raise RuntimeError(f"등록되지 않은 블로그: {blog_id}")
    pdir = profile_dir(blog_id)

    SCHEDULE_FILE = pdir / "schedule.json"
    GENERATED_DIR = pdir / "generated"
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)

    token_file = Path(b.get("token_file") or (pdir / "token.json"))
    blogid_file = Path(b.get("blogid_file") or (pdir / "blog_id.txt"))
    prof = Path(b.get("profile_dir") or (pdir / "browser_profile"))
    BLOG_ID_FILE = blogid_file

    # blog_id.txt 보장
    if not blogid_file.exists():
        blogid_file.parent.mkdir(parents=True, exist_ok=True)
        blogid_file.write_text(f"{blog_id}\n{b.get('url','')}", encoding="utf-8")

    # publisher / 업로더 모듈의 경로를 이 블로그 인증으로 스왑
    try:
        import publish_today as pub
        pub.TOKEN_FILE = token_file
        pub.BLOG_ID_FILE = blogid_file
        pub.SECRETS_FILE = SHARED_SECRETS
    except Exception:
        pass
    try:
        import upload_via_browser as uvb
        uvb.PROFILE_DIR = prof
        uvb.BLOG_ID_FILE = blogid_file
    except Exception:
        pass

    if persist and reg.get("active") != blog_id:
        reg["active"] = blog_id
        save_registry(reg)


def register_blog(blog_id, url, name, token_file=None, blogid_file=None, profile_dir_=None):
    """블로그를 레지스트리에 등록(인증 경로 지정 가능)."""
    pdir = profile_dir(blog_id)
    reg = load_registry()
    reg["blogs"][blog_id] = {
        "url": url,
        "name": name or url,
        "token_file": str(token_file or (pdir / "token.json")),
        "blogid_file": str(blogid_file or (pdir / "blog_id.txt")),
        "profile_dir": str(profile_dir_ or (pdir / "browser_profile")),
    }
    Path(reg["blogs"][blog_id]["blogid_file"]).write_text(
        f"{blog_id}\n{url}", encoding="utf-8")
    save_registry(reg)
    return reg["blogs"][blog_id]


def verify_blog_browser(blog_id: str, log=print) -> dict:
    """해당 블로그의 사진 업로드 브라우저 세션을 열어
    로그인 계정·에디터 권한을 검증하고 결과를 레지스트리에 기록.
    (브라우저 창이 뜨며, 로그인이 안 돼 있으면 사용자가 직접 로그인할 때까지 대기)"""
    import upload_via_browser as uvb
    set_active_blog(blog_id, persist=False)   # 초안 생성에 이 블로그의 API 토큰 사용
    reg = load_registry()
    b = reg["blogs"].get(blog_id)
    if not b:
        raise RuntimeError(f"등록되지 않은 블로그: {blog_id}")
    prof = b.get("profile_dir") or str(profile_dir(blog_id) / "browser_profile")
    Path(prof).mkdir(parents=True, exist_ok=True)
    log(f"   🔍 세션 검증 시작 — {b.get('name','')} ({Path(prof).name})")
    res = uvb.login_and_verify(prof, blog_id)
    b["verified_email"] = res.get("email", "")
    b["verified_ok"] = bool(res.get("editor_ok"))
    b["verified_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    save_registry(reg)
    return res


def own_profile_path(blog_id: str) -> str:
    return str(profile_dir(blog_id) / "browser_profile")


def revert_own_profile(blog_id: str):
    """공유 해제 — 이 블로그 전용 브라우저 세션으로 전환."""
    reg = load_registry()
    b = reg["blogs"].get(blog_id)
    if not b:
        raise RuntimeError(f"등록되지 않은 블로그: {blog_id}")
    b["profile_dir"] = own_profile_path(blog_id)
    b.pop("verified_ok", None)
    b.pop("verified_email", None)
    save_registry(reg)
    return b["profile_dir"]


def set_shared_browser(target_id: str, source_id: str):
    """target 블로그의 '사진 업로드 브라우저 세션'을 source 블로그 것으로 공유.
    (같은 구글 계정일 때만 의미 있음 — 이미 로그인된 세션을 재사용해 재로그인 불필요)"""
    reg = load_registry()
    src = reg["blogs"].get(source_id)
    tgt = reg["blogs"].get(target_id)
    if not src or not tgt:
        raise RuntimeError("블로그를 찾을 수 없습니다.")
    tgt["profile_dir"] = src["profile_dir"]
    save_registry(reg)
    return src["profile_dir"]


def ensure_initialized() -> dict:
    """최초 1회 — 기존 단일 블로그(k-arts)를 프로필로 마이그레이션하고 활성화.
    GUI 시작 시 load_schedule 전에 반드시 호출."""
    reg = load_registry()
    if not reg["blogs"]:
        kid = LEGACY_BLOG["id"]
        pdir = profile_dir(kid)
        # 기존 일정 데이터 이전(있으면)
        legacy_sched = SCRIPT_DIR / "schedule.json"
        if legacy_sched.exists() and not (pdir / "schedule.json").exists():
            shutil.copy(legacy_sched, pdir / "schedule.json")
        # 기존 인증/세션은 publisher 폴더 것을 '그대로' 재사용(복사하지 않음 → 로그인 보존)
        reg["blogs"][kid] = {
            "url": LEGACY_BLOG["url"],
            "name": LEGACY_BLOG["name"],
            "token_file": str(PUBLISHER_DIR / "token.json"),
            "blogid_file": str(PUBLISHER_DIR / "blog_id.txt"),
            "profile_dir": str(PUBLISHER_DIR / "browser_profile"),
        }
        reg["active"] = kid
        save_registry(reg)
    active = reg.get("active") or next(iter(reg["blogs"]))
    set_active_blog(active)
    return load_registry()


def add_blog_via_login(url_keyword, log=print):
    """새 구글 계정으로 로그인해, 그 계정의 블로그 중 url_keyword를 포함한 블로그를 등록.
    브라우저 OAuth 창이 뜹니다(해당 블로그 계정으로 로그인). 반환: (id, url, name).
    주의: 호출 후 set_active_blog로 UI 활성 블로그를 복구하세요."""
    import publish_today as pub
    from googleapiclient.discovery import build

    tmp_token = PROFILES_DIR / "_pending_token.json"
    if tmp_token.exists():
        tmp_token.unlink()
    old_token, old_secrets = pub.TOKEN_FILE, pub.SECRETS_FILE
    pub.TOKEN_FILE = tmp_token
    pub.SECRETS_FILE = SHARED_SECRETS
    try:
        log("   🔐 새 계정 로그인 창을 엽니다 — 추가할 블로그의 구글 계정으로 로그인하세요...")
        creds = pub.get_credentials()
        service = build("blogger", "v3", credentials=creds)
        items = service.blogs().listByUser(userId="self").execute().get("items", [])
        kw = url_keyword.lower().replace("https://", "").replace("http://", "").strip("/")
        match = [b for b in items if kw in b.get("url", "").lower()]
        if not match:
            avail = ", ".join(b.get("url", "") for b in items) or "(없음)"
            raise RuntimeError(f"'{url_keyword}' 블로그를 이 계정에서 찾지 못했습니다. "
                               f"이 계정의 블로그: {avail}")
        b = match[0]
        bid, url, name = b["id"], b.get("url", ""), b.get("name", "")
        pdir = profile_dir(bid)
        shutil.move(str(tmp_token), str(pdir / "token.json"))
        register_blog(bid, url, name,
                      token_file=pdir / "token.json",
                      blogid_file=pdir / "blog_id.txt",
                      profile_dir_=pdir / "browser_profile")
        log(f"   ✅ 블로그 추가 완료: {name} ({url})")
        return bid, url, name
    finally:
        pub.TOKEN_FILE, pub.SECRETS_FILE = old_token, old_secrets
        if tmp_token.exists():
            try:
                tmp_token.unlink()
            except Exception:
                pass


def scan_all_due(now=None):
    """모든 등록 블로그에서 지금 발행해야 할 (blog_id, date_str) 목록을 반환.
    부작용: 활성 블로그가 바뀝니다 — 호출 후 원하는 블로그로 set_active_blog 하세요."""
    reg = load_registry()
    out = []
    for bid in list(reg["blogs"].keys()):
        try:
            set_active_blog(bid, persist=False)
            data = load_schedule()
            for ds in due_dates(data, now):
                out.append((bid, ds))
        except Exception:
            continue
    return out


def get_entry(data: dict, date_str: str) -> dict:
    return data["entries"].get(date_str)


def set_topic(data: dict, date_str: str, topic: str, refs: str = None, time_: str = None):
    """날짜에 개별 주제/참고/시각을 지정. topic이 비고 refs/time도 없으면 항목 삭제.
    refs/time 이 None이면 해당 필드는 건드리지 않습니다."""
    topic = (topic or "").strip()
    e = data["entries"].get(date_str, {})

    if not topic and not (refs or "").strip() and not (time_ or "").strip():
        # 개별 발행 이력(status/url)이 없으면 통째로 삭제
        if not e or e.get("status") in (None, ST_PENDING):
            data["entries"].pop(date_str, None)
            return

    # 주제/참고가 바뀌면 생성물 무효화
    changed = (e.get("topic") != topic) or (refs is not None and e.get("refs", "") != refs)
    if changed and e.get("status") == ST_GENERATED:
        e["status"] = ST_PENDING

    if topic:
        e["topic"] = topic
    elif "topic" in e:
        e.pop("topic")
    if refs is not None:
        if refs.strip():
            e["refs"] = refs.strip()
        else:
            e.pop("refs", None)
    if time_ is not None:
        if time_.strip():
            e["time"] = time_.strip()
        else:
            e.pop("time", None)
    e.setdefault("status", ST_PENDING)
    data["entries"][date_str] = e


# ── 주간 템플릿 ───────────────────────────────────────────────────────────────

def _weekly_table(data: dict) -> dict:
    return data["settings"].setdefault("weekly", {})


def get_weekly(data: dict, wd: int) -> dict:
    """요일(월=0..일=6) 템플릿 반환 (없으면 빈 기본값)."""
    w = _weekly_table(data).get(str(wd), {})
    return {
        "enabled": bool(w.get("enabled", False)),
        "topic": w.get("topic", ""),
        "refs": w.get("refs", ""),
        "time": w.get("time", ""),
        "md_file": w.get("md_file", ""),
    }


def set_weekly(data: dict, wd: int, enabled: bool, topic: str, refs: str, time_: str,
               md_file: str = ""):
    _weekly_table(data)[str(wd)] = {
        "enabled": bool(enabled),
        "topic": (topic or "").strip(),
        "refs": (refs or "").strip(),
        "time": (time_ or "").strip(),
        "md_file": (md_file or "").strip(),
    }


def blog_categories(data: dict) -> list:
    """이 블로그의 시리즈 기획 카테고리 목록(블로그마다 다름).
    ① 요일별 발행 주제(활성 요일의 topic)를 그대로 카테고리로 쓰고,
    ② 사용자가 추가한 커스텀 카테고리(settings['series_categories'])를 합칩니다.
    둘 다 없으면 기본 카테고리(CATEGORIES)로 폴백."""
    out, seen = [], set()
    for wd in range(7):                       # 요일 순서대로 발행 주제
        w = get_weekly(data, wd)
        t = (w.get("topic") or "").strip()
        if w.get("enabled") and t and t not in seen:
            seen.add(t); out.append(t)
    for c in data["settings"].get("series_categories", []):   # 직접 추가한 것
        c = (c or "").strip()
        if c and c not in seen:
            seen.add(c); out.append(c)
    if not out:
        out = list(CATEGORIES.keys())
    return out


def add_category(data: dict, name: str) -> bool:
    """커스텀 카테고리(주제) 추가. 이미 목록(요일 주제 포함)에 있으면 추가 안 함.
    추가됐으면 True."""
    name = (name or "").strip()
    if not name:
        return False
    if name in set(blog_categories(data)):
        return False
    data["settings"].setdefault("series_categories", []).append(name)
    return True


# ── k-arts-now (시의성 큐레이션 블로그) 전략 프리셋 (v6) ──────────────────────
KARTS_NOW_IDENTITY = (
    "K-Culture Now — 한국의 무료·저렴한 공연·전시·페스티벌을 외국인 방문객과 거주자에게 "
    "'미리' 알려주는 시의성 큐레이션 블로그. 방문 2~3개월 전부터 계획하는 독자를 위해 "
    "D-30(다음 달 미리보기)부터 당일까지 다층으로 안내한다. 무용·공연을 중심으로, 공연 "
    "영상디자인 현장 감각을 살려 '지금 갈 만한 것'을 콕 집어 추천한다. 흔한 관광 정보가 "
    "아니라 실제 일정·예매·교통·무료 여부 같은 실용 정보를 정확히 전한다."
)

KARTS_NOW_CATEGORIES = [
    "Monthly Preview — 다음 달 라인업 미리보기 (D-30)",
    "Coming Up — 예매 시작·인기 공연 픽 (D-21)",
    "Decision Time — 갈지 결정·상세 일정·교통 (D-14)",
    "This Week — 이번 주 가볼 만한 곳 (D-7)",
    "Weekend Picks — 이번 주말 한정 (D-2)",
    "Festival Watch — 큰 페스티벌 미리 알림 (장기)",
]

# 요일별 정기 발행(주간 템플릿) — v6 §9. (월=0..일=6) → (주제, 발행시각)
# 비정기(월말 Monthly Preview·분기 Festival Watch)는 카테고리로만 두고 수동/반자동 발행.
KARTS_NOW_WEEKLY = {
    0: ("This Week — 이번 주 가볼 만한 곳 (D-7)", "06:00"),          # 월
    2: ("Coming Up · Decision Time (격주 교대, D-21/D-14)", "06:00"),  # 수
    4: ("Weekend Picks — 이번 주말 한정 (D-2)", "06:00"),            # 금
}

KARTS_NOW_SEEDS = [
    "free performances in Seoul", "Korea dance festival 2026", "free exhibitions Korea",
    "Korean traditional performance this week", "Seoul weekend events", "Korea arts festival schedule",
    "free concerts Korea", "Korean modern dance show", "Korea ballet festival", "Korea theater festival",
    "Korea performing arts calendar", "things to do in Seoul this weekend",
]

# 주요 페스티벌 달력 (v6 §7.3) — Festival Watch·Monthly Preview의 사전 알림 근거 데이터.
KOREA_FESTIVALS = [
    {"name": "전국무용제", "months": "5~6월", "field": "한국·현대무용"},
    {"name": "서울무용제", "months": "11~12월", "field": "한국·현대무용"},
    {"name": "대한민국발레축제", "months": "6월", "field": "발레"},
    {"name": "SIDance 서울세계무용축제", "months": "9~10월", "field": "국제 무용"},
    {"name": "창무국제공연예술제", "months": "9월", "field": "국제 한국무용"},
    {"name": "부산국제무용제", "months": "5~6월", "field": "국제 무용"},
    {"name": "광주디자인비엔날레", "months": "9~11월", "field": "시각예술"},
    {"name": "광주비엔날레", "months": "9~11월(격년)", "field": "시각예술"},
    {"name": "서울국제공연예술제 SPAF", "months": "10월", "field": "종합 공연"},
]


def is_karts_now(url: str) -> bool:
    """시의성 큐레이션 블로그(k-arts-now / k-culture-now 등 '-now' 블로그) 감지."""
    u = (url or "").lower()
    return "arts-now" in u or "culture-now" in u


def apply_karts_now_preset(data: dict) -> None:
    """활성 블로그(schedule data)에 k-arts-now 전략 프리셋 적용
    — 정체성·6카테고리·요일 일정(월·수·금 06시)·시드 키워드를 채웁니다."""
    s = data["settings"]
    s["blog_identity"] = KARTS_NOW_IDENTITY
    s["series_categories"] = list(KARTS_NOW_CATEGORIES)
    s["seed_keywords"] = list(KARTS_NOW_SEEDS)
    for wd, (topic, t) in KARTS_NOW_WEEKLY.items():
        set_weekly(data, wd, True, topic, "", t, "")


def read_ref_doc(path: str, cap: int = 4000) -> str:
    """참고 .md/.txt 파일 내용을 읽어 (너무 길면 잘라서) 반환."""
    if not path:
        return ""
    try:
        p = Path(path)
        if not p.exists():
            return ""
        txt = p.read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        return ""
    if len(txt) > cap:
        txt = txt[:cap] + "\n…(이하 생략)"
    return txt


def combine_refs(refs: str, md_path: str) -> str:
    """작성 방향(refs)에 첨부 참고문서 내용을 합쳐 하나의 참고 텍스트로."""
    refs = (refs or "").strip()
    doc = read_ref_doc(md_path)
    if doc:
        head = f"\n\n[첨부 참고 문서: {Path(md_path).name}]\n{doc}"
        return (refs + head).strip()
    return refs


def weekly_for_date(data: dict, date_str: str):
    """해당 날짜 요일에 활성화된 주간 템플릿(주제 있음)을 반환, 없으면 None."""
    try:
        wd = datetime.strptime(date_str, "%Y-%m-%d").weekday()
    except Exception:
        return None
    w = get_weekly(data, wd)
    if w["enabled"] and w["topic"].strip():
        return w
    return None


def planned(data: dict, date_str: str) -> dict:
    """그 날짜의 '실제 발행 계획'을 계산해 반환.
    개별 날짜 항목(entries[date])이 우선, 없으면 주간 템플릿을 사용합니다.
    반환 키: topic, refs, time, status, origin('date'|'weekly'|None),
             en_url, ko_url, published_at
    """
    e = data["entries"].get(date_str) or {}
    w = weekly_for_date(data, date_str)

    topic = (e.get("topic") or (w["topic"] if w else "") or "").strip()
    if e.get("refs") is not None and e.get("refs") != "":
        refs = e.get("refs")
    elif w:
        refs = w["refs"]
    else:
        refs = ""
    t = (e.get("time") or (w["time"] if w else "")
         or data["settings"].get("publish_time", "09:00")).strip()
    md_file = e.get("md_file") if e.get("md_file") else (w["md_file"] if w else "")

    if e.get("topic"):
        origin = "date"
    elif w:
        origin = "weekly"
    else:
        origin = None

    status = e.get("status") if e.get("status") else (ST_PENDING if origin else None)
    return {
        "topic": topic, "refs": refs, "time": t, "status": status,
        "origin": origin, "md_file": md_file,
        "en_url": e.get("en_url", ""), "ko_url": e.get("ko_url", ""),
        "published_at": e.get("published_at", ""),
    }


def planned_datetime(data: dict, date_str: str) -> datetime:
    p = planned(data, date_str)
    h, m = _parse_time(p["time"])
    d = datetime.strptime(post_date(date_str), "%Y-%m-%d")
    return datetime(d.year, d.month, d.day, h, m)


# ── 하루 여러 글: 글 키 = '날짜' 또는 '날짜#번호' (시간대가 서로 달라야 함) ──────
def post_date(key: str) -> str:
    """글 키('2026-06-20' 또는 '2026-06-20#2')에서 날짜 부분만 반환."""
    return (key or "").split("#", 1)[0]


def day_keys(data: dict, date_str: str) -> list:
    """그 날짜에 속한 모든 글 키를 시간 순으로. ('date' 와 'date#N' 모두 포함)"""
    date_str = post_date(date_str)
    keys = [k for k in data["entries"] if post_date(k) == date_str]
    return sorted(keys, key=lambda k: (data["entries"][k].get("time") or "99:99", k))


def has_explicit_post(data: dict, date_str: str) -> bool:
    """그 날짜에 개별 지정된(주제 있는) 글이 하나라도 있는지."""
    return any(data["entries"][k].get("topic") for k in day_keys(data, date_str))


def new_post_key(data: dict, date_str: str) -> str:
    """그 날짜에 새 글을 추가할 빈 키. 첫 글은 'date', 이후 'date#2','date#3'…"""
    date_str = post_date(date_str)
    if date_str not in data["entries"]:
        return date_str
    i = 2
    while f"{date_str}#{i}" in data["entries"]:
        i += 1
    return f"{date_str}#{i}"


def day_times(data: dict, date_str: str, exclude_key: str = None) -> set:
    """그 날짜 글들의 발행 시각 집합(중복 방지 검사용). exclude_key는 제외."""
    out = set()
    for k in day_keys(data, date_str):
        if k == exclude_key:
            continue
        t = (data["entries"][k].get("time") or "").strip()
        if t:
            out.add(t)
    return out


# ══════════════════════════════════════════════════════════════════════════════
#  LLM — 글 생성
# ══════════════════════════════════════════════════════════════════════════════

GEN_SYSTEM = (
    "당신은 한국 여행·전통예술 전문 블로거입니다. "
    "주어진 주제로 한국어와 영어 두 버전의 블로그 글을 작성합니다. "
    "정보는 정확하고 구체적이며, 과장 없이 현지 팁과 실용 정보를 담습니다. "
    "반드시 지정된 JSON 형식 하나만 출력하고, 그 외 설명·인사·코드펜스는 절대 쓰지 마세요."
)


def _build_gen_prompt(topic: str, photo_names, blog_hint: str, refs: str = "") -> str:
    n = len(photo_names)
    if n:
        photo_lines = "\n".join(
            f"  {i+1}. {name}" for i, name in enumerate(photo_names)
        )
        img_rule = (
            f"- 본문에는 사진 자리표시자를 <!-- IMAGE_1 alt=\"...\" --> 형식으로 넣으세요. "
            f"사진은 총 {n}장이며, 본문 흐름에 맞게 IMAGE_1 ~ IMAGE_{n} 을 적절히 배치합니다.\n"
            f"- 영어 본문(body_en)의 alt는 영어로, 한국어 본문(body_ko)의 alt는 한국어로 작성하세요.\n"
            f"- 사진 파일 목록(순서=번호):\n{photo_lines}\n"
        )
    else:
        img_rule = (
            "- 이번 글에는 사진이 없습니다. IMAGE 자리표시자는 넣지 마세요.\n"
        )

    refs = (refs or "").strip()
    refs_block = ""
    if refs:
        refs_block = (
            f"\n참고 방향(반드시 반영): 아래 자료/사이트의 내용과 관점을 참고해 작성하세요.\n"
            f"  {refs}\n"
            f"(참고처의 사실·최신 정보를 우선하되, 본문에 출처 URL을 그대로 노출하지는 마세요.)\n"
        )

    return f"""블로그 성격: {blog_hint}
오늘 작성할 주제: "{topic}"
{refs_block}
아래 JSON 형식으로만 답하세요(키 순서·이름 그대로, 값만 채움):

{{
  "en_title": "영어 제목 (60자 이내, SEO 친화적)",
  "ko_title": "한국어 제목",
  "en_meta": "영어 검색 설명 (150자 이내)",
  "ko_meta": "한국어 검색 설명 (150자 이내)",
  "en_slug": "english-url-slug-en",
  "ko_slug": "korean-topic-romanized-ko",
  "en_labels": ["English", "Travel"],
  "ko_labels": ["한국어", "Travel"],
  "category": "Travel",
  "body_en": "영어 본문 HTML",
  "body_ko": "한국어 본문 HTML"
}}

작성 규칙:
- body_en / body_ko 는 <p>, <h2>, <strong> 등 기본 HTML 태그만 사용한 본문입니다(<html><body> 없이 본문 조각만).
- 각 본문은 도입부 + 소제목(h2) 3~5개 + 실용정보(교통·운영시간·요금·현지팁) + 마무리로 구성하세요.
- slug 는 영문 소문자·하이픈만, 끝에 -en / -ko 접미사를 붙입니다.
- 두 언어는 같은 내용을 각 언어 독자에 맞게 자연스럽게 쓰되 직역투를 피합니다.
{img_rule}
JSON 외 다른 텍스트는 절대 출력하지 마세요."""


_VALID_ESC = set('"\\/bfnrtu')


def _fix_bad_escapes(s: str) -> str:
    """JSON 문자열 안의 잘못된 역슬래시 이스케이프(\\x 등)를 \\\\ 로 보정.
    LLM이 HTML/경로를 그대로 넣어 생기는 'Invalid \\escape' 오류 방지용."""
    out = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n:
            nxt = s[i + 1]
            if nxt in _VALID_ESC:
                out.append(c); out.append(nxt); i += 2; continue
            out.append("\\\\"); i += 1; continue
        out.append(c); i += 1
    return "".join(out)


def _loosen_json(s: str) -> str:
    """LLM이 흔히 내는 비표준 JSON 보정 — 주석·트레일링 콤마 제거 + 누락 콤마 삽입.
    'Expecting value'(트레일링 콤마)·'Expecting property name'(객체 사이 콤마 누락) 등을 막습니다."""
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)      # /* 블록 주석 */
    s = re.sub(r"(?m)//.*$", "", s)                   # // 줄 주석
    # 배열 원소(객체/배열) 사이에 콤마가 빠진 경우 삽입: }{  ]{  }[  ][
    s = re.sub(r"([}\]])(\s*)([{\[])", r"\1,\2\3", s)
    # 같은 객체 안에서 줄바꿈으로 이어진 키-값 사이 콤마 누락: "...."\n  "key"
    s = re.sub(r'("|\d|true|false|null)(\s*\n\s*)(")', r"\1,\2\3", s)
    s = re.sub(r",(\s*[}\]])", r"\1", s)              # 위 보정으로 생긴/원래의 트레일링 콤마 정리
    return s


def _escape_control_chars_in_strings(s: str) -> str:
    """JSON 문자열 값 '안'에 이스케이프 안 된 개행·탭·낱개 따옴표가 그대로 섞여 있으면
    (LLM이 긴 텍스트를 줄바꿈 포함해서 그냥 박아넣거나, HTML 태그 안에 실수로 따옴표를
    흘리는 경우 — 예: '<h3">입장료...') 파싱이 깨진다.
    문자열 안에서만: 개행/탭은 \\n/\\r/\\t로 이스케이프. 따옴표는 만나는 즉시 문자열을
    끝내지 않고, 그 뒤(공백 건너뛰고) 첫 글자가 JSON 구조상 문자열이 끝나는 자리에 올
    만한 문자(, : } ] 또는 끝)일 때만 진짜 종료로 보고, 아니면 값 속에 섞여 들어간
    낱개 따옴표로 보고 이스케이프해 문자열을 계속 이어간다.
    (따옴표 밖 구조는 건드리지 않음, 이미 있는 이스케이프 시퀀스는 그대로 통과)"""
    out = []
    in_str = False
    escape = False
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if in_str:
            if escape:
                out.append(ch)
                escape = False
                i += 1
                continue
            if ch == "\\":
                out.append(ch)
                escape = True
                i += 1
                continue
            if ch == '"':
                j = i + 1
                while j < n and s[j] in " \t\r\n":
                    j += 1
                nxt = s[j] if j < n else ""
                if nxt in ",:}]" or nxt == "":
                    in_str = False
                    out.append(ch)
                else:
                    out.append('\\"')
                i += 1
                continue
            if ch == "\n":
                out.append("\\n"); i += 1; continue
            if ch == "\r":
                out.append("\\r"); i += 1; continue
            if ch == "\t":
                out.append("\\t"); i += 1; continue
            out.append(ch)
            i += 1
        else:
            if ch == '"':
                in_str = True
            out.append(ch)
            i += 1
    return "".join(out)


def _fix_malformed_json_keys(s: str) -> str:
    """작은 로컬 모델이 JSON 속성 이름을 깨뜨리는 흔한 패턴들을 보정.
    한 줄에 매치되는 온갖 변형 — *hook*: / *title_ko: / Him": / strong": / hook: (따옴표
    전혀 없음) 등 — 을 하나의 정규식으로 "속성 이름":  형태로 통일한다. 이미 정상인
    "hook": 같은 줄도 같은 결과로 재작성되므로(무해한 되풀이) 안전하다.
    (모델이 원래 의도한 키 이름(예: hook)을 다른 단어로 통째로 잘못 써서 복원할 수는
    없지만, 유효한 JSON으로만 만들면 그 편은 해당 필드 없이 나머지 필드로 살아남는다 —
    호출부가 전부 .get(key, 기본값)으로 읽으므로 필드 하나 없어도 안전.)
    다음 값이 문자열/배열/객체로 시작할 때만 매치해(값 바로 앞 줄 오탐 방지) 문자열
    '값' 내용은 거의 건드리지 않는다. 그 외 객체 사이에 낀 의미없는 문자열 토큰
    (예: "    "    ",)은 배열 구조를 깨뜨리므로 제거."""
    s = re.sub(r'(?m)^(\s*)\*{0,2}\s*"?(\w+)"?\s*\*{0,2}(\s*:\s*)(?=["\[{])', r'\1"\2"\3', s)
    # 콜론·글자 없이 따옴표·쉼표·공백만으로 이루어진 줄(= 진짜 키/값이 아닌 잡음)이
    # 바로 다음 객체({) 앞에 끼어 있으면 배열 구조가 깨지므로 그 줄째로 제거.
    s = re.sub(r'(?m)^[",\s]*"[",\s]*\n(?=\s*\{)', '', s)
    return s


def _save_json_fail_debug(blob: str) -> None:
    """JSON 파싱이 끝내 실패하면 원본을 남겨 다음에 원인을 바로 볼 수 있게 한다
    (최근 10개만 보관, 그 이상은 오래된 것부터 정리)."""
    try:
        debug_dir = SCRIPT_DIR / "_json_debug"
        debug_dir.mkdir(exist_ok=True)
        fname = debug_dir / f"fail_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.txt"
        fname.write_text(blob, encoding="utf-8")
        files = sorted(debug_dir.glob("fail_*.txt"), key=lambda p: p.stat().st_mtime)
        for old in files[:-10]:
            old.unlink(missing_ok=True)
    except Exception:
        pass


def _extract_json(text: str) -> dict:
    """LLM 출력에서 JSON 본문만 추출해 파싱 (이스케이프·주석·트레일링 콤마·문자열 안
    개행 등을 자동 보정). 전부 실패하면 원본을 _json_debug/에 남기고 예외를 올린다."""
    text = text.strip()
    # 코드펜스 제거
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text.strip())
    # 첫 { 부터 마지막 } 까지
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("LLM 응답에서 JSON을 찾지 못했습니다.")
    blob = text[start:end + 1]
    blob = _fix_malformed_json_keys(blob)
    escaped = _escape_control_chars_in_strings(blob)
    last = None
    for cand in (blob, _fix_bad_escapes(blob), _loosen_json(blob),
                 _loosen_json(_fix_bad_escapes(blob)), escaped, _loosen_json(escaped),
                 _loosen_json(_fix_bad_escapes(escaped))):
        try:
            return json.loads(cand)
        except json.JSONDecodeError as e:
            last = e
    _save_json_fail_debug(blob)
    raise last


def _ollama_generate(settings: dict, prompt: str, log=print, system: str = GEN_SYSTEM) -> str:
    url = settings["ollama_url"].rstrip("/") + "/api/chat"
    # 이번 PC에서 실제로 쓸 모델을 정함(설정 모델이 없으면 자동 대체).
    model = _prepare_model(settings, log)
    if not model:
        raise RuntimeError(
            "사용할 수 있는 Ollama 모델이 없습니다. [⚙️ 설정]의 '모델 확인/받기'로 "
            "모델을 내려받거나, LLM을 'Claude'로 바꾸세요.")
    # 작은 모델은 본문이 일찍 끊기기 쉬워 출력 토큰을 더 넉넉히 준다.
    num_predict = 4000 if _is_small_model(settings) else 3000

    def _call(use_think):
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            # format:json 은 일부 모델에서 JSON 뒤에 공백을 무한 생성하는 버그가 있어
            # 사용하지 않고, _extract_json()의 보정 파서로 처리합니다.
            "options": {"temperature": 0.75, "num_ctx": 8192, "num_predict": num_predict},
            # 연속 생성 사이 모델 언로드(기본 5분)로 인한 빈 응답을 방지 — 30분 유지.
            "keep_alive": "30m",
        }
        # ★추론(thinking) 모델은 추론에 토큰을 다 써 content가 비기 쉬우므로 think를 끔.
        #   추론을 모르는 모델엔 think 키를 보내지 않음(일부는 400 오류를 냄).
        if use_think is not None:
            payload["think"] = use_think
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=1200) as resp:
            return json.loads(resp.read().decode("utf-8"))

    use_think = False if settings.get("_model_thinks") else None
    # 빈 응답(추론 소진·언로드·일시 과부하 등)이면 잠깐 쉬고 최대 3회까지 재요청
    content = ""
    for attempt in range(3):
        try:
            out = _call(use_think)
        except urllib.error.HTTPError as e:
            # think 파라미터를 거부하는 모델이면 think 없이 한 번 더.
            if use_think is not None and e.code == 400:
                use_think = None
                out = _call(use_think)
            else:
                raise
        msg = out.get("message", {})
        content = (msg.get("content") or "").strip()
        if not content:
            # think 파라미터가 무시된 경우라도, 추론 텍스트에서 JSON을 건짐
            content = (msg.get("thinking") or "").strip()
        if content:
            return content
        log(f"      ↻ 빈 응답 — 재요청({attempt + 1}/3)")
        time.sleep(3)
    return content


def ensure_ollama_running(settings: dict, log=print) -> bool:
    """Ollama 서버가 떠 있는지 확인하고, 없으면 'ollama serve'를 띄웁니다."""
    base = settings["ollama_url"].rstrip("/")
    try:
        urllib.request.urlopen(base + "/api/tags", timeout=3)
        return True
    except Exception:
        pass
    log("   ⏳ Ollama 서버 시작 중...")
    try:
        creationflags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        subprocess.Popen(
            ["ollama", "serve"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
    except FileNotFoundError:
        log("   ❌ ollama 명령을 찾을 수 없습니다. Ollama가 설치돼 있는지 확인하세요.")
        return False
    for _ in range(30):
        time.sleep(1)
        try:
            urllib.request.urlopen(base + "/api/tags", timeout=3)
            log("   ✅ Ollama 서버 준비 완료")
            return True
        except Exception:
            continue
    log("   ❌ Ollama 서버가 시작되지 않았습니다.")
    return False


# ── 모델 자동 감지 / 대체 / 다운로드 ───────────────────────────────────────────
# 품질 우선순위(좋은 것 → 작은 것). 설정한 모델이 없을 때 설치된 것 중 첫 일치를 사용.
MODEL_PREFERENCE = [
    "gemma4:26b", "gemma4",
    "gemma3:27b", "gemma3:12b", "gemma3:9b", "gemma3",
    "qwen3:32b", "qwen3:30b", "qwen3:14b", "qwen2.5:14b", "qwen2.5:7b",
    "llama3.1:8b", "llama3:8b",
    "gemma3:4b", "gemma3n:e4b", "gemma3n", "gemma3n:e2b",
]
# 설치된 모델이 하나도 없을 때 자동으로 받을 추천 모델(품질/용량 균형, 약 8GB).
RECOMMENDED_PULL = "gemma3:12b"
# 추론(thinking)을 지원하는 모델군 — think:false 로 추론을 꺼서 빈 응답을 막음.
# 그 외 모델엔 think 파라미터를 보내지 않음(일부 모델은 400 오류를 냄).
_THINKING_HINTS = ("gemma4", "qwen3", "deepseek-r1", "magistral", "qwq")
# 이 규모(B) 미만이면 '작은 모델'로 보고 분량 확보를 위해 재시도를 늘림.
_SMALL_MODEL_B = 13


def _installed_models(settings: dict) -> list:
    """Ollama에 설치된 모델 이름 목록. 실패 시 빈 목록."""
    try:
        base = settings["ollama_url"].rstrip("/")
        with urllib.request.urlopen(base + "/api/tags", timeout=5) as r:
            data = json.loads(r.read().decode("utf-8"))
        return [m.get("name", "") for m in data.get("models", []) if m.get("name")]
    except Exception:
        return []


def _match_installed(want: str, installed: list) -> str:
    """원하는 모델명이 설치돼 있는지(태그 생략 매칭 포함) 확인해 실제 이름을 반환."""
    if not want:
        return ""
    if want in installed:
        return want
    if ":" not in want:                       # 예: "gemma4" → "gemma4:latest"
        for m in installed:
            if m.split(":")[0] == want:
                return m
    return ""


def resolve_ollama_model(settings: dict, log=print) -> str:
    """설정한 모델이 있으면 그대로, 없으면 설치된 것 중 가장 좋은 것으로 자동 대체.
    아무 모델도 없으면 빈 문자열을 반환(상위에서 다운로드 안내)."""
    installed = _installed_models(settings)
    want = settings.get("ollama_model", "")
    hit = _match_installed(want, installed)
    if hit:
        return hit
    for cand in MODEL_PREFERENCE:             # 선호 체인에서 설치된 최상위
        hit = _match_installed(cand, installed)
        if hit:
            log(f"   ⚠️ '{want}' 모델이 없어 '{hit}'(으)로 자동 대체합니다.")
            return hit
    if installed:                             # 추천엔 없지만 뭐라도 있으면 사용
        log(f"   ⚠️ 추천 모델이 없어 설치된 '{installed[0]}'(으)로 시도합니다(품질이 낮을 수 있음).")
        return installed[0]
    return ""


def _model_param_b(settings: dict, model: str) -> float:
    """모델의 파라미터 규모(B 단위)를 추정. 알 수 없으면 0.0."""
    try:
        base = settings["ollama_url"].rstrip("/")
        with urllib.request.urlopen(base + "/api/tags", timeout=5) as r:
            data = json.loads(r.read().decode("utf-8"))
        for m in data.get("models", []):
            if m.get("name") == model:
                ps = (m.get("details", {}) or {}).get("parameter_size", "")
                num = re.findall(r"[\d.]+", ps)
                return float(num[0]) if num else 0.0
    except Exception:
        pass
    return 0.0


def _prepare_model(settings: dict, log=print) -> str:
    """이번 실행에서 쓸 실제 모델을 한 번 정하고, 규모/추론여부를 settings에 캐시.
    설정의 모델명이 바뀌면 다시 계산한다."""
    want = settings.get("ollama_model", "")
    if settings.get("_resolved_model") and settings.get("_resolved_for") == want:
        return settings["_resolved_model"]
    model = resolve_ollama_model(settings, log)
    if not model:
        return ""
    settings["_resolved_model"] = model
    settings["_resolved_for"] = want
    settings["_model_b"] = _model_param_b(settings, model)
    settings["_model_thinks"] = any(h in model.lower() for h in _THINKING_HINTS)
    if 0 < settings["_model_b"] < _SMALL_MODEL_B:
        log(f"   ℹ️ 작은 모델({model}, ~{settings['_model_b']:.0f}B) 감지 — "
            f"분량 확보를 위해 소주제 재시도를 늘립니다.")
    return model


def _is_small_model(settings: dict) -> bool:
    return 0 < settings.get("_model_b", 0) < _SMALL_MODEL_B


def pull_model(model: str, log=print, progress=None) -> bool:
    """`ollama pull <model>` 로 모델을 내려받습니다(진행 로그 출력)."""
    log(f"   ⬇️ '{model}' 모델 다운로드 시작 — 수 GB라 시간이 걸립니다. 창을 닫지 마세요.")
    try:
        creationflags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        proc = subprocess.Popen(
            ["ollama", "pull", model],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            creationflags=creationflags,
        )
    except FileNotFoundError:
        log("   ❌ ollama 명령을 찾을 수 없습니다. Ollama가 설치돼 있는지 확인하세요.")
        return False
    last = ""
    for line in proc.stdout:                   # 진행 상황을 한 줄씩 로그로
        line = line.strip()
        if line and line != last:
            log(f"      {line}")
            last = line
    proc.wait()
    if proc.returncode == 0:
        log(f"   ✅ '{model}' 다운로드 완료")
        return True
    log(f"   ❌ '{model}' 다운로드 실패(코드 {proc.returncode})")
    return False


def model_status(settings: dict, log=print) -> dict:
    """현재 PC의 모델 상태 점검(설치 목록·사용할 모델·다운로드 필요 여부)."""
    if not ensure_ollama_running(settings, log):
        return {"ollama": False, "installed": [], "use": "", "need_pull": ""}
    installed = _installed_models(settings)
    use = resolve_ollama_model(settings, log)
    return {
        "ollama": True,
        "installed": installed,
        "use": use,
        "need_pull": "" if use else RECOMMENDED_PULL,
    }


def _claude_generate(settings: dict, prompt: str, log=print,
                     system: str = GEN_SYSTEM, max_tokens: int = 4000) -> str:
    key = settings.get("claude_api_key", "").strip()
    if not key:
        raise RuntimeError("Claude API 키가 설정되지 않았습니다. 설정에서 키를 입력하세요.")
    try:
        import anthropic
    except ImportError:
        raise RuntimeError(
            "anthropic 패키지가 없습니다. 'pip install anthropic' 후 다시 시도하세요."
        )
    client = anthropic.Anthropic(api_key=key)
    msg = client.messages.create(
        model=settings.get("claude_model", "claude-opus-4-8"),
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(block.text for block in msg.content if block.type == "text")


def _complete(settings: dict, prompt: str, log=print, system: str = GEN_SYSTEM) -> str:
    """선택된 엔진(gemma4/Claude)으로 프롬프트를 실행해 텍스트를 반환."""
    engine = settings.get("llm", "gemma4")
    if engine == "claude":
        return _claude_generate(settings, prompt, log, system)
    if not ensure_ollama_running(settings, log):
        raise RuntimeError("Ollama 서버를 사용할 수 없습니다.")
    return _ollama_generate(settings, prompt, log, system)


# ── 다단계 생성 (개요 → 소주제별 본문 → 통합) ────────────────────────────────
DEFAULT_SECTIONS = 5

OUTLINE_SYSTEM = (
    "당신은 한국을 찾는 외국인 관광객을 돕는 여행·전통예술 전문 에디터입니다. "
    "깊이 있는 글의 '개요'를 설계합니다. 지정한 JSON 하나만 출력하고 그 외 텍스트는 쓰지 마세요."
)
SECTION_SYSTEM = (
    "당신은 한국 여행 전문 작가입니다. 한 소주제에 대해 외국인 관광객에게 "
    "실질적으로 도움이 되는, 구체적이고 사려 깊은 본문을 한국어와 영어로 작성합니다. "
    "막연한 미사여구 없이 사실·수치·요령을 담고, 한국어도 영어와 동일한 깊이로 씁니다. "
    "지정한 JSON 하나만 출력하세요."
)


def _identity(settings: dict) -> str:
    """이 블로그의 색깔(정체성). 없으면 blog_hint로 폴백."""
    return (settings.get("blog_identity") or settings.get("blog_hint") or "").strip()


# JSON 안전 규칙(짧게) — 값 안의 큰따옴표로 JSON이 깨지는 사고 방지
JSON_SAFE = "제목·문구 안에 큰따옴표(\")는 쓰지 말고 「 」 또는 ' 를 쓰세요."


# 차별화 규칙(짧게) — 흔한 주제를 피하고 이 블로그만의 각도를 강제
DIFF_RULE = (
    "차별화: 누구나 쓰는 뻔한 관광 정보 말고, 전통예술·공예·장인의 덜 알려진 매력적인 각도를 고르세요."
)

# 사실 정확성(할루시네이션 방지) — 검증 불가 단정 금지
FACT_RULE = (
    "★사실 정확성: 직접 확인되지 않은 구체 정보를 단정하지 마세요. 특히 '지역 대표 메뉴·시그니처·"
    "유명 맛집·특정 상호·가격·운영시간'은 [검증된 현지 정보]나 참고자료에 없으면 쓰지 말고, "
    "'한식·백숙 등 다양한 식당이 있다'처럼 안전한 일반화로 서술하세요. 없는 가게·메뉴·축제를 지어내지 마세요."
)


def _refs_block(refs: str, cap: int = 600) -> str:
    refs = (refs or "").strip()
    if not refs:
        return ""
    # 프롬프트가 과도하게 길면 gemma 출력이 불안정(빈 응답)해지므로 길이 제한
    if len(refs) > cap:
        refs = refs[:cap].rsplit(" ", 1)[0] + " …"
    return (f"\n참고 방향(반드시 반영): 아래 자료/사이트의 내용·관점을 참고하세요.\n  {refs}\n"
            f"(참고처의 사실·최신 정보를 우선하되 본문에 URL은 노출하지 마세요.)\n")


def _series_block(series_ctx) -> str:
    """시리즈 맥락을 프롬프트용 안내문으로."""
    if not series_ctx:
        return ""
    s = series_ctx
    nxt = (s.get("next") or "").strip()
    prv = (s.get("prev") or "").strip()
    lines = [
        f"\n[시리즈 맥락] 이 글은 '{s.get('title','')}' 시리즈의 "
        f"{s.get('index','?')}/{s.get('total','?')}편입니다.",
        f"핵심 SEO 키워드: {s.get('keyword','')}",
    ]
    if s.get("hook"):
        lines.append(f"이 편의 후킹 포인트: {s.get('hook')}")
    if prv:
        lines.append(f"이전 편 제목: {prv} (도입부에서 자연스럽게 연결 가능)")
    if nxt:
        lines.append(f"다음 편 제목: {nxt} (맺음말에서 궁금증을 남기며 예고할 것)")
    return "\n".join(lines) + "\n"


def _past_titles_block(past: list) -> str:
    """과거 발행/생성 제목 목록을 '중복 금지' 프롬프트 블록으로."""
    if not past:
        return ""
    lines = []
    for r in past:
        parts = []
        if r.get("ko"):
            parts.append(r["ko"])
        if r.get("en"):
            parts.append(r["en"])
        if not parts and r.get("topic"):
            parts.append(r["topic"])
        if parts:
            lines.append("  - " + " / ".join(parts))
    if not lines:
        return ""
    block = "\n".join(lines)
    return (
        f"\n[이미 발행·생성된 글 제목 목록 — 아래 제목과 같거나 비슷한 제목·내용·각도는 절대 반복하지 마세요.\n"
        f"핵심 검색 키워드가 겹치면(예: 같은 장소·같은 주제를 다른 표현으로) 구글이 두 글을 서로 경쟁시켜 "
        f"둘 다 순위가 떨어집니다(키워드 자기잠식) — 제목 문구가 달라도 다루는 핵심 키워드/주제가 "
        f"이미 있다면 다른 각도나 다른 소재를 고르세요]\n"
        f"{block}\n"
        f"→ 위 목록에 없는 새로운 각도, 새로운 소재, 새로운 제목으로 작성해야 합니다.\n"
    )


_WORD_POST_MARKERS = ("k-word dictionary", "단어 사전", "한국 문화 단어")


def _is_word_post(topic: str, refs: str = "") -> bool:
    """이 글이 '한국 문화 단어 사전' 시리즈 편인지(주제/참고에 마커가 있으면 True).
    단어 글은 장소가 없어 사진 매칭이 엉뚱하므로, 라이브러리 매칭을 끄고 타이틀 카드를 쓴다."""
    blob = f"{topic or ''} {refs or ''}".lower()
    return any(m in blob for m in _WORD_POST_MARKERS)


_CONN_HEADING_KEYS = ("연결", "연관", "관련 단어", "관련어", "connected", "related", "linked")


def _is_connections_heading(*headings) -> bool:
    """소주제 제목이 '연결된 단어들' 같은 연관어 섹션인지(연관 단어 카드를 넣을 자리)."""
    blob = " ".join((h or "") for h in headings).lower()
    return any(k in blob for k in _CONN_HEADING_KEYS)


def _lead_word(title: str) -> str:
    """제목에서 앞쪽 '단어'만 뽑는다(— : · ( [ 뜻 meaning 앞까지). 카드 폴백용."""
    t = (title or "").strip()
    for sep in ("—", "–", ":", "·", "|", "(", "[", " 뜻", " meaning", " Meaning"):
        if sep in t:
            t = t.split(sep)[0]
    return t.strip(" -—:·|")


def _truncate_at_sentence(text: str, max_len: int = 90) -> str:
    """문장 중간에서 뚝 끊기지 않도록, 상한 길이 안의 마지막 문장부호까지만 자른다.
    문장부호가 없으면 마지막 단어 경계에서 자르고 '…'을 붙인다(카드 부제용)."""
    text = (text or "").strip()
    if len(text) <= max_len:
        return text
    cut = text[:max_len]
    for i in range(len(cut) - 1, -1, -1):
        if cut[i] in ".!?。":
            return cut[:i + 1]
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > 0 else cut).rstrip(",，、") + "…"


def _outline_prompt(topic, refs, identity, n_photos, n_sections, series_ctx=None,
                    past=None, word_mode=False) -> str:
    kw = (series_ctx or {}).get("keyword", "")
    seo_kw = f"핵심 키워드 '{kw}' 를 제목·메타·슬러그·도입부·소제목 중 자연스럽게 반영." if kw else \
             "주제의 핵심 검색 키워드를 제목·메타·슬러그에 자연스럽게 반영."
    word_block = (
        '  "word_ko": "다루는 단어를 화면에 표시할 형태. 둘 이상이면 \'상수 / 하수\'처럼 / 로 구분(조사 와·과·및·그리고 금지). FOV처럼 영어에서 온 말은 음차하지 말고 영문 그대로.",\n'
        '  "word_en": "영어권에서 실제 쓰는 전문용어·번역어가 있으면 그것(예: 상수/하수 → \'Stage Right / Stage Left\', 여백 → \'Negative Space\', FOV → \'Field of View\'). 영어 대응어가 없는 순수 고유어면 빈 문자열. 음차(에프오브이 등) 절대 금지.",\n'
        '  "related_words": [{"ko": "연관 단어", "en": "영어 표기(대응어 없으면 빈 문자열)", '
        '"desc": "한 줄 설명(한국어 10~16자)", "desc_en": "같은 뜻 한 줄 설명(영어, 10~16단어)"}],\n'
        ) if word_mode else ""
    word_rule = (
        "- 이 글은 단어 사전의 한 편입니다. 한 단어(또는 한 쌍)만 깊게 다루세요.\n"
        "- summary_ko/summary_en는 '이 단어는 ~를 뜻한다'는 직접 정의로 시작해 첫 문장에서 뜻을 바로 알게 하세요. "
        "'~를 알아보자/살펴보자/이해해보자' 같은 막연한 도입은 절대 금지(뜻부터).\n"
        "- related_words는 2~3개, 각각 desc(한국어)·desc_en(영어) 둘 다 포함(같은 뜻, 언어만 다름). "
        "마지막 소주제 제목에 '연결된 단어'를 넣어 그 단어들을 다루세요.\n"
        ) if word_mode else ""
    return f"""[이 블로그의 색깔]
{identity}

주제: "{topic}"
독자: 한국 문화·예술에 관심 있는 외국인(영어판) + 한국어 독자.
{_past_titles_block(past or [])}{_series_block(series_ctx)}{_refs_block(refs)}
{DIFF_RULE}

이 주제로 '깊이 있고 이 블로그만의 색깔이 살아있는' 글의 개요를 설계하세요. 아래 JSON만 출력:

{{
  "en_title": "영어 제목(60자 이내): 핵심 검색 키워드를 맨 앞에 두고 Guide/Tips/Highlights 같은 검색 의도 반영(추상 에세이형 금지)",
  "ko_title": "한국어 제목: 핵심 키워드(장소·소재)를 앞에 두고 구체적으로. 예) 'OO 가는 법·관람 팁'(막연한 에세이형 금지)",
  "en_meta": "영어 검색설명(150자 이내, 키워드+클릭할 이유=가치 제안)",
  "ko_meta": "한국어 검색설명(150자 이내, 키워드+클릭할 이유)",
  "en_slug": "english-keyword-slug-en",
  "ko_slug": "keyword-romanized-ko",
  "en_labels": ["English", "Travel"],
  "ko_labels": ["한국어", "Travel"],
  "category": "Travel",
  "location": "지도에 표시할 실제 장소명(한국어, 예: 울산 주전 몽돌해변). 특정 장소가 없는 주제면 빈 문자열.",
  "summary_en": "1~2 sentence TL;DR that answers the reader's core question up front (inverted pyramid). Plain text, no tags.",
  "summary_ko": "본문 맨 위에 올 핵심 한 줄 요약 1~2문장: 결론·핵심 답을 먼저 제시(역피라미드). 텍스트만.",
{word_block}  "intro_en": "영어 '들어가는말' 3~4문장: 강한 후킹(질문/의외의 사실)으로 시작해 독자의 궁금증을 자극하고 이 글이 무엇을 해결해 주는지 약속. 태그 없이 텍스트만.",
  "intro_ko": "한국어 들어가는말 3~4문장(같은 의도).",
  "sections": [
    {{"en_heading": "...", "ko_heading": "...", "intent": "이 섹션에서 다룰 핵심(한국어 1문장)"}}
  ]
}}

규칙:
- sections 는 정확히 {n_sections}개. 문화·예술에 관심 있는 외국인에게 진짜 유용한 구성으로 고르세요:
  배경·의미/역사 / 예술·공예의 특징과 감상 포인트 / 장인·전승 이야기 / 직접 보고 체험하는 법 /
  찾아가는 법·실용정보 / 현지에서만 아는 팁 / 주변 문화 연계 / 가지 말아야 할 함정·실패담
  (무엇을 피해야 하는지, 대형 여행 플랫폼은 잘 안 다루는 솔직한 정보 — 실용적인 주제일 때만) 중에서.
- 시간이 지나면 바뀌는 정보(요금·운영시간·최근 개편)를 다룰 때는 "최신 확인 필요" 같은 표현으로
  최신성을 드러내되, 확인 안 된 구체 수치는 지어내지 마세요(아래 사실 정확성 규칙 우선).
- en_labels/ko_labels 는 글 내용에 맞는 '구체적인 태그' 4~6개(지역·소재·테마 등)로 채우세요.
  각각 첫 태그는 언어 태그(ko_labels는 "한국어", en_labels는 "English")로 시작합니다.
- location 은 글이 특정 장소를 다루면 그 장소명을 한국어로(지도 검색용), 아니면 빈 문자열로 두세요.
- summary_ko/summary_en 는 검색해서 들어온 독자가 첫 1~2문장에서 바로 답을 얻도록 핵심·결론을 먼저(역피라미드). 서론으로 미루지 마세요.
{word_rule}
- 들어가는말은 '후킹 → 공감 → 이 글이 주는 가치 약속' 흐름으로 독자를 끌어들이세요.
- 구글 SEO: {seo_kw} 제목은 사람들이 실제 검색할 구체적 키워드를 앞에 두세요(예: 'Suwon Hwaseong Fortress Travel Guide: History, Tips, and Highlights').
- {FACT_RULE}
{JSON_SAFE}
JSON 외 텍스트는 절대 출력하지 마세요."""


# ── 실용정보 표 + FAQ (SEO: 스니펫·체류시간↑) ────────────────────────────────
SEO_EXTRAS_SYSTEM = (
    "당신은 한국 여행 정보를 외국인에게 정확하게 정리해 주는 에디터입니다. "
    "확실하지 않은 운영시간·요금·날짜는 지어내지 말고 '방문 전 공식 안내 확인'으로 적습니다. "
    "지정한 JSON 하나만 출력하세요."
)


def _seo_extras_prompt(topic, location, refs, facts="") -> str:
    place = (location or topic or "").strip()
    facts_block = (facts + "\n") if (facts or "").strip() else ""
    return f"""주제: "{topic}"   장소: "{place}"
{facts_block}{_refs_block(refs)}
외국인 독자에게 도움이 되는 '실용 정보'와 'FAQ'를 한국어·영어로 작성하세요. 아래 JSON만 출력:

{{
  "tips_ko": "한국어 실용정보 HTML",
  "tips_en": "영어 실용정보 HTML",
  "faq_ko": "한국어 FAQ HTML",
  "faq_en": "영어 FAQ HTML"
}}

실용정보(tips) 규칙:
- 찾아가는 법(대중교통·가까운 역/정류장) · 추천 방문 시간대 · 주변에 함께 볼 곳 · 방문 팁(언어·준비물·무료 여부)을
  ★표나 목록이 아니라 **자연스러운 문단(<p>)으로 풀어서** 서술하세요(2~4문단). '구분/상세정보' 같은
  항목 나열식(표)이 아니라, 읽기 편한 안내 글로. 소제목이 필요하면 <strong>만 문단 안에 쓰세요.
- ★운영시간·입장료처럼 정확한 수치를 모르면 지어내지 말고 "방문 전 공식 사이트·현지 안내 확인"으로 적으세요(틀린 정보 절대 금지).
FAQ 규칙:
- 외국인이 실제로 궁금해할 질문 3~4개와 간결한 답. 질문은 <h3>, 답은 <p>.
  (예: 무료인가요? 어떻게 가나요? 언제 가면 좋나요? 사진 촬영 되나요?)
- 답은 사실에 근거하고, 모르면 일반적 안내로(추측성 단정 금지).
- HTML 태그는 <p><strong><h3> 만 사용(표·목록 태그 금지).
- {FACT_RULE}
{JSON_SAFE}
JSON 외 텍스트는 절대 출력하지 마세요."""


def _gen_seo_extras(topic, location, refs, settings, log, facts="") -> dict:
    """실용정보 표 + FAQ(한/영) 생성. 실패하면 빈 값(글은 그대로 진행)."""
    try:
        d = _extract_json(_complete(settings, _seo_extras_prompt(topic, location, refs, facts),
                                    log, SEO_EXTRAS_SYSTEM))
        return {k: (d.get(k) or "").strip() for k in ("tips_ko", "tips_en", "faq_ko", "faq_en")}
    except Exception as e:
        log(f"      ⚠️ 실용정보·FAQ 생략(생성 실패): {e}")
        return {"tips_ko": "", "tips_en": "", "faq_ko": "", "faq_en": ""}


CONCLUSION_SYSTEM = (
    "당신은 글을 깔끔하게 마무리하는 카피라이터입니다. 글의 '맺음말'을 한국어와 영어로 작성합니다. "
    "핵심을 짧게 정리하고 따뜻하게 마무리합니다. 지정한 JSON 하나만 출력하세요."
)


def _conclusion_prompt(topic, series_ctx) -> str:
    nxt_ko = (series_ctx or {}).get("next", "")
    nxt_en = (series_ctx or {}).get("next_en", "") or nxt_ko
    # ★다음 편 예고는 '실제 시리즈의 다음 편이 있을 때만' — 단독 글은 가짜 다음편을
    #   지어내지 않도록 명시적으로 금지합니다.
    tease = (f"- conc_ko 마지막에 다음 편 \"{nxt_ko}\" 을(를) 예고하며 궁금증을 남기는 한 문장을 넣으세요.\n"
             f"- conc_en 마지막에는 다음 편 \"{nxt_en}\" 을(를) 예고하세요(스포일러 없이 호기심만 자극)."
             if nxt_ko else
             "- 이 글은 시리즈가 아닙니다. '다음 편/다음 글/다음에 다룰 내용' 같은 예고는 절대 넣지 마세요.\n"
             "  존재하지 않는 후속 글이나 소재(예: 다음엔 카페 이야기 등)를 지어내지 말고, "
             "이 글 자체를 따뜻하게 마무리하세요.")
    return f"""글 주제: "{topic}"
이 글의 '맺음말'을 한국어와 영어로 작성하세요. 아래 JSON만 출력:

{{"conc_ko": "한국어 맺음말 HTML", "conc_en": "영어 맺음말 HTML"}}

작성 규칙:
- 각 언어 <p> 문단 2~3개.
- 본문 핵심을 한 문장으로 부드럽게 정리(요약)하고, 독자에게 실질적 격려/한 줄 팁을 더하세요.
{tease}
- 과장·상투적 표현 금지. <p>, <strong> 만 사용. <h2>·제목은 넣지 마세요(자동으로 붙습니다).
{JSON_SAFE}
JSON 외 텍스트는 절대 출력하지 마세요."""


def _gen_conclusion(topic, series_ctx, settings, log) -> dict:
    for attempt in range(2):
        try:
            d = _extract_json(_complete(settings, _conclusion_prompt(topic, series_ctx),
                                        log, CONCLUSION_SYSTEM))
            ko = (d.get("conc_ko") or "").strip()
            en = (d.get("conc_en") or "").strip()
            if ko and en:
                return {"conc_ko": ko, "conc_en": en}
        except Exception as e:
            log(f"      ⚠️ 맺음말 재시도({attempt + 1}/2): {e}")
    return {"conc_ko": "", "conc_en": ""}


# ── 사진 캡션 생성 ────────────────────────────────────────────────────────────
CAPTION_SYSTEM = (
    "당신은 사진 설명(캡션) 작가입니다. 파일명이 사진 내용을 설명합니다. "
    "각 사진에 어울리는 짧고 자연스러운 캡션을 한국어와 영어로 만듭니다. 지정한 JSON 하나만 출력하세요."
)


def _caption_prompt(topic, photo_names) -> str:
    n = len(photo_names)
    lines = "\n".join(f"  {i + 1}. {name}" for i, name in enumerate(photo_names))
    return f"""글 주제: "{topic}"
아래는 본문에 들어갈 사진들의 파일명입니다(파일명이 사진 내용을 설명함). 사진 순서대로
각 사진에 어울리는 짧은 캡션을 한국어·영어로 작성하세요. 아래 JSON만 출력:

{{"captions": [{{"ko": "한국어 캡션", "en": "English caption"}}]}}

사진 파일명(순서=번호):
{lines}

규칙:
- captions 는 사진 순서대로 정확히 {n}개.
- 각 캡션은 한 줄(한국어 12~25자, 영어 6~14단어). 사진 내용을 구체적이고 자연스럽게 묘사.
- 파일명의 번호·확장자는 빼고, 장소·대상·장면을 설명하세요.
{JSON_SAFE}"""


def _fallback_caption(name, idx=0):
    stem = Path(name).stem
    stem = re.sub(r"^\d+[_\-\.\s]*", "", stem).replace("_", " ").strip()
    en = stem if (stem and stem.isascii()) else f"Photo {idx + 1}"
    return {"ko": stem or "사진", "en": en}


def _clean_alt(s):
    return (s or "").replace('"', "'").replace("\n", " ").strip()


def _caption_from_filename(name) -> str:
    """파일명을 사진 설명(캡션)으로 다듬는다 — 사용자가 파일명에 적어둔 제목이 vision 모델의
    자동 캡션보다 정확하기 때문(예: '아펜젤러목사 동상.jpg'). 확장자·언더스코어·앞뒤 순번
    숫자를 정리('배제학당 역사박물관3' → '배제학당 역사박물관'). LLM 캡션 실패 시 폴백."""
    s = Path(name).stem.replace("_", " ")
    s = re.sub(r"^\d+[\-\.\s]*", "", s)   # 앞쪽 순번(01_, 1- 등)
    s = re.sub(r"\s*\d+$", "", s)          # 끝 순번(…박물관3 → …박물관)
    s = re.sub(r"\s+", " ", s).strip()
    return s.replace('"', "'")


_PHOTO_CAP_SYSTEM = ("당신은 사진 캡션 에디터입니다. 운영자가 적은 파일명(정확)과 AI 비전 분석"
                     "(참고)을 결합해 자연스러운 사진 설명을 만들어 지정 JSON 하나만 출력하세요.")


def _photo_caption_prompt(items) -> str:
    # 파일명은 순번 숫자를 뗀 깔끔한 형태로 넘긴다(LLM이 '역사박물관3'의 3을 이름 일부로
    # 착각해 캡션에 그대로 넣는 것 방지).
    lines = "\n".join(f"{i + 1}. 파일명: {_caption_from_filename(n)} / 사진 내용(참고): {c}"
                      for i, (n, c) in enumerate(items))
    return f'''아래 사진들에 어울리는 자연스러운 '사진 설명(캡션)'을 한 줄씩 만드세요.
- 파일명은 운영자가 직접 적은 정확한 제목입니다 — 대상·장소·인물 이름을 반드시 그대로 살리세요.
- '사진 내용(참고)'은 AI 비전 분석이라 틀릴 수 있습니다. 파일명과 어긋나면 파일명을 믿고,
  맞으면 시각적 특징(분위기·구도·빛·질감)을 살짝 곁들여 딱딱한 나열 대신 자연스럽게 쓰세요.
- 한국어 15~35자. 사진에 실제로 있을 법한 것만(없는 내용 지어내기 금지).
{lines}

아래 JSON만 출력(설명·코드펜스 없이): {{"captions": ["1번 사진 캡션", "2번 사진 캡션", ...]}}'''


def _describe_captions(items, settings, log=print) -> list:
    """[(파일명, 비전캡션)] → 파일명+비전을 결합한 자연스러운 사진 설명 리스트(순서 유지).
    실패하면 파일명 기반(_caption_from_filename)으로 폴백."""
    if not items:
        return []
    for attempt in range(2):
        try:
            d = _extract_json(_complete(settings, _photo_caption_prompt(items),
                                        log, _PHOTO_CAP_SYSTEM))
            caps = [str(c).strip() for c in (d.get("captions") or []) if str(c).strip()]
            if len(caps) >= len(items):
                return caps[:len(items)]
        except Exception as e:
            log(f"      ↻ 사진 설명 캡션 재시도({attempt + 1}/2): {e}")
    return [_caption_from_filename(n) for n, _ in items]


def _gen_caption_chunk(topic, names, settings, log) -> list:
    """사진 몇 장(소묶음)의 캡션을 생성. 실패 시 [] 반환."""
    for attempt in range(2):
        try:
            d = _extract_json(_complete(settings, _caption_prompt(topic, names),
                                        log, CAPTION_SYSTEM))
            caps = d.get("captions") or []
            if caps:
                return caps
        except Exception as e:
            log(f"      ⚠️ 캡션 묶음 재시도({attempt + 1}/2): {e}")
    return []


def _series_photo_folders(photo_dir: str) -> list:
    """시리즈 사진 폴더의 '편별 소재'가 될 하위 폴더 목록. 바로 아래에 사진이 있는 하위
    폴더가 있으면 그 폴더들(이름순)을 반환 — 시리즈처럼 상위 폴더 하나에 장소/소재별
    하위 폴더가 나뉜 구조를 편별로 1:1 매칭하는 데 씀. 하위 폴더가 없으면(사진이 바로
    photo_dir 안에 있는 경우) photo_dir 자신 하나만 담아 반환(기존처럼 전체 공유)."""
    try:
        import photo_library as pl
        subs = pl.list_subdirs(photo_dir)
    except Exception:
        return [photo_dir]
    subs = [s["path"] for s in subs if s.get("own", 0) > 0]
    return subs or [photo_dir]


def _best_matching_folder(text: str, folders: list):
    """편의 제목·키워드 텍스트(text)와 가장 잘 맞는 하위 폴더를 이름으로 찾는다.
    공백을 다 지운 뒤(복합어 띄어쓰기 차이 무시 — 예: 폴더 '태화강삼호지구 대나무숲'의
    '삼호지구'가 제목 '삼호지구의 초록빛 터널'과 띄어쓰기가 달라도 매칭되게) **가장 긴
    공통 부분 문자열 길이**를 우선 보고(지명이 실제로 겹치는지가 핵심), 그 외엔 전체
    문자열 유사도(SequenceMatcher.ratio)로 보조 판단한다.
    (2026-07-08 버그 수정 — 예전엔 편 순서·폴더 이름 가나다순으로 그냥 1:1 배정해서
    편 내용과 무관한 폴더가 배정되는 경우가 흔했음. 예: '주전 몽돌해변' 편에 '대왕암'
    폴더가 배정되는 식. 처음엔 공백 기준 단어 통째 포함 여부로 보정했으나, '태화강삼호
    지구'처럼 붙여 쓴 복합 지명은 부분만 겹쳐도 놓치는 경우가 있어 최장 공통 부분
    문자열 방식으로 다시 개선.)
    반환: (가장 잘 맞는 폴더 경로 또는 None, 매칭 점수)."""
    import difflib
    text_norm = re.sub(r"\s+", "", text or "")
    best, best_score = None, 0.0
    for f in folders:
        name_norm = re.sub(r"\s+", "", Path(f).name)
        sm = difflib.SequenceMatcher(None, name_norm, text_norm)
        lcs_len = sm.find_longest_match(0, len(name_norm), 0, len(text_norm)).size
        score = lcs_len * 10 + sm.ratio()
        if score > best_score:
            best_score, best = score, f
    return (best, best_score) if best_score > 0 else (None, 0.0)


def _describe_photos(photos: list, settings: dict, log=print, max_analyze: int = 6) -> str:
    """확보된 사진들이 '실제로 무엇을 찍었는지' 파악해 글쓰기 참고문(refs)에 덧붙일 텍스트로
    반환. 그동안 본문 생성이 topic/refs 텍스트만 보고 사진 내용은 전혀 모른 채 써서, 실제
    사진(예: 울산 주전항 풍경)과 무관한 소재(예: 민화)로 글이 나가는 문제가 있었음 — 이 함수가
    그 간극을 메운다. 라이브러리에 이미 태깅된 사진은 그 캡션을 재사용(빠름), 없으면 vision
    모델로 최대 max_analyze장까지 즉석 분석(느릴 수 있음). 실패해도 빈 문자열 반환(생략됨)."""
    if not photos:
        return ""
    try:
        import photo_library as pl
        import photo_vision as pv
    except Exception as e:
        log(f"   ⚠️ 사진 내용 분석 생략(모듈 없음): {e}")
        return ""
    paths = [str(p) for p in photos]
    try:
        cached = pl.photos_by_paths(paths)
    except Exception:
        cached = {}
    descs, analyzed = [], 0
    for p in photos:
        p = Path(p)
        row = cached.get(str(p).lower())
        cap = (row.get("auto_caption") or "").strip() if row else ""
        if not cap and analyzed < max_analyze:
            log(f"   👁 사진 내용 분석 중: {p.name}")
            hints = ({"folder_tags": row.get("folder_tags", ""), "place": row.get("place", ""),
                      "region": row.get("region", "")} if row else {})
            try:
                result = pv.caption(str(p), settings, hints=hints, log=log)
                cap = (result.get("caption_ko") or "").strip()
            except Exception as e:
                log(f"      ⚠️ 사진 분석 실패({p.name}): {e}")
            analyzed += 1
        if cap:
            descs.append(f"- {p.name}: {cap}")
    if not descs:
        return ""
    return ("[실제로 준비된 사진에 담긴 내용 — 이 사진들에 실제로 있는 것을 중심으로 소개·"
            "묘사·추천하세요. 사진에 없는 다른 소재를 억지로 끌어와 섞지 마세요.]\n"
            + "\n".join(descs))


# ── 사진 기반 글: 사진 개별 분석 + 주제 그룹핑 ────────────────────────────────
# (photo_dir이 지정된 글에서만 사용 — 각 사진의 실제 내용을 파악해 '같은 대상'끼리 묶고,
#  그 그룹을 각각 한 소주제로 삼아 사진↔글이 어긋나지 않게 한다. 2026-07-10)
def _file_md5(p) -> str:
    """파일 내용 해시(완전 동일한 중복 사진 제거용). 실패 시 None."""
    import hashlib
    try:
        h = hashlib.md5()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def _photo_caption_one(p, settings, cached, log, do_analyze=True) -> str:
    """사진 1장의 한국어 캡션 — 라이브러리 캐시 우선, 없으면 vision 즉석 분석."""
    p = Path(p)
    row = cached.get(str(p).lower())
    cap = (row.get("auto_caption") or "").strip() if row else ""
    if not cap and do_analyze:
        try:
            import photo_vision as pv
            hints = ({"folder_tags": row.get("folder_tags", ""), "place": row.get("place", ""),
                      "region": row.get("region", "")} if row else {})
            result = pv.caption(str(p), settings, hints=hints, log=log)
            cap = (result.get("caption_ko") or "").strip()
        except Exception as e:
            log(f"      ⚠️ 사진 분석 실패({p.name}): {e}")
    return cap


_GROUP_SYSTEM = ("당신은 사진 편집자입니다. 사진들을 '실제로 담긴 대상·장소'가 같은 것끼리 "
                 "그룹으로 묶어, 지정한 JSON 하나만 출력하세요.")


def _group_photos_prompt(topic, items) -> str:
    lines = "\n".join(f"{i + 1}. {name} — {cap}" for i, (name, cap) in enumerate(items))
    return f'''주제: "{topic}"
아래는 이 글에 넣을 사진들의 실제 내용(비전 분석 결과)입니다.
{lines}

이 사진들을 '실제로 담긴 대상·장소'가 같은 것끼리 그룹으로 묶으세요.
- 각 그룹은 글의 한 소주제가 됩니다(예: 건물 전경 / 특정 인물의 동상과 설명판 / 기념공원 등).
- 같은 대상의 사진들(예: 어떤 동상 + 그 동상 설명판)은 반드시 한 그룹으로 묶으세요.
- 건물 전체 전경·외관처럼 '대상 전체를 넓게 보여주는' 그룹은 is_overview를 true로 하세요
  (그런 그룹이 없으면 모두 false).
- 그룹 수는 사진 내용에 따라 자연스럽게(보통 2~4개). 모든 사진이 어느 한 그룹엔 속해야 합니다.
- ★각 그룹의 photo_numbers는 **전체를 넓게 보여주는 전경·전체 사진을 먼저, 부분·근접(클로즈업)·
  세부(설명판·현판 등) 사진을 나중에** 오도록 정렬하세요(전체 → 부분 흐름).
- ★label_ko/label_en은 단순 분류명("인물 흉상", "건물")이 아니라, 그 대상의 **의미·역할·가치를
  살린 격조 있는 소제목**으로 지으세요. 특히 인물이면 그의 업적·위상을 담아 격상시키세요.
  (예: 교회를 세운 목사들의 흉상 → "복음을 전한 개척자들" / "이 땅에 복음을 심은 선구자들",
   창립자 동상 → "배움의 터를 연 스승", 건물 전경 → "붉은 벽돌에 새겨진 신앙의 세월")

아래 JSON만 출력(설명·코드펜스 없이):
{{"groups":[{{"label_ko":"그룹 제목(한국어)","label_en":"group title (English)","is_overview":true,"photo_numbers":[1,2],"desc_ko":"이 그룹 사진들이 실제로 보여주는 것 한두 문장"}}]}}'''


# 파일명에 이런 낱말이 있으면 '설명판·안내문'(글자만 있는 판)으로 보고, 본문에서 빼고 글자만
# 판독(OCR)해 정보로 쓴다. ★기념비·기념탑·비석·동상·흉상은 '볼거리 조형물'이라 제외하지 않음
# (글자가 새겨져 있어도 사진 자체가 콘텐츠) — 키워드를 '설명/안내'로 좁게 유지.
_SIGN_KEYWORDS = ("설명", "안내")


def _is_signboard(name) -> bool:
    """파일명으로 '설명판·비석·안내문' 사진인지 판단."""
    return any(k in str(name) for k in _SIGN_KEYWORDS)


def _ocr_signboards(sign_photos, settings, log=print) -> str:
    """설명판·비석 사진들의 글자를 판독(vision OCR)해 본문 생성 근거로 쓸 텍스트 블록을 만든다.
    이 사진들은 본문에는 넣지 않고(밋밋한 글자판) 정보만 활용 — 판독 실패·글자 없음은 건너뜀."""
    try:
        import photo_vision as pv
    except Exception:
        return ""
    blocks = []
    for p in sign_photos or []:
        p = Path(p)
        log(f"   📖 설명판 글자 판독: {p.name}")
        try:
            txt = pv.ocr(str(p), settings, log=log)
        except Exception as e:
            log(f"      ⚠️ 판독 실패({p.name}): {e}")
            txt = ""
        if txt and len(txt) > 20:
            blocks.append(f"- [{_caption_from_filename(p.name)}]\n{txt[:900]}")
    if not blocks:
        return ""
    return ("[사진 속 설명판·비석에 실제로 적힌 내용 — 이 사실들을 본문에 자연스럽게 녹여 정확하게 "
            "서술하세요(그대로 베끼지 말고 요약·인용·활용). 여기 없는 사실은 지어내지 마세요.]\n"
            + "\n\n".join(blocks))


def _analyze_and_group_photos(photos, topic, settings, log=print, max_analyze: int = 12) -> list:
    """사진들을 ①완전 중복 제거 ②개별 vision 분석 ③주제 그룹핑 하여 반환.
    반환: [{label_ko,label_en,is_overview,desc_ko,photos:[Path],captions:[str]}]
      — is_overview(전경/전체) 그룹이 리스트 맨 앞으로 정렬됨(첫 사진 섹션용)."""
    photos = [Path(p) for p in photos]
    # 1) 완전 동일 파일(바이트 해시) 중복 제거
    seen, uniq = {}, []
    for p in photos:
        h = _file_md5(p)
        if h and h in seen:
            log(f"   🗑 중복 사진 제외: {p.name} (= {seen[h]})")
            continue
        if h:
            seen[h] = p.name
        uniq.append(p)
    photos = uniq
    if not photos:
        return []
    # 2) 각 사진 캡션 확보(캐시 우선)
    try:
        import photo_library as pl
        cached = pl.photos_by_paths([str(p) for p in photos])
    except Exception:
        cached = {}
    items, analyzed = [], 0
    for p in photos:
        has_cache = bool((cached.get(str(p).lower()) or {}).get("auto_caption"))
        do = has_cache or analyzed < max_analyze
        cap = _photo_caption_one(p, settings, cached, log, do_analyze=do)
        if do and not has_cache:
            analyzed += 1
            log(f"   👁 사진 분석: {p.name} → {cap[:30] or '(실패)'}")
        items.append((p.name, cap or "(내용 파악 실패)"))
    # 3) LLM 그룹핑
    groups = None
    prompt = _group_photos_prompt(topic, items)
    for attempt in range(3):
        try:
            d = _extract_json(_complete(settings, prompt, log, _GROUP_SYSTEM))
            if d.get("groups"):
                groups = d["groups"]
                break
        except Exception as e:
            log(f"      ↻ 사진 그룹핑 재시도({attempt + 1}/3): {e}")
    # 3.5) 파일명+비전을 결합한 '자연스러운 사진 설명(캡션)' 생성 — 화면에 보이는 사진 설명글로 씀
    disp = _describe_captions(items, settings, log)
    disp_of = {i: (disp[i] if i < len(disp) else _caption_from_filename(items[i][0]))
               for i in range(len(photos))}
    if not groups:   # 폴백: 전부 한 그룹
        return [{"label_ko": "사진", "label_en": "Photos", "is_overview": True, "desc_ko": "",
                 "photos": photos, "captions": [c for _, c in items],
                 "display": [disp_of[i] for i in range(len(photos))]}]
    # 4) 그룹 번호 → 사진 매핑
    cap_of = {i: items[i][1] for i in range(len(photos))}
    used, out = set(), []
    for g in groups:
        gidx = []
        for n in (g.get("photo_numbers") or []):
            if isinstance(n, int) and 1 <= n <= len(photos) and (n - 1) not in used:
                gidx.append(n - 1)
                used.add(n - 1)
        if not gidx:
            continue
        out.append({
            "label_ko": (g.get("label_ko") or "사진").strip(),
            "label_en": (g.get("label_en") or "Photos").strip(),
            "is_overview": bool(g.get("is_overview")),
            "desc_ko": (g.get("desc_ko") or "").strip(),
            "photos": [photos[i] for i in gidx],
            "captions": [cap_of[i] for i in gidx],
            "display": [disp_of[i] for i in gidx],
        })
    # 매핑 안 된 사진은 마지막 그룹에 흡수(하나도 없으면 새 그룹)
    leftover = [i for i in range(len(photos)) if i not in used]
    if leftover:
        if out:
            out[-1]["photos"].extend(photos[i] for i in leftover)
            out[-1]["captions"].extend(cap_of[i] for i in leftover)
            out[-1]["display"].extend(disp_of[i] for i in leftover)
        else:
            out.append({"label_ko": "사진", "label_en": "Photos", "is_overview": True,
                        "desc_ko": "", "photos": [photos[i] for i in leftover],
                        "captions": [cap_of[i] for i in leftover],
                        "display": [disp_of[i] for i in leftover]})
    # 전경/전체(is_overview) 그룹을 맨 앞으로(첫 사진 섹션에 대표 전경이 오게)
    out.sort(key=lambda g: 0 if g["is_overview"] else 1)
    return out


# ── 사진 기반 글: 섹션 조립([사진, 트렌드, 사진, …], 사진으로 시작·끝) ──────────
_TREND_SYSTEM = ("당신은 여행·문화 콘텐츠 에디터입니다. 요즘 독자가 관심 갖는 최신 트렌드와 "
                 "특정 장소를 연결한 소주제를 만들어 지정한 JSON 하나만 출력하세요.")


def _trend_prompt(topic, n, seed_kws) -> str:
    seed = ("참고 트렌드 키워드(이 중 어울리는 것을 활용, 부족하면 새로 제안): "
            + ", ".join(seed_kws) + "\n") if seed_kws else ""
    return f'''주제(장소/소재): "{topic}"
{seed}이 장소를 다루는 글에 넣을, '요즘 여행자·독자가 관심 갖는 최신 트렌드/관심사'와 이 장소를
연결한 소주제를 정확히 {n}개 만드세요. 각 소주제는 트렌드를 이 장소와 자연스럽게 잇습니다
(예: 요즘 뜨는 '근대건축 산책', 'SNS 인생샷 명소', '역사 다크투어', '한 나절 도보 코스' 등과
이곳의 접점). 서로 다른 트렌드로 다양하게.

아래 JSON만 출력(설명·코드펜스 없이):
{{"sections":[{{"ko_heading":"소제목(한국어)","en_heading":"heading (English)","intent":"이 섹션에서 다룰 핵심(한국어 1문장)"}}]}}'''


def _trend_sections(topic, n, settings, log=print) -> list:
    """트렌드 소주제 n개(heading/intent) 생성. keyword_pool의 대기 키워드를 씨앗으로 참고하고
    (소진하지 않음), LLM이 이 장소와 연결한 소주제를 만든다. 실패 시 있는 만큼만 반환."""
    if n <= 0:
        return []
    seed_kws = []
    try:
        import keyword_pool as kp
        seed_kws = [p["keyword"] for p in kp.get_next_keywords(n + 3, mark_used=False)]
    except Exception:
        pass
    prompt = _trend_prompt(topic, n, seed_kws)
    for attempt in range(3):
        try:
            d = _extract_json(_complete(settings, prompt, log, _TREND_SYSTEM))
            secs = [s for s in (d.get("sections") or [])
                    if (s.get("ko_heading") or s.get("en_heading"))]
            if secs:
                return secs[:n]
        except Exception as e:
            log(f"      ↻ 트렌드 소주제 재시도({attempt + 1}/3): {e}")
    return []


def _photo_based_sections(topic, photo_groups, settings, log=print) -> list:
    """사진 그룹들과 트렌드 소주제를 [사진, 트렌드, 사진, 트렌드, …] 로 교차 배치해 sections를
    조립한다(사진으로 시작·끝, 사진 그룹 N개 → 트렌드 N-1개). 각 섹션에 _kind('photo'/'trend')와
    사진 섹션이면 _group(그 그룹 dict)을 달아 이후 _generate_multi가 사진을 정확히 배치하게 한다."""
    n_photo = len(photo_groups)
    if n_photo == 0:
        return []
    n_trend = max(0, n_photo - 1)
    trends = _trend_sections(topic, n_trend, settings, log) if n_trend else []
    sections, ti = [], 0
    for gi, g in enumerate(photo_groups):
        caps = "; ".join(c for c in g.get("captions", []) if c and c != "(내용 파악 실패)")[:220]
        sections.append({
            "ko_heading": g["label_ko"],
            "en_heading": g["label_en"],
            "intent": (f"이 사진들에 실제로 담긴 것을 중심으로 소개·묘사하세요: "
                       f"{g.get('desc_ko', '')} (사진 속 내용: {caps}). "
                       "사진에 없는 소재를 지어내 섞지 마세요."),
            "_kind": "photo",
            "_group": g,
        })
        # 사진 그룹 사이에만 트렌드(마지막 그룹 뒤에는 넣지 않음 → 사진으로 끝)
        if gi < n_trend and ti < len(trends):
            t = trends[ti]; ti += 1
            sections.append({
                "ko_heading": (t.get("ko_heading") or t.get("en_heading") or "").strip(),
                "en_heading": (t.get("en_heading") or t.get("ko_heading") or "").strip(),
                "intent": (t.get("intent") or "").strip(),
                "_kind": "trend",
            })
    return sections


def gen_captions(topic, photo_names, settings, log=print) -> list:
    """사진 파일명 기반 한·영 캡션 생성. 한 번에 많이 시키면 출력이 잘리므로
    6장씩 나눠서 생성하고, 실패한 항목은 파일명으로 대체합니다."""
    if not photo_names:
        return []
    CHUNK = 6
    out = []
    for start in range(0, len(photo_names), CHUNK):
        chunk = photo_names[start:start + CHUNK]
        log(f"   🖼  캡션 {start + 1}~{start + len(chunk)}/{len(photo_names)} 작성...")
        caps = _gen_caption_chunk(topic, chunk, settings, log)
        for j, name in enumerate(chunk):
            c = caps[j] if j < len(caps) else {}
            fb = _fallback_caption(name, start + j)
            ko = _clean_alt(c.get("ko")) or fb["ko"]
            en = _clean_alt(c.get("en")) or fb["en"]
            out.append({"ko": ko, "en": en})
    return out


# 섹션마다 '도입 방식'을 다르게 지정해 같은 공식의 반복을 막는다(첫 문장 차별화).
_OPENER_MODES = [
    "구체적인 동작·장면을 클로즈업하듯 묘사하며 시작하세요(추상적 단언으로 시작하지 말 것).",
    "구체적인 수치·연도·규격·재료·통계 같은 '사실' 하나로 시작하세요.",
    "방문자가 현장에서 바로 쓰는 실용 정보나 관찰 요령으로 시작하세요.",
    "역사적 일화·인물·사건, 또는 유래를 이야기하듯 시작하세요.",
    "흔한 오해를 바로잡거나, 다른 대상과 비교·대조하며 시작하세요.",
    "보고·듣고·만지는 감각(소리·냄새·질감·색)에 대한 묘사로 시작하세요.",
]


def _first_sentence(html: str, limit: int = 80) -> str:
    """본문(HTML)에서 평문 첫 문장을 추출(반복 도입 회피용)."""
    txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()
    if not txt:
        return ""
    m = re.search(r"[.!?。…]", txt)
    s = txt[:m.end()] if m else txt
    return s[:limit].strip()


def _section_prompt(topic, sec, refs, identity="", siblings=None, index=0, used_openers=None,
                    facts="") -> str:
    id_line = f"[이 블로그의 색깔]\n{identity}\n\n" if identity else ""
    facts_block = (facts + "\n") if (facts or "").strip() else ""
    sib_block = ""
    if siblings:
        lines = "\n".join(
            f"  {'▶' if i == index else '-'} {h}" for i, h in enumerate(siblings))
        sib_block = (
            f"이 글 전체의 소제목 구성(▶ 가 지금 쓸 섹션):\n{lines}\n"
            f"→ 당신은 '▶' 섹션만 씁니다. 다른 소제목이 맡은 내용은 넘보지 마세요(중복 금지).\n\n")
    avoid_block = ""
    used_lines = "\n".join(f"  - {o}" for o in (used_openers or []) if o)
    if used_lines:
        avoid_block = (
            "앞 섹션들이 이미 사용한 '첫 문장'입니다 — 이와 비슷한 소재·표현·문장 구조로 시작하지 마세요:\n"
            f"{used_lines}\n\n")
    opener = _OPENER_MODES[index % len(_OPENER_MODES)]
    return f"""{id_line}전체 글 주제: "{topic}"
{sib_block}{avoid_block}{facts_block}지금 쓸 섹션 — 한국어: "{sec.get('ko_heading','')}" / 영어: "{sec.get('en_heading','')}"
이 섹션에서 다룰 내용: {sec.get('intent','')}
{_refs_block(refs)}
이 '한 섹션'의 본문을 한국어와 영어로 풍부하고 사려 깊게 작성하세요. 아래 JSON만 출력:

{{"body_ko": "한국어 본문 HTML", "body_en": "영어 본문 HTML"}}

작성 규칙:
- ★상투적 도입 공식 금지: 'A는 단순한 B가 아니라 C입니다', 'A는 단순히 B를 넘어 C입니다',
  영어의 'not merely/just ... but ...' 같은 대조 단언 구조로 시작하지 마세요(모든 섹션 공통 금지).
- ★이 섹션의 도입 방식(반드시 따르기): {opener}
- ★도입부 재소개 금지: 이 글에는 이미 별도의 도입부가 있습니다. 전체 주제나 배경을 다시 소개하지 말고,
  '○○는 ~한 곳입니다/~로 유명합니다' 같은 일반적 소개 문장으로 시작하지 마세요.
- ★다른 섹션과 같은 단어·소재(같은 사물·동작·이미지)로 시작하지 말고, 첫 문장의 주어와 구조를
  이 섹션만의 것으로 다르게 하세요. 첫 문장부터 이 소제목 고유의 구체적 사실·장면·수치·요령으로 들어가세요.
- 한국어·영어 각각 <p> 문단을 3~5개. 각 문단은 2~4문장으로 구체적으로.
- 문화·예술에 관심 있는 외국인에게 실질적 도움이 되는 사실·맥락·요령을 담되, 이 블로그의 색깔
  (전통예술·공예·장인·지역 문화)이 묻어나는 깊이 있는 시선을 유지하세요(뻔한 관광 안내 톤 금지).
- 한국어도 영어와 '동일한 분량·깊이'로 작성하세요(절대 짧게 쓰지 말 것).
- 목록이 어울리면 <ul><li> 사용 가능. <h2>나 소제목은 넣지 마세요(자동으로 붙습니다).
- <p>, <strong>, <ul>, <li> 외 태그·이미지·링크는 넣지 마세요.
- {FACT_RULE}
{JSON_SAFE}
JSON 외 텍스트는 절대 출력하지 마세요."""


def _plain_len(html: str) -> int:
    """태그를 뺀 평문 길이 (본문이 너무 짧은지 판정용)."""
    return len(re.sub(r"<[^>]+>", "", html or "").strip())


# 소주제 본문 최소 평문 길이(한 줄짜리 빈약한 본문 방지). 한·영 각각 기준.
_SECTION_MIN_LEN = 120


def _gen_section_safe(topic, sec, refs, settings, log, identity="", siblings=None, index=0,
                      used_openers=None, facts="") -> dict:
    """한 섹션을 생성. 본문이 충분히 길 때까지 최대 3회 재시도하고,
    매번 '가장 충실한(긴) 결과'를 보관해 한 줄짜리 빈약한 본문으로 떨어지지 않게 합니다.
    used_openers: 앞 섹션들의 첫 문장(반복 도입 회피용). facts: 검증된 현지 정보(그라운딩)."""
    best = None
    # 작은 모델은 한 번에 충분한 분량을 못 내는 일이 잦아 재시도를 더 준다(분량 확보).
    attempts = 5 if _is_small_model(settings) else 3

    def _score(d):
        return _plain_len(d["body_ko"]) + _plain_len(d["body_en"])

    for attempt in range(attempts):
        try:
            d = _extract_json(_complete(
                settings,
                _section_prompt(topic, sec, refs, identity, siblings, index, used_openers, facts),
                log, SECTION_SYSTEM))
            ko = (d.get("body_ko") or "").strip()
            en = (d.get("body_en") or "").strip()
            if ko and en:
                cand = {"body_ko": ko, "body_en": en}
                if best is None or _score(cand) > _score(best):
                    best = cand
                if _plain_len(ko) >= _SECTION_MIN_LEN and _plain_len(en) >= _SECTION_MIN_LEN:
                    return best   # 충분히 길면 채택
                log(f"      ↻ 소주제 본문이 짧아 재시도({attempt + 1}/{attempts}) "
                    f"(한 {_plain_len(ko)}자/영 {_plain_len(en)}자)")
        except Exception as e:
            log(f"      ⚠️ 소주제 재시도({attempt + 1}/{attempts}): {e}")
            time.sleep(1.5)   # 빈 응답/일시 과부하 시 Ollama가 회복할 여유

    if best is not None:
        # 3회 내 최소 길이엔 못 미쳤지만, 그나마 가장 충실한 결과를 사용
        if _score(best) < _SECTION_MIN_LEN:
            log("      ⚠️ 이 소주제 본문이 다소 짧습니다 — 발행 후 [지금 생성]으로 다시 시도 가능")
        return best

    # 3회 모두 예외(파싱 실패 등) — 한 줄 대신 안내 문단으로 표시
    intent = (sec.get("intent") or sec.get("ko_heading") or "").strip()
    para_ko = f"<p>{intent}</p>" if intent else "<p>(이 소주제 본문 생성에 실패했습니다. 다시 생성해 주세요.)</p>"
    para_en = (f"<p>{sec.get('en_heading', intent)}</p>"
               if (intent or sec.get('en_heading')) else "<p>(Section generation failed.)</p>")
    log("      ❌ 소주제 본문 생성 실패 — 임시 문구로 대체(발행 후 다시 생성 권장)")
    return {"body_ko": para_ko, "body_en": para_en}


def _map_block(place, lang) -> str:
    """장소명으로 구글 지도 임베드 블록 생성."""
    place = (place or "").strip()
    if not place:
        return ""
    q = urllib.parse.quote(place)
    if lang == "ko":
        head = f"<p><strong>📍 지도 — {place}</strong></p>"
        hl = "ko"
    else:
        head = f"<p><strong>📍 Map — {place}</strong></p>"
        hl = "en"
    return (f'{head}\n<iframe src="https://maps.google.com/maps?q={q}&output=embed&hl={hl}" '
            f'width="100%" height="300" style="border:0;border-radius:8px;margin-top:8px;" '
            f'allowfullscreen="" loading="lazy"></iframe>')


def _fix_slug(slug: str, lang: str, max_base: int = 35) -> str:
    """슬러그 정리 — 영소문자·하이픈만, Blogger가 잘라내지 않을 길이(기본 35자)로
    하이픈 경계에서 자르고 -en/-ko 접미사를 보장합니다.
    (긴 슬러그는 Blogger가 임의로 잘라 URL 불일치/재발행 사고의 원인이 됨)"""
    suffix = f"-{lang}"
    s = (slug or "").strip().lower()
    s = re.sub(r"[^a-z0-9-]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    if s.endswith(suffix):
        s = s[: -len(suffix)].rstrip("-")
    if len(s) > max_base:
        s = s[:max_base]
        if "-" in s:
            s = s.rsplit("-", 1)[0]   # 단어 중간에서 끊기지 않게
    return (s + suffix) if s else ""


def _norm_labels(labels, lang, category: str = "") -> list:
    """라벨 목록 정리 — 언어 태그(한국어/English)와 카테고리를 항상 포함시킨다.
    카테고리를 라벨에 넣지 않으면 Blogger의 '카테고리(라벨)' 사이드바가 있어도
    글들이 그 카테고리로 묶이지 않는 문제가 생기므로(2026-07-06 버그 수정),
    LLM이 준 구체적 태그와 별개로 category 값을 항상 라벨에 포함시킨다."""
    tag = "한국어" if lang == "ko" else "English"
    out = []
    for l in (labels or []):
        if isinstance(l, str) and l.strip() and l.strip() not in out:
            out.append(l.strip())
    category = (category or "").strip()
    if category and category not in out:
        out.insert(0, category)
    if tag not in out:
        out.insert(0, tag)
    return out[:8]


def _summary_block(text: str, lang: str) -> str:
    """본문 맨 위 '핵심 요약(TL;DR)' 박스. 역피라미드 — 검색 유입 독자가 즉시 답을 얻게 한다."""
    text = (text or "").strip()
    if not text:
        return ""
    label = "핵심 요약" if lang == "ko" else "In Short"
    return (f'<div style="border-left:4px solid #4a90d9;background:#f3f7fb;'
            f'padding:10px 14px;margin:0 0 1.2em;border-radius:0 6px 6px 0;">'
            f'<strong>{label}:</strong> {text}</div>')


def _toc_block(sections, lang: str) -> str:
    """소주제 제목으로 점프 링크 목차(TOC)를 만든다. 소주제가 3개 미만이면 생략.
    긴 글의 가독성과 검색 스니펫(구조 신호) 모두에 도움이 된다.
    각 소주제 <h2>에는 같은 id(s1, s2 ...)가 붙어 있어야 링크가 동작한다."""
    if not sections or len(sections) < 3:
        return ""
    key = "ko_heading" if lang == "ko" else "en_heading"
    alt = "en_heading" if lang == "ko" else "ko_heading"
    lis = []
    for i, s in enumerate(sections):
        h = (s.get(key) or s.get(alt) or "").strip()
        if h:
            lis.append(f'<li><a href="#s{i + 1}">{h}</a></li>')
    if not lis:
        return ""
    title = "목차" if lang == "ko" else "Contents"
    inner = "\n".join(lis)
    return (f'<div style="border:1px solid #e5e5e5;border-radius:8px;'
            f'padding:12px 16px;margin:1.2em 0;background:#fafafa;">'
            f'<strong>{title}</strong>\n<ul style="margin:6px 0 0;padding-left:1.2em;">\n'
            f'{inner}\n</ul></div>')


def _related_block(past, lang: str, limit: int = 4, exclude_urls=None) -> str:
    """과거 발행 글 중 URL이 있는 것들로 '관련 글' 내부 링크 박스를 만든다(없으면 빈 문자열).
    내부 링크는 검색 크롤러의 사이트 구조 파악과 독자 체류시간 모두에 도움이 된다.
    exclude_urls: 자기 자신(재생성 중인 글) 등 제외할 URL 모음."""
    if not past:
        return ""
    skip = set(exclude_urls or [])
    items = []
    for r in past:
        url = (r.get(f"{lang}_url") or "").strip()
        title = (r.get(lang) or r.get("topic") or "").strip()
        if url and title and url not in skip:
            items.append((title, url))
        if len(items) >= limit:
            break
    if not items:
        return ""
    heading = "관련 글" if lang == "ko" else "Related Posts"
    lis = "\n".join(f'<li><a href="{u}">{t}</a></li>' for t, u in items)
    return f"<h2>{heading}</h2>\n<ul>\n{lis}\n</ul>"


def _author_block(settings, lang: str) -> str:
    """글 하단 '글쓴이' 소개(E-E-A-T). 이름·소개가 모두 없으면 생략. LLM 호출 없는 고정 템플릿.
    JSON-LD의 author(settings.author_name)와 짝을 이뤄 저자 신뢰 신호를 보강한다."""
    name = (settings.get("author_name") or "").strip()
    bio = (settings.get(f"author_bio_{lang}") or "").strip()
    if not name and not bio:
        return ""
    label = "글쓴이" if lang == "ko" else "About the Author"
    body = ""
    if name:
        body += f"<strong>{name}</strong>"
    if name and bio:
        body += " — "
    if bio:
        body += bio
    return (f'<div style="margin:1.5em 0 0;padding:12px 14px;border-top:2px solid #eee;'
            f'font-size:14px;color:#444;"><div style="color:#888;font-size:12px;'
            f'margin-bottom:4px;">{label}</div>{body}</div>')


def _related_desc_list(related, lang: str) -> str:
    """연결된 단어 카드 아래에 붙는 '단어 — 한 줄 설명' 목록. desc(한국어)/desc_en(영어)를
    언어별로 따로 붙여 언어 혼입 없이 양쪽 다 설명이 들어가게 한다."""
    items = []
    for r in (related or []):
        ko = (r.get("ko") or "").strip()
        en = (r.get("en") or "").strip()
        desc = (r.get("desc") or "").strip()
        desc_en = (r.get("desc_en") or "").strip()
        if not ko:
            continue
        if lang == "ko":
            label = f"<strong>{ko}</strong>" + (f" ({en})" if en else "")
            items.append(f"<li>{label}{(' — ' + desc) if desc else ''}</li>")
        else:
            label = (f"<strong>{en}</strong> ({ko})" if en else f"<strong>{ko}</strong>")
            items.append(f"<li>{label}{(' — ' + desc_en) if desc_en else ''}</li>")
    if not items:
        return ""
    return "<ul>\n" + "\n".join(items) + "\n</ul>"


def _series_nav_block(series_ctx, lang: str) -> str:
    """시리즈 이전/다음 편 실링크 내비게이션(있는 것만). 같은 시리즈 글끼리 크롤 동선·체류↑.
    series_ctx에 prev_{lang}_url / next_{lang}_url 와 제목이 채워져 있을 때만 링크를 만든다.
    next 링크는 발행 시 역주입되므로(B단계) 보통 생성 시점엔 prev만 채워진다."""
    if not series_ctx:
        return ""
    parts = []
    purl = (series_ctx.get(f"prev_{lang}_url") or "").strip()
    ptitle = (series_ctx.get(f"prev_{lang}") or series_ctx.get("prev") or "").strip()
    if purl:
        plabel = "◀ 이전 편" if lang == "ko" else "◀ Previous"
        sep = f": {ptitle}" if ptitle else ""
        parts.append(f'<a href="{purl}">{plabel}{sep}</a>')
    nurl = (series_ctx.get(f"next_{lang}_url") or "").strip()
    ntitle = (series_ctx.get(f"next_{lang}") or series_ctx.get("next") or "").strip()
    if nurl:
        nlabel = "다음 편 ▶" if lang == "ko" else "Next ▶"
        sep = f": {ntitle}" if ntitle else ""
        parts.append(f'<a href="{nurl}">{nlabel}{sep}</a>')
    if not parts:
        return ""
    inner = ' &nbsp;·&nbsp; '.join(parts)
    return (f'<p style="margin:1em 0;padding-top:8px;border-top:1px solid #eee;'
            f'font-size:14px;">{inner}</p>')


def _authority_block(sources, lang: str) -> str:
    """검증된 출처(백과사전 등)로 아웃바운드 권위 링크 박스(E-E-A-T). 출처 없으면 빈 문자열.
    sources: [(title, url)] — factcheck.grounding_sources() 결과(실제 링크만, 지어내지 않음)."""
    if not sources:
        return ""
    heading = "참고 자료" if lang == "ko" else "References"
    lis = "\n".join(
        f'<li><a href="{u}" target="_blank" rel="noopener noreferrer">{t}</a></li>'
        for t, u in sources if u)
    if not lis:
        return ""
    return f"<h3>{heading}</h3>\n<ul>\n{lis}\n</ul>"


def _article_jsonld(title, description, date_iso, image_url, author, lang,
                    publisher="") -> str:
    """글 본문에 넣을 BlogPosting JSON-LD(<script>). 검색엔진의 글 이해·리치결과에 도움.
    image·datePublished는 발행 시점에만 확정되므로 publish_date에서 호출한다.
    (Blogger가 본문 <script>를 보존해야 동작 — settings.seo_schema 토글로 끌 수 있음.)"""
    title = (title or "").strip()
    if not title:
        return ""
    d = {
        "@context": "https://schema.org",
        "@type": "BlogPosting",
        "headline": title[:110],
        "inLanguage": "ko" if lang == "ko" else "en",
    }
    desc = (description or "").strip()
    if desc:
        d["description"] = desc[:300]
    if date_iso:
        d["datePublished"] = date_iso
        d["dateModified"] = date_iso
    if image_url:
        d["image"] = [image_url]
    name = (author or "").strip()
    if name:
        d["author"] = {"@type": "Person", "name": name}
    pub_name = (publisher or "").strip()
    if pub_name:
        d["publisher"] = {"@type": "Organization", "name": pub_name}
    return ('\n<script type="application/ld+json">'
            + json.dumps(d, ensure_ascii=False) + '</script>')


def _inject_next_into_prev(service, blog_id, data, series_ctx, cur_cfg,
                           en_url, ko_url, log=print):
    """방금 발행한 글(N편)의 이전 편(N-1편) 라이브 포스트에 '다음 편 ▶' 링크를 1회 역주입.
    멱등성: 본문에 <!-- SERIES_NEXT --> 가 있으면 건너뜀. 실패해도 발행에는 영향 없음(호출부 try)."""
    if not series_ctx:
        return
    prev_key, prev_e = _series_sibling_entry(
        data, series_ctx.get("sid"), (series_ctx.get("index") or 0) - 1)
    if not prev_e or prev_e.get("next_link_injected"):
        return
    from urllib.parse import urlsplit
    targets = [("en", prev_e.get("en_url"), en_url, (cur_cfg or {}).get("en_title", "")),
               ("ko", prev_e.get("ko_url"), ko_url, (cur_cfg or {}).get("ko_title", ""))]
    done_any = False
    for lang, prev_url, nxt_url, nxt_title in targets:
        prev_url = (prev_url or "").strip()
        nxt_url = (nxt_url or "").strip()
        if not prev_url or not nxt_url:
            continue
        path = urlsplit(prev_url).path
        try:
            post = service.posts().getByPath(
                blogId=blog_id, path=path,
                fields="id,content,title,labels,searchDescription").execute()
        except Exception as e:
            log(f"   ⚠️ 이전 편 조회 실패({lang}): {e}")
            continue
        html = post.get("content", "") or ""
        if "<!-- SERIES_NEXT -->" in html:
            continue   # 이미 역주입됨
        label = "다음 편 ▶" if lang == "ko" else "Next ▶"
        sep = f": {nxt_title}" if nxt_title else ""
        block = (f'<!-- SERIES_NEXT --><p style="margin:1em 0;padding-top:8px;'
                 f'border-top:1px solid #eee;font-size:14px;">'
                 f'<a href="{nxt_url}">{label}{sep}</a></p>')
        try:
            service.posts().update(
                blogId=blog_id, postId=post["id"],
                body={"title": post.get("title", ""), "content": html + "\n" + block,
                      "labels": post.get("labels", [])}).execute()
            # update가 searchDescription을 비울 수 있어, 있던 값은 patch로 복구
            meta = (post.get("searchDescription") or "").strip()
            if meta:
                try:
                    service.posts().patch(blogId=blog_id, postId=post["id"],
                                          body={"searchDescription": meta[:150]}).execute()
                except Exception:
                    pass
            log(f"   🔗 이전 편에 '다음 편 ▶' 링크 역주입({lang})")
            done_any = True
        except Exception as e:
            log(f"   ⚠️ 이전 편 업데이트 실패({lang}): {e}")
    if done_any:
        prev_e["next_link_injected"] = True
        save_schedule(data)


def _generate_multi(topic, refs, settings, n_photos, n_sections, log,
                    progress=None, series_ctx=None, captions=None, past=None,
                    word_mode=False, stop_check=None, photo_groups=None) -> dict:
    """개요(후킹 들어가는말) → 소주제별 본문 → 맺음말(다음편 예고) → 통합.
    captions: 사진별 {ko,en} 캡션 리스트 — 섹션 앞 IMAGE 자리표시자의 설명문으로 사용.
    past: past_titles() 결과 — 이미 발행·생성된 제목 목록(중복 방지).
    word_mode: 단어 사전 글이면 맨 위에 타이틀 카드(히어로) 자리표시자를 넣고 word_ko/en을 받는다.
    stop_check: 호출 시 True를 반환하면 다음 소주제 작성 전에 중단(진행 중인 LLM 응답 1회는
    끝까지 기다림).
    photo_groups: (사진 기반 글) _analyze_and_group_photos 결과. 있으면 개요의 sections를
    [사진 그룹, 트렌드, 사진 그룹, …]로 교체하고, 각 사진 섹션에 그 그룹 사진들을 IMAGE
    자리표시자로 배치(그룹 순서대로 IMAGE_1..M — 발행 시 이 순서로 사진이 채워짐)."""
    progress = progress or (lambda *a, **k: None)
    stop_check = stop_check or (lambda: False)
    captions = captions or []
    identity = _identity(settings)
    facts = ""                       # 개요에서 장소(location)를 얻은 뒤 채움(그라운딩)
    steps = n_sections + 2  # 개요 1 + 소주제 n + 맺음말 1
    progress(1.0, "개요(소주제 구성) 작성 중")
    log("   1) 개요(소주제 구성) 작성 중...")
    outline_prompt = _outline_prompt(topic, refs, identity, n_photos, n_sections, series_ctx,
                                     past=past, word_mode=word_mode)
    outline, _last = None, None
    for attempt in range(3):     # 개요도 빈 응답/파싱 실패 시 재시도
        if stop_check():
            log("   ■ 사용자 요청으로 중단했습니다.")
            raise StopRequested("사용자가 중단했습니다.")
        try:
            o = _extract_json(_complete(settings, outline_prompt, log, OUTLINE_SYSTEM))
            if o.get("sections"):
                outline = o
                break
            _last = "개요에 소주제(sections)가 없음"
        except Exception as e:
            _last = str(e)
            log(f"      ↻ 개요 재시도({attempt + 1}/3): {e}")
            time.sleep(1.5)
    if outline is None:
        raise ValueError(f"개요 생성 실패: {_last}")
    sections = outline.get("sections") or []

    # 사진 기반 글: 개요의 소주제 구성을 [사진 그룹, 트렌드, 사진 그룹, …]로 교체한다.
    # (제목·메타·요약·들어가는말은 위 개요 LLM 결과를 그대로 쓰고, sections만 바꿔 사진과
    #  글이 어긋나지 않게 함.) 각 사진 섹션 사진에 '그룹 순서대로'의 전역 IMAGE 번호를 매긴다.
    photo_index = {}   # str(path) → 전역 IMAGE 번호(1-based)
    if photo_groups:
        sections = _photo_based_sections(topic, photo_groups, settings, log)
        outline["sections"] = sections
        gi = 0
        for g in photo_groups:
            for p in g.get("photos", []):
                gi += 1
                photo_index[str(p)] = gi

    # 사실 검증(그라운딩) — 네이버 지역검색·백과사전·뉴스로 실제 정보를 받아 근거로 주입(키 있을 때만)
    sources = []                     # 아웃바운드 권위 링크용 실제 출처 [(title,url)]
    try:
        import factcheck
        facts = factcheck.grounding_facts(topic, (outline.get("location") or ""), settings, log)
        sources = factcheck.grounding_sources(topic, (outline.get("location") or ""), settings)
    except Exception as e:
        log(f"   ⚠️ 사실 검증 그라운딩 생략: {e}")

    ko_parts, en_parts = [], []

    def _blank_before(parts):
        """소주제 시작부분과 이전 단락 사이에 실제로 보이는 빈 줄 하나를 둔다(맨 앞일 땐 생략).
        그냥 빈 문자열만 넣으면 HTML에서 줄바꿈이 무시되어 화면엔 안 보이므로,
        빈 단락을 넣어 실제로 한 줄 띄워 보이게 한다."""
        if parts:
            parts.append('<p>&nbsp;</p>')

    # 단어 사전: 맨 위 타이틀 카드(히어로) 자리표시자. 실제 카드는 generate_post에서
    # 단어로 생성해 library_photos[0]로 넣어 발행 단계에 IMAGE_1 로 치환된다.
    if word_mode:
        wko = (outline.get("word_ko") or "").strip()
        wen = (outline.get("word_en") or "").strip()
        alt_ko = (f"{wko} 뜻 — 한국 문화 단어 사전" if wko else "한국 문화 단어 사전")
        alt_en = (f"{wen} meaning — K-Culture Dictionary" if wen else "K-Culture Dictionary")
        ko_parts.append(f'<!-- IMAGE_1 alt="{alt_ko}" -->')
        en_parts.append(f'<!-- IMAGE_1 alt="{alt_en}" -->')

    # 핵심 요약(TL;DR) — 도입부보다 먼저(역피라미드: 검색 유입 독자가 즉시 답을 얻게)
    sum_ko = _summary_block(outline.get("summary_ko"), "ko")
    sum_en = _summary_block(outline.get("summary_en"), "en")
    if sum_ko:
        ko_parts.append(sum_ko)
    if sum_en:
        en_parts.append(sum_en)

    intro_ko = (outline.get("intro_ko") or "").strip()
    intro_en = (outline.get("intro_en") or "").strip()
    if intro_ko:
        ko_parts.append(f"<p>{intro_ko}</p>")
    if intro_en:
        en_parts.append(f"<p>{intro_en}</p>")

    # 목차(TOC) — 도입부 바로 뒤. 소주제 <h2 id="sN">로 점프(긴 글 가독성·구조 신호)
    toc_ko = _toc_block(sections, "ko")
    toc_en = _toc_block(sections, "en")
    if toc_ko:
        ko_parts.append(toc_ko)
    if toc_en:
        en_parts.append(toc_en)

    # 연관 단어(연결된 단어 카드용) — word_mode일 때만
    related_words = []
    if word_mode:
        for rw in (outline.get("related_words") or []):
            if isinstance(rw, dict) and (rw.get("ko") or "").strip():
                related_words.append({"ko": rw["ko"].strip(), "en": (rw.get("en") or "").strip(),
                                      "desc": (rw.get("desc") or "").strip(),
                                      "desc_en": (rw.get("desc_en") or "").strip()})
            elif isinstance(rw, str) and rw.strip():
                related_words.append({"ko": rw.strip(), "en": "", "desc": "", "desc_en": ""})
        related_words = related_words[:3]
    related_done = False
    _wko = (outline.get("word_ko") or "").strip()
    _wen = (outline.get("word_en") or _wko).strip()

    total = len(sections)
    sib_headings = [(s.get("ko_heading") or s.get("en_heading") or "").strip() for s in sections]
    short_sections = []   # 짧게 생성된 소주제 번호(완성도 점검용)
    used_openers = []     # 앞 섹션들의 첫 문장(반복 도입 회피용)
    for i, sec in enumerate(sections):
        if stop_check():
            log("   ■ 사용자 요청으로 중단했습니다.")
            raise StopRequested("사용자가 중단했습니다.")
        kh = (sec.get("ko_heading") or "").strip()
        eh = (sec.get("en_heading") or "").strip()
        progress((i + 1) / steps * 100.0,
                 f"소주제 {i + 1}/{total} 작성 중: {kh or eh}")
        log(f"   2) 소주제 {i + 1}/{total} 작성: {kh or eh}")
        body = _gen_section_safe(topic, sec, refs, settings, log, identity,
                                 siblings=sib_headings, index=i, used_openers=used_openers,
                                 facts=facts)
        if _plain_len(body["body_ko"]) < _SECTION_MIN_LEN:
            short_sections.append(i + 1)
        # 다음 섹션이 같은 도입을 반복하지 않도록 이 섹션 첫 문장을 기록
        for opener in (_first_sentence(body["body_ko"]), _first_sentence(body["body_en"])):
            if opener:
                used_openers.append(opener)
        # 섹션마다 사진 배치
        _blank_before(ko_parts)
        _blank_before(en_parts)
        if photo_groups:
            # 사진 기반: 소제목(h2) 먼저, 그 뒤에 이 섹션(사진 그룹)의 사진들을 '그룹 순서대로'의
            # 전역 IMAGE 번호로 배치. 트렌드 섹션(_kind!=photo)은 사진 없이 글만.
            if kh:
                ko_parts.append(f'<h2 id="s{i + 1}">{kh}</h2>')
            if eh:
                en_parts.append(f'<h2 id="s{i + 1}">{eh}</h2>')
            grp = sec.get("_group")
            ko_ph, en_ph = [], []
            if sec.get("_kind") == "photo" and grp:
                gdisp = grp.get("display", [])
                for j, p in enumerate(grp.get("photos", [])):
                    gidx = photo_index.get(str(p))
                    if not gidx:
                        continue
                    # 사진 설명(캡션)은 파일명(정확)+비전(참고)을 결합한 자연스러운 문구(display).
                    # 실패 시 파일명 정리로 폴백. 영어는 그룹 제목(label_en).
                    ko_alt = _clean_alt(gdisp[j] if j < len(gdisp) else "") \
                        or _caption_from_filename(p.name) or kh
                    en_alt = _clean_alt(grp.get("label_en")) or eh
                    ko_ph.append(f'<!-- IMAGE_{gidx} alt="{ko_alt}" -->')
                    en_ph.append(f'<!-- IMAGE_{gidx} alt="{en_alt}" -->')
            # 전경(첫 사진)은 소제목 바로 뒤에, 나머지 사진은 본문 문단 사이에 분산 —
            # "사진 줄줄이 → 글" 구조 대신 "문단 → 사진 → 문단 → 사진"으로 글과 함께 보이게.
            if ko_ph:
                ko_parts.append(ko_ph[0])
                ko_parts.append(_insert_midbody_figures(body["body_ko"], ko_ph[1:]))
            else:
                ko_parts.append(body["body_ko"])
            if en_ph:
                en_parts.append(en_ph[0])
                en_parts.append(_insert_midbody_figures(body["body_en"], en_ph[1:]))
            else:
                en_parts.append(body["body_en"])
        else:
            if i < n_photos:
                cap = captions[i] if i < len(captions) else {}
                ko_alt = _clean_alt(cap.get("ko")) or kh
                en_alt = _clean_alt(cap.get("en")) or eh
                ko_parts.append(f'<!-- IMAGE_{i + 1} alt="{ko_alt}" -->')
                en_parts.append(f'<!-- IMAGE_{i + 1} alt="{en_alt}" -->')
            # '연결된 단어들' 섹션이면 그 제목 뒤에 연관 단어 카드(IMAGE_2) 자리표시자
            is_conn = (word_mode and related_words and not related_done
                       and _is_connections_heading(kh, eh))
            alt2_ko = (f"{_wko}와 연결된 단어 — " + ", ".join(r["ko"] for r in related_words)) if is_conn else ""
            alt2_en = (f"Words connected to {_wen}") if is_conn else ""
            if kh:
                ko_parts.append(f'<h2 id="s{i + 1}">{kh}</h2>')
            if is_conn:
                ko_parts.append(f'<!-- IMAGE_2 alt="{alt2_ko}" -->')
                _dl = _related_desc_list(related_words, "ko")
                if _dl:
                    ko_parts.append(_dl)
            ko_parts.append(body["body_ko"])
            if eh:
                en_parts.append(f'<h2 id="s{i + 1}">{eh}</h2>')
            if is_conn:
                en_parts.append(f'<!-- IMAGE_2 alt="{alt2_en}" -->')
                _dl = _related_desc_list(related_words, "en")
                if _dl:
                    en_parts.append(_dl)
            en_parts.append(body["body_en"])
            if is_conn:
                related_done = True

    # 본문에 못 들어간 나머지 사진은 캡션과 함께 갤러리로 (모든 사진이 캡션을 갖도록)
    # (사진 기반 글은 모든 사진이 그룹 섹션에 배치되므로 갤러리를 만들지 않는다.)
    placed = min(total, n_photos)
    if not photo_groups and n_photos > placed:
        _blank_before(ko_parts)
        _blank_before(en_parts)
        ko_parts.append("<h2>📷 사진 갤러리</h2>")
        en_parts.append("<h2>📷 Photo Gallery</h2>")
        for k in range(placed, n_photos):
            cap = captions[k] if k < len(captions) else {}
            ko_alt = _clean_alt(cap.get("ko")) or f"사진 {k + 1}"
            en_alt = _clean_alt(cap.get("en")) or f"Photo {k + 1}"
            ko_parts.append(f'<!-- IMAGE_{k + 1} alt="{ko_alt}" -->')
            en_parts.append(f'<!-- IMAGE_{k + 1} alt="{en_alt}" -->')

    # 연결된 단어 섹션을 LLM이 안 만들었으면 카드가 들어갈 자리를 끝에 보강
    if word_mode and related_words and not related_done:
        _blank_before(ko_parts); _blank_before(en_parts)
        ko_parts.append("<h2>연결된 단어들</h2>")
        ko_parts.append(f'<!-- IMAGE_2 alt="{_wko}와 연결된 단어" -->')
        _dl = _related_desc_list(related_words, "ko")
        if _dl:
            ko_parts.append(_dl)
        en_parts.append("<h2>Connected Words</h2>")
        en_parts.append(f'<!-- IMAGE_2 alt="Words connected to {_wen}" -->')
        _dl = _related_desc_list(related_words, "en")
        if _dl:
            en_parts.append(_dl)
        related_done = True

    # 위치 지도(실제 장소가 있을 때만)
    location = (outline.get("location") or "").strip()
    if location:
        log(f"   📍 위치 지도 삽입: {location}")
        mk, me = _map_block(location, "ko"), _map_block(location, "en")
        if mk:
            ko_parts.append(mk)
        if me:
            en_parts.append(me)

    # 실용정보 표 + FAQ (SEO: 검색 스니펫·체류시간↑)
    log("   3) 실용정보 표 + FAQ 작성...")
    extras = _gen_seo_extras(topic, location, refs, settings, log, facts)
    if extras["tips_ko"]:
        _blank_before(ko_parts)
        ko_parts.append("<h2>여행 꿀팁 (실용 정보)</h2>"); ko_parts.append(extras["tips_ko"])
    if extras["tips_en"]:
        _blank_before(en_parts)
        en_parts.append("<h2>Travel Tips (Practical Info)</h2>"); en_parts.append(extras["tips_en"])
    if extras["faq_ko"]:
        _blank_before(ko_parts)
        ko_parts.append("<h2>자주 묻는 질문 (FAQ)</h2>"); ko_parts.append(extras["faq_ko"])
    if extras["faq_en"]:
        _blank_before(en_parts)
        en_parts.append("<h2>FAQ</h2>"); en_parts.append(extras["faq_en"])

    # 맺음말(다음 편 예고/후킹)
    progress((total + 1) / steps * 100.0, "맺음말(다음 편 예고) 작성 중")
    log("   4) 맺음말(다음 편 예고) 작성...")
    conc = _gen_conclusion(topic, series_ctx, settings, log)
    if conc["conc_ko"]:
        _blank_before(ko_parts)
        ko_parts.append("<h2>맺음말</h2>")
        ko_parts.append(conc["conc_ko"])
    if conc["conc_en"]:
        _blank_before(en_parts)
        en_parts.append("<h2>Wrapping Up</h2>")
        en_parts.append(conc["conc_en"])

    # 아웃바운드 권위 링크(참고 자료) — 검증된 실제 출처만(키 없으면 비어 있어 생략)
    auth_ko = _authority_block(sources, "ko")
    auth_en = _authority_block(sources, "en")
    if auth_ko:
        _blank_before(ko_parts); ko_parts.append(auth_ko)
    if auth_en:
        _blank_before(en_parts); en_parts.append(auth_en)

    # 시리즈 이전/다음 편 실링크 (생성 시점엔 보통 '이전 편'만 채워짐 — 다음 편은 B단계 역주입)
    nav_ko = _series_nav_block(series_ctx, "ko")
    nav_en = _series_nav_block(series_ctx, "en")
    if nav_ko:
        _blank_before(ko_parts); ko_parts.append(nav_ko)
    if nav_en:
        _blank_before(en_parts); en_parts.append(nav_en)

    # 관련 글 내부 링크 박스 (SEO: 사이트 구조·체류시간↑) — 과거 발행 글 중 URL 있는 것
    # 시리즈 이전/다음 편은 위 내비에 이미 있으니 관련 글에서 중복 제외
    _excl = set()
    if series_ctx:
        for _k in ("prev_ko_url", "prev_en_url", "next_ko_url", "next_en_url"):
            if series_ctx.get(_k):
                _excl.add(series_ctx[_k])
    rel_ko = _related_block(past, "ko", exclude_urls=_excl)
    rel_en = _related_block(past, "en", exclude_urls=_excl)
    if rel_ko:
        _blank_before(ko_parts); ko_parts.append(rel_ko)
    if rel_en:
        _blank_before(en_parts); en_parts.append(rel_en)

    # 글쓴이 소개(E-E-A-T) — 맨 끝. 이름/소개 설정이 있을 때만(없으면 생략)
    abio_ko = _author_block(settings, "ko")
    abio_en = _author_block(settings, "en")
    if abio_ko:
        _blank_before(ko_parts); ko_parts.append(abio_ko)
    if abio_en:
        _blank_before(en_parts); en_parts.append(abio_en)

    if short_sections:
        log(f"   ⚠️ 소주제 {short_sections}번 본문이 짧게 생성되었습니다 — "
            f"필요하면 [지금 생성]으로 다시 만들면 더 충실해집니다.")

    return {
        "en_title": outline.get("en_title", topic),
        "ko_title": outline.get("ko_title", topic),
        "en_meta": outline.get("en_meta", ""),
        "ko_meta": outline.get("ko_meta", ""),
        "en_slug": outline.get("en_slug", ""),
        "ko_slug": outline.get("ko_slug", ""),
        "en_labels": _norm_labels(outline.get("en_labels"), "en"),
        "ko_labels": _norm_labels(outline.get("ko_labels"), "ko"),
        "category": outline.get("category", "Travel"),
        "location": location,
        "word_ko": (outline.get("word_ko") or "").strip(),
        "word_en": (outline.get("word_en") or "").strip(),
        "summary_ko": (outline.get("summary_ko") or "").strip(),
        "summary_en": (outline.get("summary_en") or "").strip(),
        "related_words": related_words,
        "body_en": "\n".join(en_parts),
        "body_ko": "\n".join(ko_parts),
    }


def _generate_single(topic, refs, settings, photo_names, log) -> dict:
    """예전 단일 호출 방식(폴백용)."""
    prompt = _build_gen_prompt(topic, photo_names, settings.get("blog_hint", ""), refs)
    cfg = _extract_json(_complete(settings, prompt, log, GEN_SYSTEM))
    for k in ["en_title", "ko_title", "body_en", "body_ko"]:
        if not cfg.get(k):
            raise ValueError(f"단일 생성 응답에 '{k}'가 비어 있습니다.")
    return cfg


def generate_post(date_str: str, topic: str, settings: dict, log=print,
                  refs: str = "", progress=None, series_ctx=None, photo_dir: str = None,
                  data: dict = None, stop_check=None) -> dict:
    """주제로 한/영 글을 생성해 cfg 딕셔너리를 반환하고 generated/{date}/ 에 캐시.
    소주제별로 나눠 깊이 있게 쓴 뒤 하나의 글로 통합합니다(빈약한 글 방지).
    refs는 참고 사이트/작성 방향. series_ctx는 시리즈 맥락. photo_dir는 사진 폴더(없으면 날짜폴더).
    data: schedule 데이터(과거 제목 수집용 — 있으면 중복 방지 프롬프트에 반영).
    progress(pct, msg) 콜백으로 진행률 보고.
    stop_check: 호출 시 True를 반환하면 다음 소주제 작성 전에 중단."""
    progress = progress or (lambda *a, **k: None)
    stop_check = stop_check or (lambda: False)
    # 단어 사전 글: 개념이라 어울리는 사진이 없음 → 라이브러리 자동 매칭을 끈다(엉뚱한 이미지 방지).
    # 대신 아래에서 '타이틀 카드'(깔끔한 배경 + 한글/영어 단어)를 히어로로 자동 생성.
    word_mode = _is_word_post(topic, refs)
    photos = resolve_photos(date_str, photo_dir, settings.get("auto_date_photos", False))
    # 폴더 지정·자동탐색 모두 없으면 → 내 사진 라이브러리에서 자동 매칭(1B). 단어 글은 제외.
    library_photos = []
    if not photos and not word_mode and settings.get("use_photo_library", True) and not photo_dir:
        try:
            import photo_library as pl
            n_sec = max(3, int(settings.get("sections", DEFAULT_SECTIONS)))
            location_hint = _extract_location_hint(topic)
            # 지역명이 감지되면 strict_location=True — 다른 지역 사진이 섞이는 것 방지
            cand = pl.collect_for_post(topic, location_hint, n_sec + 1,
                                       strict_location=bool(location_hint))
            if cand:
                photos = cand
                library_photos = [str(p) for p in cand]
                hint_msg = f" (지역: {location_hint})" if location_hint else ""
                log(f"   📚 내 사진 라이브러리에서 {len(cand)}장 자동 매칭{hint_msg}")
            elif location_hint:
                log(f"   📚 라이브러리에 '{location_hint}' 사진 없음 — 사진 없이 진행")
        except Exception as e:
            log(f"   ⚠️ 라이브러리 검색 생략: {e}")

    # 사진 기반 글(photo_dir 직접 지정 + 실사진): 각 사진을 vision으로 분석해 '같은 대상'끼리
    # 그룹으로 묶고, 그 그룹을 각각 한 소주제로 삼는다(사진↔글 정확 매칭). 개요 sections는
    # [사진 그룹, 트렌드, 사진 그룹, …]로 조립된다(_generate_multi가 photo_groups로 처리).
    photo_groups = None
    sign_ocr_block = ""
    if photos and not word_mode and (photo_dir or "").strip():
        # 사진 기반 글은 그룹 완전성이 우선 — 10장 제한(resolve_photos)은 일반 글 기준이므로
        # 여기서는 폴더 전체 사진으로 그룹핑하고, 그룹당 상한으로 과다(사진첩화)만 막는다.
        try:
            import publish_today as _pub
            allp = _pub.find_photos(Path(photo_dir))
            if allp:
                photos = allp
        except Exception:
            pass
        # 설명판·비석 사진은 본문에서 빼고(글자만 판독해 정보로 활용), 나머지만 그룹핑·게재.
        sign_photos = [p for p in photos if _is_signboard(p.name)]
        if sign_photos:
            photos = [p for p in photos if not _is_signboard(p.name)]
            log(f"   📖 설명판 사진 {len(sign_photos)}장은 본문에서 빼고 글자만 판독해 내용에 반영합니다.")
            try:
                sign_ocr_block = _ocr_signboards(sign_photos, settings, log)
            except Exception as e:
                log(f"   ⚠️ 설명판 판독 생략: {e}")
        log(f"   🧩 사진 기반 글 — {len(photos)}장을 분석·그룹핑합니다...")
        try:
            photo_groups = _analyze_and_group_photos(photos, topic, settings, log) or None if photos else None
        except Exception as e:
            log(f"   ⚠️ 사진 그룹핑 생략(기존 방식으로): {e}")
            photo_groups = None
        if photo_groups:
            _MAX_PER_GROUP = 6   # 한 소주제(섹션)에 너무 많은 사진이 몰리지 않게(폴더 사진은 최대한 활용)
            for g in photo_groups:
                g["photos"] = g["photos"][:_MAX_PER_GROUP]
                g["captions"] = g["captions"][:_MAX_PER_GROUP]
                g["display"] = g.get("display", [])[:_MAX_PER_GROUP]
            # 그룹 순서대로 사진을 다시 나열(중복 제거·상한 반영) → IMAGE 번호·발행 순서와 일치
            photos = [p for g in photo_groups for p in g["photos"]]
            log(f"   🧩 사진 {len(photo_groups)}개 그룹 / 총 {len(photos)}장으로 구성")

    photo_names = [p.name for p in photos]
    n_photos = len(photos)
    # 카드 히어로는 '단어 글이면서 명시적 사진이 전혀 없을 때만' — 사진을 직접 넣으면 그 사진 우선.
    card_mode = word_mode and n_photos == 0
    n_sections = max(3, int(settings.get("sections", DEFAULT_SECTIONS)))
    if photo_groups:
        n_sections = 2 * len(photo_groups) - 1   # [사진,트렌드,사진,…] — 진행률 표시용

    engine = settings.get("llm", "gemma4")
    log(f"   🤖 글 생성 시작 ({engine}) — 주제: {topic}  "
        f"[소주제 {n_sections}개 + 들어가는말·맺음말, 사진 {n_photos}장]")
    progress(0.5, "글 생성 준비 중")
    if engine != "claude" and not ensure_ollama_running(settings, log):
        raise RuntimeError("Ollama 서버를 사용할 수 없습니다.")

    # 사진이 '실제로 무엇을 찍었는지' 파악해 refs에 덧붙임 — 본문이 사진 내용을 모른 채
    # topic만 보고 무관한 소재로 흘러가는 문제 방지(직접 지정한 photo_dir일 때 특히 중요).
    if photos and not word_mode:
        photo_desc = _describe_photos(photos, settings, log)
        if photo_desc:
            refs = (refs + "\n\n" + photo_desc) if (refs or "").strip() else photo_desc

    # 설명판·비석 사진에서 위에서 판독한 글자를 본문 근거로 refs에 추가 — 사진 속 실제
    # 역사·설명 정보를 본문에 정확히 녹인다(사진은 본문에서 이미 제외됨).
    if sign_ocr_block:
        refs = (refs + "\n\n" + sign_ocr_block) if (refs or "").strip() else sign_ocr_block

    # 사진 캡션(한·영) 먼저 생성 — 본문 IMAGE 자리표시자의 설명문으로 사용.
    # (사진 기반 글은 그룹핑 단계에서 얻은 vision 캡션을 쓰므로 파일명 기반 캡션은 생략.)
    captions = []
    if n_photos and not photo_groups:
        log(f"   🖼  사진 {n_photos}장 캡션 작성 중...")
        captions = gen_captions(topic, photo_names, settings, log)

    # 과거 발행·생성 제목 수집 — LLM에게 "이미 쓴 제목이니 반복 금지" 로 전달
    _past = []
    if data:
        try:
            _past = past_titles(data)
            if _past:
                log(f"   🔁 중복 방지: 기존 발행/생성 글 {len(_past)}편 제목을 프롬프트에 전달합니다.")
        except Exception:
            pass

    # 시리즈: 이전 편이 이미 발행돼 URL이 있으면 본문에 '◀ 이전 편' 실링크로 연결
    if series_ctx and data:
        prev_key, prev_e = _series_sibling_entry(
            data, series_ctx.get("sid"), (series_ctx.get("index") or 0) - 1)
        if prev_e and (prev_e.get("en_url") or prev_e.get("ko_url")):
            pcfg = load_generated(prev_key) or {}
            series_ctx = dict(series_ctx)   # 저장된 entry 변형 방지(사본 사용)
            series_ctx["prev_en_url"] = (prev_e.get("en_url") or "").strip()
            series_ctx["prev_ko_url"] = (prev_e.get("ko_url") or "").strip()
            series_ctx["prev_ko"] = series_ctx.get("prev") or (pcfg.get("ko_title") or "")
            series_ctx["prev_en"] = (pcfg.get("en_title") or series_ctx.get("prev") or "")

    try:
        cfg = _generate_multi(topic, refs, settings, n_photos, n_sections, log,
                              progress, series_ctx, captions, past=_past, word_mode=card_mode,
                              stop_check=stop_check, photo_groups=photo_groups)
    except StopRequested:
        raise   # 사용자 중단은 폴백(단일 생성)하지 않고 그대로 전파
    except Exception as e:
        log(f"   ⚠️ 다단계 생성 실패({e}) — 단일 생성으로 대체합니다.")
        progress(50.0, "단일 방식으로 다시 작성 중")
        cfg = _generate_single(topic, refs, settings, photo_names, log)

    # 필수 키 검증/보정
    required = ["en_title", "ko_title", "body_en", "body_ko"]
    for k in required:
        if not cfg.get(k):
            raise ValueError(f"LLM 응답에 '{k}'가 비어 있습니다.")
    cfg.setdefault("en_meta", "")
    cfg.setdefault("ko_meta", "")
    cfg.setdefault("en_slug", "")
    cfg.setdefault("ko_slug", "")
    cfg.setdefault("category", "Travel")
    cfg["en_labels"] = _norm_labels(cfg.get("en_labels"), "en", cfg["category"])
    cfg["ko_labels"] = _norm_labels(cfg.get("ko_labels"), "ko", cfg["category"])
    cfg["en_slug"] = _fix_slug(cfg.get("en_slug"), "en")
    cfg["ko_slug"] = _fix_slug(cfg.get("ko_slug"), "ko")
    cfg.setdefault("location", "")
    cfg["date"] = date_str
    cfg["topic"] = topic
    cfg["refs"] = refs or ""
    cfg["series"] = series_ctx or {}
    cfg["photo_dir"] = photo_dir or ""
    # 사진 기반 글: IMAGE_1..M 번호가 '그룹 순서'로 매겨졌으므로, 발행 때도 같은 순서로
    # 사진을 올려야 자리표시자와 사진이 일치한다. 그 순서를 photo_order에 저장(publish_date가 사용).
    if photo_groups:
        cfg["photo_order"] = [str(p) for p in photos]

    # 단어 글: 영어 표기 정리.
    #  · word_en(LLM)에 영어 전문용어·번역어가 있으면 그대로 사용(예: Stage Right / Stage Left, Field of View).
    #  · 비어 있으면(순수 고유어) 표준 로마자(국립국어원)로 채움 — LLM 음차 오류(발림→Baldim) 방지.
    #  · 복합어(상수 / 하수)·외래어(FOV)는 romanize가 구분자/영문을 그대로 통과시킴.
    if word_mode:
        try:
            import romanize as _rom
            wko = (cfg.get("word_ko") or "").strip()
            term = (cfg.get("word_en") or "").strip()       # LLM이 준 영어 전문용어(없으면 "")
            roman = _rom.romanize(wko) if wko else ""
            english_line = term or roman                     # 전문용어 우선, 없으면 로마자
            cfg["word_en"] = english_line
            cfg["word_roman"] = roman
            # 연관 단어 영어: 대응어 있으면 그대로, 없으면 로마자
            for r in (cfg.get("related_words") or []):
                rko = (r.get("ko") or "").strip()
                if not (r.get("en") or "").strip() and rko:
                    r["en"] = _rom.romanize(rko)
            # 슬러그를 영어 표기 기반으로(깔끔·검색친화). 한글 슬러그는 로마자.
            base = english_line or roman
            if base:
                cfg["en_slug"] = _fix_slug(base, "en")
                cfg["ko_slug"] = _fix_slug(roman or base, "ko")
        except Exception as e:
            log(f"   ⚠️ 영어 표기 정리 생략: {e}")

    # 단어 사전 글: ① 타이틀 카드(히어로, IMAGE_1) ② 연관 단어 네트워크 카드(연결된 단어 섹션, IMAGE_2).
    # 본문엔 _generate_multi 가 IMAGE_1/IMAGE_2 자리를 넣어 둠. library_photos 순서가 그 번호와 일치해야 함.
    def _strip_img(n):
        cfg["body_ko"] = re.sub(rf'<!--\s*IMAGE_{n}\s+alt="[^"]*"\s*-->\n?', '', cfg["body_ko"])
        cfg["body_en"] = re.sub(rf'<!--\s*IMAGE_{n}\s+alt="[^"]*"\s*-->\n?', '', cfg["body_en"])

    if card_mode:
        out_dir = GENERATED_DIR / date_str
        out_dir.mkdir(parents=True, exist_ok=True)
        wko = (cfg.get("word_ko") or _lead_word(cfg.get("ko_title"))).strip()
        wen = (cfg.get("word_en") or _lead_word(cfg.get("en_title"))).strip()
        # 카드 부제 = 뜻 한 줄(핵심 요약). 카드는 텍스트가 이미지 픽셀에 박히므로
        # 한/영 두 벌을 따로 만든다(한 장을 공유하면 영문 글에 한글 설명이 그대로 보임).
        sub_ko = _truncate_at_sentence(cfg.get("summary_ko") or cfg.get("ko_meta") or "")
        sub_en = _truncate_at_sentence(cfg.get("summary_en") or cfg.get("en_meta") or "")
        rel = cfg.get("related_words") or []
        hero_ko = hero_en = relcard_ko = relcard_en = ""
        try:
            import title_card
            hero_ko = title_card.make_word_card(
                wko, wen, str(out_dir / "_wordcard_ko.png"), subtitle=sub_ko, log=log)
            hero_en = title_card.make_word_card(
                wko, wen, str(out_dir / "_wordcard_en.png"), subtitle=sub_en, log=log)
            if hero_ko and hero_en and rel and "<!-- IMAGE_2 " in cfg.get("body_ko", ""):
                relcard_ko = title_card.make_related_card(
                    wko, rel, str(out_dir / "_relcard_ko.png"), lang="ko", log=log)
                relcard_en = title_card.make_related_card(
                    wko, rel, str(out_dir / "_relcard_en.png"),
                    label="CONNECTED WORDS", lang="en", log=log)
        except Exception as e:
            log(f"   ⚠️ 단어 카드 생성 생략: {e}")
        library_photos_ko = library_photos_en = []
        if hero_ko and hero_en:
            library_photos_ko = [hero_ko] + ([relcard_ko] if relcard_ko else [])
            library_photos_en = [hero_en] + ([relcard_en] if relcard_en else [])
            log(f"   🎴 단어 카드 생성(한/영 각각): {wko} / {wen}"
                + (" + 연관 단어 카드" if relcard_ko else ""))
            if not relcard_ko:
                _strip_img(2)   # 연관 카드 없음 → IMAGE_2 자리 제거
        else:
            _strip_img(1); _strip_img(2)   # 전체 실패 → 자리표시자 모두 제거
        cfg["library_photos_ko"] = library_photos_ko
        cfg["library_photos_en"] = library_photos_en

        # 카드 2장(타이틀·연관단어) 외 보강 — 관광공사·공유마당·위키미디어 등 실제 사진을
        # 본문 중간중간에 출처 캡션과 함께(실사진은 한/영이 같은 파일을 공유해도 무방).
        # 최근 사용한 이미지는 제외(블로그 내·블로그 간 중복 방지).
        _insert_context_photos_for_word_post(cfg, settings, log)

    cfg["library_photos"] = library_photos   # 라이브러리에서 자동 매칭된 실사진 경로(한/영 공유, 발행 시 업로드 폴백)

    # 사진이 전혀 없는 일반 글(단어 글 제외) — 관광공사·공공데이터·무료 이미지 사이트에서
    # 전체 1장 + 소주제별 1장을 자동으로 찾아 출처와 함께 넣는다. 예전엔 GUI의
    # '🖼 소주제별 이미지 자동 채우기' 버튼을 수동으로 눌러야만 들어갔는데, 자동 발행
    # 스케줄러(_tick_scheduler → publish_date → generate_post)는 이 버튼을 거치지 않아
    # 사진 없는 글이 그대로 발행되는 문제가 있었음(2026-07-07) — 여기서 자동 보강.
    if n_photos == 0 and not word_mode:
        try:
            _autofill_found_images(cfg, settings, log)
        except Exception as e:
            log(f"   ⚠️ 자동 이미지 검색 생략(사진 없이 발행됩니다): {e}")

    # 캐시 저장
    out_dir = GENERATED_DIR / date_str
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "config.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "post_en.html").write_text(cfg["body_en"], encoding="utf-8")
    (out_dir / "post_ko.html").write_text(cfg["body_ko"], encoding="utf-8")
    log(f"   ✅ 글 생성 완료 — 한국어 {len(cfg['body_ko']):,}자 / 영어 {len(cfg['body_en']):,}자")
    progress(100.0, "글 생성 완료")
    return cfg


def delete_generated(date_str: str) -> bool:
    """해당 날짜의 생성 캐시 폴더(generated/{date})를 통째로 삭제.
    스케줄에서 글을 삭제할 때 함께 호출 → 다음에 다시 만들면 캐시가 아니라 새로 생성된다
    (옛 생성물의 사진·본문이 그대로 재사용되는 혼선 방지)."""
    out_dir = GENERATED_DIR / date_str
    if out_dir.exists():
        shutil.rmtree(out_dir, ignore_errors=True)
        return True
    return False


def load_generated(date_str: str):
    out_dir = GENERATED_DIR / date_str
    cfg_f = out_dir / "config.json"
    if cfg_f.exists():
        try:
            return json.loads(cfg_f.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def _download_for_rehost(url: str, dest_dir: Path, idx: int) -> Path:
    """원격 이미지를 임시 파일로 내려받습니다."""
    import image_finder as imgf
    req = urllib.request.Request(url, headers={"User-Agent": imgf.UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = r.read()
    ext = ".png" if url.lower().split("?")[0].endswith(".png") else ".jpg"
    path = dest_dir / f"found_{idx}{ext}"
    path.write_bytes(data)
    return path


def rehost_found_images(items: list, log=print) -> list:
    """find_images() 결과의 외부 URL을 내려받아 Blogger CDN(우리 글의 고유 이미지)으로
    업로드하고, 각 항목의 url을 그 CDN URL로 교체한 새 리스트를 반환합니다.
    (외부 사이트 핫링크 대신 자체 호스팅해야 구글이 '우리 글의 이미지'로 인식 — SEO 목적)
    개별 다운로드/업로드 실패 항목은 원래 외부 URL을 그대로 둡니다(폴백)."""
    import publish_today as pub

    targets = [(i, it) for i, it in enumerate(items) if it and (it.get("url") or "").strip()]
    if not targets:
        return items

    tmp_dir = Path(tempfile.mkdtemp(prefix="rehost_"))
    try:
        downloaded = {}
        for i, it in targets:
            try:
                downloaded[i] = _download_for_rehost(it["url"], tmp_dir, i)
            except Exception as e:
                log(f"   ⚠️ 이미지 다운로드 실패({it.get('source','')}): {e}")
        if not downloaded:
            log("   ⚠️ 외부 이미지를 하나도 내려받지 못해 원본 링크를 그대로 사용합니다.")
            return items

        log(f"   🌐 외부 이미지 {len(downloaded)}장을 블로그 CDN으로 재호스팅 중...")
        uploaded = pub.preload_photos(list(downloaded.values()))
        path_to_cdn = {p: u for p, u in uploaded}

        out = list(items)
        ok = 0
        for i, path in downloaded.items():
            cdn = path_to_cdn.get(path)
            if cdn:
                new_it = dict(out[i])
                new_it["url"] = cdn
                out[i] = new_it
                ok += 1
        log(f"   ✅ 재호스팅 완료: {ok}/{len(downloaded)}장(실패분은 원본 링크 사용)")
        return out
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _figure_html(url: str, alt: str, caption: str) -> str:
    """본문에 넣을 이미지+출처캡션 블록(외부 이미지는 그대로 핫링크).
    HTML width="100%" 속성도 함께 박음 — 에디터에서 사진 순서·위치를 바꿔도 크기가
    원본 픽셀로 풀려 작아지는 문제를 방지(2026-07-10, publish_today.make_img_tag와 동일)."""
    alt = (alt or "").replace('"', "'")
    return (
        '<figure style="margin:0 0 18px;">'
        f'<img src="{url}" alt="{alt}" width="100%" '
        'style="width:100%;height:auto;border-radius:6px;display:block;"/>'
        '<figcaption style="font-size:12px;color:#888;margin-top:4px;'
        f'text-align:center;">{caption}</figcaption>'
        '</figure>')


def _insert_midbody_figures(body: str, figs_html: list) -> str:
    """본문 문단(</p>) 사이사이 균등한 지점에 이미지 블록을 끼워 넣는다(단어 카드처럼
    특정 <h2> 구조에 기대지 않는 범용 방식 — '중간중간'에 자연스럽게 분산).
    문단이 아예 없으면(비정상 본문) 맨 끝에 이어 붙인다."""
    if not figs_html:
        return body
    parts = re.split(r"(</p>)", body or "")
    close_idxs = [i for i, p in enumerate(parts) if p == "</p>"]
    if not close_idxs:
        return (body or "") + "\n" + "\n".join(figs_html)
    n = len(figs_html)
    positions = [close_idxs[int(len(close_idxs) * (k + 1) / (n + 1))] for k in range(n)]
    for pos, fig in sorted(zip(positions, figs_html), key=lambda t: -t[0]):
        parts.insert(pos + 1, fig)
    return "".join(parts)


_USED_IMAGES_FILE = "used_images.json"


def _load_used_image_urls(cross_blog: bool = True) -> set:
    """이미 다른 글(기본: 다른 블로그 포함)에 쓴 참고사진 원본 URL 집합(중복 방지용).
    각 블로그 프로필 폴더의 used_images.json을 모아 합집합으로 반환."""
    urls = set()
    files = list(PROFILES_DIR.glob(f"*/{_USED_IMAGES_FILE}")) if cross_blog else \
        [GENERATED_DIR.parent / _USED_IMAGES_FILE]
    for f in files:
        try:
            urls.update(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            pass
    return urls


def _record_used_image_urls(urls: list) -> None:
    """이번에 쓴 참고사진 원본 URL을 현재 활성 블로그의 기록에 추가(다음 검색부터 제외됨)."""
    urls = [u for u in urls if u]
    if not urls:
        return
    f = GENERATED_DIR.parent / _USED_IMAGES_FILE
    try:
        existing = set(json.loads(f.read_text(encoding="utf-8"))) if f.exists() else set()
    except Exception:
        existing = set()
    existing.update(urls)
    f.write_text(json.dumps(sorted(existing), ensure_ascii=False, indent=2), encoding="utf-8")


_VISUAL_Q_SYSTEM = ("당신은 사진 리서처입니다. 글에서 '실제로 사진으로 찍을 수 있는 구체적 "
                    "대상'을 뽑아 이미지 검색어를 만들어 지정한 JSON 하나만 출력하세요.")


def _visual_queries_prompt(wko, wen, summary, body_text) -> str:
    return f'''단어: "{wko}" ({wen})
뜻: {summary}
아래는 이 단어를 설명한 글의 본문입니다:
---
{body_text[:2500]}
---

이 글에 곁들일 참고 사진을 찾으려 합니다. 본문에서 **실제로 언급된, 사진으로 찍을 수 있는
구체적인 대상·물건·장면**을 4~6개 뽑아 이미지 검색어를 만드세요.
- 추상 개념(정신·아름다움·흐름)이 아니라 눈에 보이는 것만: 예) 나무의 나이테, 비단의 짜임,
  한옥 목조 기둥, 나전칠기, 전통 매듭, 도자기 물레.
- search_en은 이미지 검색 API에 넣을 짧은 영어 검색어(2~5단어, 구체적으로).
- korea: 그 대상이 한국 고유의 것(한옥·칠기·매듭 등)이면 true, 나이테·비단 질감처럼
  만국 공통 소재면 false.

아래 JSON만 출력(설명·코드펜스 없이):
{{"subjects":[{{"ko":"대상(한국어)","search_en":"image search query","korea":true}}]}}'''


def _visual_queries_for_word(cfg, settings, log=print) -> list:
    """단어 글 본문에서 '찍을 수 있는 구체적 소재'를 추출해 [{ko,search_en,korea}] 반환.
    실패 시 빈 리스트(호출부가 단어 자체 검색으로 폴백)."""
    wko = (cfg.get("word_ko") or "").strip()
    wen = (cfg.get("word_en") or "").strip()
    summary = (cfg.get("summary_ko") or cfg.get("ko_meta") or "").strip()
    body_text = re.sub(r"<[^>]+>", " ", cfg.get("body_ko") or "")
    body_text = re.sub(r"\s+", " ", body_text).strip()
    if not body_text:
        return []
    prompt = _visual_queries_prompt(wko, wen, summary, body_text)
    for attempt in range(2):
        try:
            d = _extract_json(_complete(settings, prompt, log, _VISUAL_Q_SYSTEM))
            subs = [s for s in (d.get("subjects") or [])
                    if (s.get("search_en") or "").strip()]
            if subs:
                return subs[:6]
        except Exception as e:
            log(f"      ↻ 시각 소재 추출 재시도({attempt + 1}/2): {e}")
    return []


_WISHLIST_SYSTEM = ("당신은 사진 리서처입니다. 글에서 '무료 스톡·관광 공공데이터로는 찾기 "
                    "어렵지만 운영자가 직접 찍을 수 있는 구체적 실물·질감·공정·장면'만 골라 "
                    "지정한 JSON 하나만 출력하세요.")


def _wishlist_subjects_prompt(topic, body_text) -> str:
    return f'''글 제목: "{topic}"
아래는 이 글의 본문입니다:
---
{body_text[:2800]}
---

이 글을 더 완성도 있게 만들 '직접 찍을 사진'의 소재를 3~6개 뽑으세요.
반드시 **무료 스톡 사이트나 관광 공공데이터에서는 찾기 어려운, 눈에 보이는 구체적 실물·질감·
공정·장면**만 고르세요. 예) 나무의 나이테, 나무껍질과 옹이, 비단의 짜임새, 한옥 목조 기둥,
나전칠기 표면, 전통 매듭, 도자기 물레, 발효 항아리 내부, 안료·염료, 장인의 손동작.
제외할 것: 추상 개념(정신·아름다움·조화), 유명 풍경·도시 전경·관광명소(스톡에 흔함),
공연장·건물 외관, 글의 구성 요소(맺음말·FAQ·여행 팁·전체 대표컷·관련 글).

각 소재:
- ko: 무엇을 찍는지 한국어로 짧고 구체적으로(예: "강진 청자 상감 문양 표면")
- guide: 어떻게 찍으면 좋은지 한 줄(구도·근접 여부 등)
- search_en: 참고용 짧은 영어(2~5단어)
- korea: 한국 고유 소재면 true

아래 JSON만 출력(설명·코드펜스 없이):
{{"subjects":[{{"ko":"...","guide":"...","search_en":"...","korea":true}}]}}'''


def _post_body_text(cfg: dict) -> str:
    """생성 캐시에서 본문 텍스트를 뽑아 태그 제거·공백 정리."""
    for k in ("body_ko", "ko_body", "html_ko", "body"):
        if (cfg.get(k) or "").strip():
            t = re.sub(r"<[^>]+>", " ", cfg[k])
            return re.sub(r"\s+", " ", t).strip()
    return ""


def wishlist_subjects(cfg: dict, settings: dict, log=print) -> list:
    """발행글 본문에서 '무료 스톡으로는 못 찾는, 직접 찍을 구체적 실물 소재'를 추출
    → [{ko, guide, search_en, korea}]. 촬영 위시리스트(photo_wishlist)용.
    본문이 없으면 빈 리스트(호출부가 촬영목록으로 폴백)."""
    topic = (cfg.get("ko_title") or cfg.get("word_ko") or cfg.get("topic") or "").strip()
    body_text = _post_body_text(cfg)
    if not body_text:
        return []
    prompt = _wishlist_subjects_prompt(topic, body_text)
    for attempt in range(2):
        try:
            d = _extract_json(_complete(settings, prompt, log, _WISHLIST_SYSTEM))
            subs = [s for s in (d.get("subjects") or [])
                    if (s.get("ko") or "").strip()]
            if subs:
                return subs[:6]
        except Exception as e:
            log(f"      ↻ 위시 소재 추출 재시도({attempt + 1}/2): {e}")
    return []


# 풍경·관광 사진을 걸러내는 표시어 — 소재 클로즈업(나이테·질감·공정)에는 절대 안 나오는 말들.
# 관광공사(KTO) 등이 이런 마케팅 제목의 풍경 사진을 소재 검색어에도 밀어 넣어 오탐을 만든다.
_SCENIC_WORDS = (
    "풍경", "전경", "풍광", "명소", "야경", "전망", "관광", "여행", "매력", "도시",
    "마을", "축제", "거리", "해변", "바다", "산", "강", "하늘", "노을", "일출", "일몰",
    "공원", "정원", "궁궐", "사찰", "폭포", "호수", "섬", "숲", "들판", "논밭",
    "landscape", "scenery", "scenic", "cityscape", "skyline", "aerial", "panorama",
    "view", "travel", "tourist", "tourism", "festival", "village", "beach", "coast",
    "mountain", "river", "sunset", "sunrise", "park", "garden", "palace", "temple",
    "waterfall", "lake", "island", "forest", "field", "attraction",
)


def _is_scenic_result(it: dict) -> bool:
    """이미지 결과가 '풍경·관광 사진'인지(소재 클로즈업이 아님) 제목·태그로 판별."""
    text = " ".join([str(it.get("title") or ""), str(it.get("title_ko") or ""),
                     str(it.get("tags") or "")]).lower()
    return any(w in text for w in _SCENIC_WORDS)


def _library_photo_for_subject(subject: dict, settings: dict, used_paths: set,
                               log=print):
    """내 사진 라이브러리에서 이 소재에 맞는 사진을 찾고 vision으로 실제 내용을 검증해
    반환(사진 dict, _caption_ko 포함) — 없거나 검증 실패면 None. 라이브러리 키워드 검색이
    경로 토큰으로 엉뚱한 사진(예: '나이테'에 정동 야경)을 반환하는 일이 잦아, 후보를 vision으로
    확인해 실제로 그 소재를 담은 사진만, 강한 풍경컷은 제외하고 쓴다."""
    try:
        import photo_library as photolib
        import photo_vision
    except Exception:
        return None
    ko = (subject.get("ko") or "").strip()
    en = (subject.get("search_en") or "").strip()
    cands, seen = [], set()
    for q in filter(None, [ko, en]):
        try:
            for r in photolib.search(q, n=5):
                p = (r.get("path") or "").strip()
                if p and p not in used_paths and p not in seen and Path(p).exists():
                    seen.add(p); cands.append(r)
        except Exception:
            pass
    if not cands:
        return None
    # 소재의 '핵심 명사'만 추출 — 조사 제거 + 너무 흔한 일반어 제외(예: '나무'는 야외 사진
    # 대부분에 걸려 오탐을 만들므로 매칭 키워드에서 뺀다. '나무껍질'의 '껍질'처럼 구체어만 남김).
    _GEN_KO = {"나무", "한국", "전통", "자연", "재료", "모습", "사진", "공간", "우리", "문화",
               "한옥", "기와", "지붕", "담장", "마당", "건물", "거리", "야경", "하늘", "풍경"}
    _GEN_EN = {"tree", "wood", "korea", "korean", "traditional", "nature", "natural",
               "texture", "pattern", "close", "closeup", "detail", "surface", "photo"}
    terms_ko = []
    for t in re.findall(r"[가-힣]{2,}", ko):
        t = re.sub(r"(의|와|과|을|를|이|가|은|는|에|으로|로|에서)$", "", t)
        if len(t) >= 2 and t not in _GEN_KO:
            terms_ko.append(t)
    terms_en = [t for t in re.findall(r"[a-z]{4,}", en.lower()) if t not in _GEN_EN]
    if not terms_ko and not terms_en:
        return None

    def _match(vtext):
        for t in terms_ko:                       # 한국어: 전체 또는 끝 2글자(핵심 명사) 일치
            if t in vtext or (len(t) >= 3 and t[-2:] in vtext):
                return True
        for t in terms_en:
            if t[:4] in vtext:
                return True
        return False

    for r in cands[:5]:
        try:
            v = photo_vision.caption(r["path"], settings, log=lambda *a: None,
                                     hints={"filename": Path(r["path"]).name})
        except Exception:
            continue
        vtext = (v.get("caption_ko", "") + " "
                 + " ".join(v.get("tags_ko", []) + v.get("tags_en", []))).lower()
        if not vtext or any(w in vtext for w in _SCENIC_WORDS):
            continue        # 내 사진은 '소재 클로즈업'만 — 풍경 요소가 하나라도 있으면 제외
        if _match(vtext):
            r["_caption_ko"] = v.get("caption_ko", "")
            return r
    return None


def _word_photo_root(settings: dict) -> str:
    """단어별 정리 사진 폴더의 루트. 기본: 촬영반입 폴더의 부모 아래 '정리'
    (예: D:\\Source\\한국사진\\정리). settings['word_photo_root']로 지정 가능."""
    root = (settings.get("word_photo_root") or "").strip()
    if root:
        return root
    try:
        import photo_intake
        return str(Path(photo_intake.intake_dir(settings)).parent / "정리")
    except Exception:
        return (r"D:\Source\한국사진\정리" if sys.platform == "win32"
                else str(Path.home() / "한국사진" / "정리"))


def _word_folder_photos(cfg: dict, settings: dict) -> list:
    """이 단어 전용으로 운영자가 정리해 둔 폴더(예: 정리\\결)의 사진 경로 목록.
    파일명이 소재(나이테·나무껍질·천 등)를 나타내므로 캡션에 그대로 활용한다."""
    word = (cfg.get("word_ko") or "").strip()
    if not word:
        return []
    folder = Path(_word_photo_root(settings)) / word
    if not folder.exists():
        return []
    exts = {".jpg", ".jpeg", ".png", ".webp"}
    return sorted(str(p) for p in folder.iterdir()
                  if p.is_file() and p.suffix.lower() in exts)


def _translate_captions_en(caps_ko: list, settings: dict, log=print) -> list:
    """한국어 캡션 목록을 영어로 번역(영문 블로그용). 실패 시 원본 그대로."""
    caps_ko = [c for c in caps_ko]
    try:
        prompt = ("다음 사진 캡션들을 자연스러운 영어로 번역해 JSON 배열 하나로만 출력하세요."
                  "(사진 소재 설명, 각 8단어 이내)\n"
                  + json.dumps(caps_ko, ensure_ascii=False)
                  + '\n출력: {"en":["...", ...]}')
        d = _extract_json(_complete(settings, prompt, log,
                                    "당신은 번역가입니다. JSON 하나만 출력."))
        en = d.get("en") or []
        if len(en) == len(caps_ko):
            return [str(e).strip() or caps_ko[i] for i, e in enumerate(en)]
    except Exception as e:
        log(f"      ⚠️ 캡션 영어 번역 실패(원문 사용): {e}")
    return caps_ko


def _insert_word_folder_figures(cfg: dict, settings: dict, log=print) -> bool:
    """단어 전용 정리 폴더(정리\\<단어>)에 사진이 있으면 그걸 최우선으로 본문에 삽입.
    반환: 삽입했으면 True(외부 검색 생략), 없으면 False."""
    photos = _word_folder_photos(cfg, settings)
    if not photos:
        return False
    import publish_today as pub
    photos = photos[:6]
    up = dict(pub.preload_photos([Path(p) for p in photos]))
    credit = (settings.get("photo_credit") or "").strip()
    tail = f"© {credit}" if credit else "직접 촬영"
    word = (cfg.get("word_ko") or "").strip()
    caps_ko = []
    for p in photos:
        c = _caption_from_filename(Path(p).name)
        if word:                                  # 앞에 붙은 단어("결 나이테"→"나이테") 제거
            c = re.sub(r"^" + re.escape(word) + r"\s*", "", c).strip() or c
        caps_ko.append(c)
    caps_en = _translate_captions_en(caps_ko, settings, log)
    ko_figs, en_figs, recs = [], [], []
    for p, cko, cen in zip(photos, caps_ko, caps_en):
        url = up.get(Path(p)) or up.get(str(Path(p)))
        if not url:
            continue
        ko_figs.append(_figure_html(url, cko, f"{cko} · {tail}"))
        en_figs.append(_figure_html(url, cen, f"{cen} · {tail}"))
        recs.append({"url": url, "source": "내 사진", "license": ""})
    if not ko_figs:
        return False
    cfg["body_ko"] = _insert_midbody_figures(cfg["body_ko"], ko_figs)
    cfg["body_en"] = _insert_midbody_figures(cfg["body_en"], en_figs)
    cfg.setdefault("found_images", []).extend(recs)
    cfg.setdefault("photo_dir", str(Path(_word_photo_root(settings)) / cfg.get("word_ko", "")))
    log(f"   📸 '{cfg.get('word_ko','')}' 정리 폴더의 내 사진 {len(ko_figs)}장을 본문에 넣었습니다.")
    return True


def _insert_context_photos_for_word_post(cfg: dict, settings: dict, log=print) -> None:
    """단어 사전 글: 본문에서 '사진으로 찍을 수 있는 구체적 소재'(나이테·비단 짜임·한옥 기둥
    등)를 추출해 소재별로 검색 — 예전처럼 단어 자체("Gyeol")로 검색하면 추상어라 무관한 풍경
    사진이 나오던 문제를 해결(2026-07-11). 우선순위: ① 단어 전용 정리 폴더(정리\\<단어>)의 내
    사진 → ② 관광공사·공유마당·위키미디어·무료스톡 검색(풍경 제외). 이미 쓴 사진은 제외."""
    try:
        import image_finder as imgf
        wko = (cfg.get("word_ko") or "").strip()
        wen = (cfg.get("word_en") or "").strip()
        query = wen or wko
        if not query:
            return
        # ⓞ 이 단어 전용 정리 폴더의 내 사진이 있으면 최우선 사용(외부 검색 생략)
        try:
            if _insert_word_folder_figures(cfg, settings, log):
                return
        except Exception as e:
            log(f"   ⚠️ 정리 폴더 사진 사용 실패(외부 검색으로 진행): {e}")
        used = _load_used_image_urls()

        # ① 본문에서 시각적 소재 추출 → 소재별 검색(소재당 1장, 최대 3장)
        items, used_queries = [], []
        subjects = _visual_queries_for_word(cfg, settings, log)
        if subjects:
            log("   🔎 본문 속 시각 소재로 사진 검색: "
                + ", ".join(s.get("ko") or s["search_en"] for s in subjects[:6]))
        def _relevant(q, it):
            """검색어와 결과 제목의 토큰 겹침 확인 — 관광공사 등 '느슨한 소스'가 무관한
            관광지 사진(예: tree rings → Flower Blooming Island)을 1순위로 밀어넣는 것 방지."""
            title = ((it.get("title") or "") + " " + (it.get("tags") or "")).lower()
            stop = {"and", "the", "of", "with", "korean", "korea", "traditional"}
            toks = [t for t in re.findall(r"[a-z]+", q.lower()) if len(t) >= 4 and t not in stop]
            if not toks:
                return True
            return any(t[:5] in title for t in toks)   # 어간 비슷하면 인정(rings→ring)

        seen_urls = set()
        used_lib_paths = set()
        for s in subjects:
            if len(items) >= 3:
                break
            # ① 내 사진 라이브러리 우선(vision 검증) — 단, 라이브러리에 '재료 클로즈업'이 태그돼
            #    있을 때만 안전. 태그 안 된 여행·풍경 사진이 vision 오탐으로 뽑히는 걸 막기 위해
            #    기본은 꺼 둠(settings["word_use_my_photos"]=True 로 켜면 동작). 태그된 소재
            #    사진이 쌓이면(촬영 위시리스트 워크플로우) 켜서 내 사진이 자동 우선되게 한다.
            lib = (_library_photo_for_subject(s, settings, used_lib_paths, log)
                   if settings.get("word_use_my_photos") else None)
            if lib:
                used_lib_paths.add(lib["path"])
                items.append({"_local": lib["path"],
                              "title_ko": (lib.get("_caption_ko") or s.get("ko") or "").strip(),
                              "title": s.get("search_en") or s.get("ko", ""),
                              "source": "내 사진"})
                log(f"      📸 내 사진 사용: {Path(lib['path']).name} ← {s.get('ko','')}")
                continue
            # ② 라이브러리에 없으면 외부(관광공사·공유마당·위키·무료스톡) 검색
            q = s["search_en"].strip()
            try:
                found = imgf.find_images(q, n=6, settings=settings,
                                         korea_focus=bool(s.get("korea")))
            except Exception:
                found = []
            for it in found:
                u = (it.get("url") or "").strip()
                if not u or u in used or u in seen_urls:
                    continue
                if _is_scenic_result(it):   # 풍경·관광 사진은 소재 클로즈업이 아니므로 제외
                    continue
                if not _relevant(q, it):
                    continue
                it["title_ko"] = (s.get("ko") or "").strip() or it.get("title_ko")
                items.append(it)
                seen_urls.add(u)
                used_queries.append(q)
                break

        # ③ 소재별로 '관련 있는' 사진(내 사진·외부)을 못 찾으면 사진을 넣지 않는다(단어 자체
        #    재검색하면 추상어라 무관한 풍경 사진이 들어오던 문제 — 폴백 제거). 촬영 위시리스트 대상.
        if not items:
            log(f"   📷 '{query}' 본문 소재에 정확히 맞는 사진을 못 찾아 사진 없이 둡니다"
                f"(풍경 사진으로 대체하지 않음 — 촬영 위시리스트 대상).")
            return
        # 내 사진(로컬)은 업로드, 외부는 재호스팅. 순서(=본문 배치 순서)는 유지.
        import publish_today as pub
        import photo_library as photolib
        local_paths = [it["_local"] for it in items if it.get("_local")]
        # preload_photos는 Path를 기대(내부에서 .name 사용) → Path로 감싸고 결과 키는 str로 정규화
        uploaded = {}
        if local_paths:
            for p, u in pub.preload_photos([Path(p) for p in local_paths]):
                uploaded[str(p)] = u
        ext_idx = [i for i, it in enumerate(items) if not it.get("_local")]
        orig_urls = [items[i].get("url") for i in ext_idx]   # rehost 전 외부 원본 URL
        if ext_idx:
            ext_list = _translate_titles_ko([items[i] for i in ext_idx], settings, log)
            ext_list = rehost_found_images(ext_list, log)
            for i, newit in zip(ext_idx, ext_list):
                items[i] = newit
        for it in items:
            if it.get("_local"):
                it["url"] = uploaded.get(it["_local"], "")
        ko_figs, en_figs = [], []
        for it in items:
            url = (it.get("url") or "").strip()
            if not url:
                continue
            if it.get("_local"):
                # 내 사진: 캡션 = 사진 내용 설명(vision) + (설정된 크레딧이 있으면 덧붙임)
                credit = photolib.photo_caption({"path": it["_local"]}, settings, "ko")
                desc = (it.get("title_ko") or query).strip()
                cap = f"{desc} · {credit}" if credit else desc
                ko_figs.append(_figure_html(url, desc, cap))
                en_figs.append(_figure_html(url, desc, cap))
            else:
                alt_en = it.get("title") or query
                alt_ko = it.get("title_ko") or alt_en
                ko_figs.append(_figure_html(url, alt_ko, imgf.attribution_caption(it, "ko")))
                en_figs.append(_figure_html(url, alt_en, imgf.attribution_caption(it, "en")))
        if not ko_figs:
            return
        cfg["body_ko"] = _insert_midbody_figures(cfg["body_ko"], ko_figs)
        cfg["body_en"] = _insert_midbody_figures(cfg["body_en"], en_figs)
        cfg.setdefault("found_images", []).extend(
            {"url": it.get("url"), "source": it.get("source") or "내 사진",
             "license": it.get("license", "")}
            for it in items)
        if orig_urls:
            _record_used_image_urls([u for u in orig_urls if u])
        n_mine = sum(1 for it in items if it.get("_local"))
        log(f"   📷 참고 사진 {len(ko_figs)}장 삽입(내 사진 {n_mine} / 외부 {len(ko_figs) - n_mine}).")
    except Exception as e:
        log(f"   ⚠️ 참고 사진 삽입 생략: {e}")


def _dedupe_items(items: list, log=print) -> list:
    """같은 사진(원본 URL 기준)이 한 글에 두 번 들어가지 않도록, 이미 나온 URL이
    다시 나오면 그 자리를 빈 슬롯으로 비웁니다. (소주제별 자동 채우기에서 비슷한
    소주제끼리 같은 사진이 중복 선택되는 경우가 있어 추가)"""
    seen = set()
    out = []
    dropped = 0
    for it in items:
        u = (it.get("url") or "").strip() if it else ""
        if u and u in seen:
            out.append(None)
            dropped += 1
            continue
        if u:
            seen.add(u)
        out.append(it)
    if dropped:
        log(f"   🔁 같은 사진 {dropped}장이 중복 선택돼 제외했습니다.")
    return out


TRANSLATE_TITLE_SYSTEM = (
    "당신은 사진 설명을 한국어로 옮기는 번역가입니다. 영어 사진 설명을 자연스러운 "
    "한국어 한 문장으로 번역합니다. 지정한 JSON 하나만 출력하세요."
)


def _translate_titles_ko(items: list, settings: dict, log=print) -> list:
    """영어로만 있는 사진 제목(title)을 한국어 캡션용으로 번역해 item['title_ko']에 채워 넣음.
    지명·건축물 등 고유명사는 한국어 정식 명칭으로 옮기고, Unsplash·Pexels 같은
    사이트/브랜드 이름은 번역문에 절대 등장하지 않게 한다(한국어 본문에만 적용,
    영어 본문 캡션은 원래 영어 title을 그대로 사용)."""
    targets = [(i, it) for i, it in enumerate(items)
               if it and (it.get("title") or "").strip() and not it.get("title_ko")]
    if not targets:
        return items
    lines = "\n".join(f"{n}. {it['title']}" for n, (i, it) in enumerate(targets, 1))
    prompt = f"""다음은 사진 제목(영어) 목록입니다. 각각을 한국 블로그 사진 캡션에 쓸
자연스러운 한국어 한 문장으로 번역하세요.
- 지명·건축물 등 고유명사는 널리 쓰이는 한국어 명칭으로 쓰세요(예: Gyeongbokgung Palace → 경복궁).
- Unsplash, Pexels, Pixabay 같은 사이트/브랜드 이름은 절대 번역문에 넣지 마세요.
- 번역문만 쓰고 부가 설명·따옴표는 쓰지 마세요.

{lines}

아래 JSON만 출력(설명 없이):
{{"translations": ["번역1", "번역2", ...]}}
{JSON_SAFE}"""
    try:
        d = _extract_json(_complete(settings, prompt, log, TRANSLATE_TITLE_SYSTEM))
        trans = d.get("translations") or []
        done = 0
        for (i, it), ko in zip(targets, trans):
            t = (ko or "").strip()
            if t:
                items[i] = dict(it, title_ko=t)
                done += 1
        if done:
            log(f"   🌐 사진 제목 {done}개를 한글로 번역했습니다.")
    except Exception as e:
        log(f"   ⚠️ 사진 제목 한글 번역 실패(영문 그대로 사용): {e}")
    return items


def insert_images_into_generated(date_str: str, items: list, settings: dict = None,
                                  log=print) -> int:
    """찾은 이미지(items)를 그 날짜 생성글 본문 '맨 위'에 출처 캡션과 함께 삽입하고 재저장.
    한국어 본문엔 한국어 출처, 영어 본문엔 영어 출처를 붙입니다. 삽입한 장수를 반환."""
    import image_finder as imgf
    cfg = load_generated(date_str)
    if not cfg:
        log("   ⚠️ 생성된 글이 없어 이미지를 넣을 수 없습니다. 먼저 글을 생성하세요.")
        return 0
    items = _dedupe_items(items, log)
    items = _translate_titles_ko(items, settings or {}, log)
    items = rehost_found_images(items, log)
    ko_figs, en_figs, recs = [], [], []
    for it in items:
        if not it:
            continue
        url = (it.get("url") or "").strip()
        if not url:
            continue
        alt_en = it.get("title") or ""
        alt_ko = it.get("title_ko") or alt_en
        ko_figs.append(_figure_html(url, alt_ko, imgf.attribution_caption(it, "ko")))
        en_figs.append(_figure_html(url, alt_en, imgf.attribution_caption(it, "en")))
        recs.append({"url": url, "source": it.get("source", ""),
                     "license": it.get("license", ""), "source_page": it.get("source_page", "")})
    if not ko_figs:
        return 0
    cfg["body_ko"] = "\n".join(ko_figs) + "\n" + cfg.get("body_ko", "")
    cfg["body_en"] = "\n".join(en_figs) + "\n" + cfg.get("body_en", "")
    cfg.setdefault("found_images", []).extend(recs)   # 출처 추적용
    out_dir = GENERATED_DIR / date_str
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "config.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "post_en.html").write_text(cfg["body_en"], encoding="utf-8")
    (out_dir / "post_ko.html").write_text(cfg["body_ko"], encoding="utf-8")
    log(f"   🖼 이미지 {len(ko_figs)}장을 글 맨 위에 출처와 함께 삽입했습니다.")
    return len(ko_figs)


_TRAILING_FIGS_RE = re.compile(r'(?:\s*<figure[^>]*>.*?</figure>\s*)+$', re.S)
_LEADING_FIGS_RE = re.compile(r'^(?:\s*<figure[^>]*>.*?</figure>\s*)+', re.S)


def _place_by_section(body: str, items: list, lang: str) -> str:
    """items[0]=전체(히어로, 도입부 뒤), items[1:]=소주제별(각 <h2> 뒤)로 이미지+출처 삽입.
    None인 슬롯은 건너뜀. 기존 IMAGE 자리표시자와, 같은 자리에 이전 실행이 넣어둔
    사진(다시 실행할 때 누적되는 것 방지)도 새로 넣기 전에 먼저 제거한다."""
    import image_finder as imgf
    body = re.sub(r"<!--\s*IMAGE_\d+[^>]*-->\s*", "", body or "")

    def fig(it):
        if not it:
            return ""
        alt = it.get("title_ko") if lang == "ko" and it.get("title_ko") else it.get("title", "")
        return "\n" + _figure_html(it.get("url", ""), alt,
                                   imgf.attribution_caption(it, lang)) + "\n"

    parts = re.split(r"(<h2>.*?</h2>)", body, flags=re.S)
    parts[0] = _TRAILING_FIGS_RE.sub("", parts[0])     # 이전 히어로 사진 제거(누적 방지)
    out = [parts[0]]
    if items and items[0]:
        out.append(fig(items[0]))              # 히어로
    si, i = 1, 1
    while i < len(parts):
        out.append(parts[i])                   # <h2>
        if i + 1 < len(parts):
            parts[i + 1] = _LEADING_FIGS_RE.sub("", parts[i + 1])  # 이전 소주제 사진 제거
        if si < len(items):
            out.append(fig(items[si]))
        si += 1
        if i + 1 < len(parts):
            out.append(parts[i + 1])
        i += 2
    return "".join(out)


def _autofill_found_images(cfg: dict, settings: dict, log=print) -> int:
    """사진이 전혀 없는 글(cfg, 아직 디스크에 저장 전)에 전체 1장 + 소주제별 1장을
    관광공사·공공데이터·무료 이미지 사이트에서 찾아 출처 캡션과 함께 넣는다.
    GUI의 '🖼 소주제별 이미지 자동 채우기'(insert_found_images_by_section)와 같은
    로직이지만, 그쪽은 이미 저장된 글을 디스크에서 다시 읽어와 처리하는 반면 이 함수는
    generate_post() 안에서 저장 직전 cfg를 그 자리에서 바로 채운다(중복 디스크 I/O 방지)."""
    import photo_plan as pplan
    import image_finder as imgf
    log("   🔎 사진이 없는 글 — 관광공사·공공데이터·무료 이미지에서 자동 검색합니다...")
    shots = pplan.generate_shot_list(cfg, settings, log)
    used = _load_used_image_urls()
    items = []
    for s in shots:
        q = s.get("search_en") or s.get("heading") or cfg.get("en_title", "")
        found = imgf.find_images(q, n=4, settings=settings)
        found = [it for it in found if (it.get("url") or "").strip() not in used]
        items.append(found[0] if found else None)
    placed = sum(1 for it in items if it)
    if not placed:
        log("   ⚠️ 이 글에 맞는 무료 이미지를 찾지 못했습니다 — 사진 없이 발행됩니다.")
        return 0
    items = _dedupe_items(items, log)
    items = _translate_titles_ko(items, settings, log)
    items = rehost_found_images(items, log)
    cfg["body_ko"] = _place_by_section(cfg.get("body_ko", ""), items, "ko")
    cfg["body_en"] = _place_by_section(cfg.get("body_en", ""), items, "en")
    recs = [{"url": it.get("url"), "source": it.get("source"), "license": it.get("license")}
            for it in items if it]
    cfg.setdefault("found_images", []).extend(recs)
    _record_used_image_urls([r["url"] for r in recs if r.get("url")])
    n = sum(1 for it in items if it)
    log(f"   ✅ 이미지 {n}장을 전체·소주제별로 자동 삽입했습니다(출처 포함).")
    return n


def insert_found_images_by_section(date_str: str, items: list, settings: dict = None,
                                    log=print) -> int:
    """찾은 이미지를 '전체 1장 + 소주제별 1장'으로 글에 배치(외부 핫링크 + 출처 캡션).
    items: [전체, 소주제1, 소주제2, ...] (각 항목은 find_images 결과 dict 또는 None)."""
    cfg = load_generated(date_str)
    if not cfg:
        log("   ⚠️ 생성된 글이 없습니다.")
        return 0
    placed = sum(1 for it in items if it)
    if not placed:
        return 0
    items = _dedupe_items(items, log)
    placed = sum(1 for it in items if it)
    if not placed:
        return 0
    items = _translate_titles_ko(items, settings or {}, log)
    items = rehost_found_images(items, log)
    cfg["body_ko"] = _place_by_section(cfg.get("body_ko", ""), items, "ko")
    cfg["body_en"] = _place_by_section(cfg.get("body_en", ""), items, "en")
    cfg.setdefault("found_images", []).extend(
        {"url": it.get("url"), "source": it.get("source"), "license": it.get("license")}
        for it in items if it)
    out_dir = GENERATED_DIR / date_str
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "config.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "post_en.html").write_text(cfg["body_en"], encoding="utf-8")
    (out_dir / "post_ko.html").write_text(cfg["body_ko"], encoding="utf-8")
    log(f"   🖼 소주제별 이미지 {placed}장 배치 완료(출처 표기 포함).")
    return placed


# ══════════════════════════════════════════════════════════════════════════════
#  시리즈 기획 — 키워드 기반 연결성 있는 N편 설계
# ══════════════════════════════════════════════════════════════════════════════

SERIES_SYSTEM = (
    "당신은 한국 인바운드 여행 콘텐츠 전략가입니다. 외국인 관광객의 검색 수요(SEO)를 바탕으로 "
    "연결성 있는 블로그 시리즈를 기획합니다. 각 편은 독립적으로 읽히되 이전 편을 잇고 다음 편을 "
    "궁금하게 만들어야 합니다. 지정한 JSON 하나만 출력하세요."
)


def _series_prompt(theme, n, seeds, identity, anchor_topics=None, request="",
                   photo_context="") -> str:
    seed_str = ", ".join(seeds[:16]) if seeds else "(없음 — 블로그 색깔에 맞게 선정)"
    theme_line = (f"중심 테마: {theme}" if (theme or "").strip()
                  else "테마는 자유(블로그 색깔 안에서).")
    req_block = ""
    if (request or "").strip():
        req_block = ("[사용자 요청사항 — 가장 먼저·반드시 반영]\n"
                     f"{request.strip()}\n"
                     "→ 이 요청을 시리즈 주제·구성·각 편 내용에 최우선으로 반영하세요.\n\n")
    photo_block = ""
    if (photo_context or "").strip():
        photo_block = (
            "[실제로 촬영한 사진 폴더에 담긴 내용 — 이 시리즈는 이 사진들로 뒷받침되어야 하므로,\n"
            "각 편의 소재(장소·장면)가 아래 내용에서 최대한 벗어나지 않게 기획하세요. "
            "사진에 없는 무관한 소재를 지어내 편을 채우지 마세요.]\n"
            f"{photo_context.strip()}\n\n")
    anchor_line = ""
    if anchor_topics:
        if (request or "").strip():
            # 사용자가 구체적 요청을 한 경우, 평소 주제는 '참고만' — 안 맞으면 절대 억지로 안 엮음.
            # (실제 사진이 있는 여행기 요청에 무관한 요일 주제(민화·무용 등)가 억지로 섞이는
            #  문제가 있어 2026-07-03 완화)
            anchor_line = ("이 블로그가 평소 다루는 다른 주제(참고만 하고, 위 사용자 요청과 "
                           "안 맞으면 절대 끌어오지 말 것): " + "; ".join(anchor_topics) + "\n")
        else:
            anchor_line = "평소 다루는 주제(연관시킬 것): " + "; ".join(anchor_topics) + "\n"
    return f"""블로그 색깔: {identity}
{req_block}{photo_block}{theme_line}
{anchor_line}참고 키워드: {seed_str}
{DIFF_RULE}

위 색깔과 '사용자 요청사항'을 우선해 '연결성 있고 차별화된' {n}편 시리즈를 기획하세요. 아래 JSON만 출력(설명·코드펜스 없이):

{{
  "series_title_ko": "...",
  "series_title_en": "...",
  "theme": "한 줄 설명",
  "keywords": ["대표 SEO 키워드 5~8개(영어)"],
  "posts": [
    {{"title_ko": "1편 한국어 제목", "title_en": "1편 영어 제목", "keyword": "핵심 키워드", "hook": "후킹 한 줄", "summary": "다룰 내용 1~2문장"}}
  ]
}}

규칙:
- posts 는 정확히 {n}개. 하나의 큰 줄기 안에서 논리적으로 연결되게 배열.
- 뻔한 관광 정보 금지 — 덜 알려진 예술·장인·문화 이야기 우선. 제목에 고유명사·구체성.
- keyword 는 문화에 관심 있는 외국인이 검색할 영어 위주.
- 사용자 요청사항이 구체적인 장소·소재를 다루고 있다면, 그와 무관한 다른 소재(예: 평소 주제 목록의
  다른 항목)를 억지로 갖다 붙이지 마세요. 제목·요약 모두 요청사항의 실제 대상에서 벗어나지 말 것.
- 사진 폴더 내용이 주어졌다면, 그 사진들에 실제로 담긴 장소·장면을 각 편에 최대한 나눠 반영하세요.
{JSON_SAFE}"""


def plan_series(theme, n, settings, log=print, progress=None, anchor_topics=None,
                request="", stop_check=None, photo_dir: str = None) -> dict:
    """시리즈를 기획해 dict로 반환(저장은 안 함).
    theme: 중심 테마/키워드(선택). request: 사용자가 직접 쓴 기획 요청사항(최우선 반영).
    anchor_topics: 주간 요일별 주제 등, 시리즈가 연관돼야 할 블로그의 평소 관심사.
    photo_dir: 실제 촬영한 사진 폴더(선택) — 지정하면 그 사진들의 실제 내용을 vision으로
    파악해 시리즈 기획 자체를 그 내용에 맞춰 짠다(사진과 무관한 소재가 섞이는 문제 방지).
    stop_check: 호출 시 True를 반환하면 재시도 전에 중단(진행 중인 LLM 응답 1회는 끝까지
    기다리되, 그다음 재시도는 하지 않음 — 사용자가 편수 등을 잘못 입력했을 때 멈추는 용도)."""
    progress = progress or (lambda *a, **k: None)
    stop_check = stop_check or (lambda: False)
    n = max(3, int(n))
    engine = settings.get("llm", "gemma4")
    log(f"   🎬 시리즈 기획 시작 ({engine}) — {n}편 (블로그 색깔 반영)")
    if (request or "").strip():
        log(f"   📝 요청사항 반영: {request.strip()[:60]}")
    photo_context = ""
    photo_folders = []
    if (photo_dir or "").strip():
        try:
            photo_folders = _series_photo_folders(photo_dir)
            if len(photo_folders) > 1:
                log(f"   👁 사진 폴더 분석 중 — 하위 폴더 {len(photo_folders)}개(편별 소재로 매칭)...")
                blocks = []
                for folder in photo_folders:
                    fphotos = resolve_photos("", folder, False)
                    if not fphotos:
                        continue
                    desc = _describe_photos(fphotos, settings, log, max_analyze=2)
                    if desc:
                        body = "\n".join(desc.split("\n")[1:])   # 안내문구 줄 제외, 항목만
                        blocks.append(f"[폴더 '{Path(folder).name}']\n{body}")
                if blocks:
                    photo_context = (
                        f"이 폴더에는 하위 폴더가 {len(photo_folders)}개 있습니다 — 가능하면 "
                        "각 편이 그중 하나씩을 중심으로 다루도록 편성하세요.\n\n" + "\n\n".join(blocks))
            else:
                photos = resolve_photos("", photo_dir, False)
                if photos:
                    log(f"   👁 사진 폴더 분석 중 — {len(photos)}장 중 대표 사진 내용 파악...")
                    photo_context = _describe_photos(photos, settings, log)
                else:
                    log(f"   ⚠️ 사진 폴더에서 사진을 찾지 못했습니다: {photo_dir}")
        except Exception as e:
            log(f"   ⚠️ 사진 폴더 분석 생략: {e}")
    progress(10.0, "블로그 색깔·요청사항 기반 시리즈 설계 중")
    if engine != "claude" and not ensure_ollama_running(settings, log):
        raise RuntimeError("Ollama 서버를 사용할 수 없습니다.")
    seeds = settings.get("seed_keywords", [])
    prompt = _series_prompt(theme, n, seeds, _identity(settings), anchor_topics, request,
                            photo_context=photo_context)
    last_err = None
    for attempt in range(3):
        if stop_check():
            log("   ■ 사용자 요청으로 중단했습니다.")
            raise StopRequested("사용자가 중단했습니다.")
        if attempt:
            log(f"   ↻ 시리즈 기획 재시도 {attempt + 1}/3...")
            progress(10.0 + attempt * 10, f"재시도 {attempt + 1}/3")
        try:
            plan = _extract_json(_complete(settings, prompt, log, SERIES_SYSTEM))
            posts = plan.get("posts") or []
            if posts:
                progress(100.0, "시리즈 기획 완료")
                log(f"   ✅ 시리즈 기획 완료 — {len(posts)}편: {plan.get('series_title_ko', '')}")
                return plan
            last_err = "posts가 비어 있음"
        except Exception as e:
            last_err = str(e)
    raise RuntimeError(f"시리즈 기획 결과를 해석하지 못했습니다: {last_err}")


def apply_series_to_calendar(data, plan, start_date_str, interval_days=1, log=print,
                             photo_dir: str = None):
    """기획된 시리즈를 캘린더 날짜에 배정(각 entry에 series 맥락 저장).
    이미 발행된 날짜는 덮어쓰지 않고 건너뛰며(조용히 넘기지 않고 로그로 알림).
    photo_dir가 있으면(시리즈 기획 때 지정한 사진 폴더):
      · 바로 아래에 사진이 있는 하위 폴더가 여럿이면 편마다 **서로 다른 하위 폴더를 1:1로**
        배정(장소/소재별 하위 폴더 구조에 맞춤 — 여러 편이 같은 사진을 공유하지 않게).
      · 하위 폴더가 없으면(사진이 photo_dir 바로 안에 있음) 기존처럼 모든 편이 공유.
    개별 생성 시에도 배정된 폴더 사진이 자동으로 쓰이고, 실제 내용이 다시 반영됨(_describe_photos).
    반환: (series_id, [배정된 날짜...], [건너뛴 날짜...])"""
    posts = plan.get("posts") or []
    if not posts:
        raise ValueError("배정할 글(posts)이 없습니다.")
    total = len(posts)
    sid = datetime.now().strftime("S%Y%m%d%H%M%S")
    series_title = (plan.get("series_title_ko") or plan.get("series_title_en") or "시리즈").strip()

    photo_folders = _series_photo_folders(photo_dir) if (photo_dir or "").strip() else []
    per_post_folder = len(photo_folders) > 1   # 하위 폴더 여럿 → 편마다 1:1, 아니면 전체 공유
    folder_assignments = {}   # i(0-based) -> 배정된 폴더 경로
    if per_post_folder:
        remaining = list(photo_folders)
        for i, post in enumerate(plan.get("posts") or []):
            if not remaining:
                break
            text = f"{post.get('title_ko','')} {post.get('title_en','')} {post.get('keyword','')}"
            best, best_score = _best_matching_folder(text, remaining)
            if best and best_score > 0:
                folder_assignments[i] = best
                remaining.remove(best)
        unmatched = total - len(folder_assignments)
        log(f"   📷 하위 폴더 {len(photo_folders)}개 중 {len(folder_assignments)}개 편을 "
            f"주제와 이름이 맞는 폴더로 배정했습니다"
            + (f" (나머지 {unmatched}편은 맞는 폴더를 못 찾아 라이브러리 자동매칭에 맡깁니다)"
               if unmatched > 0 else "") + ".")

    data.setdefault("series", {})[sid] = {
        "title": series_title,
        "title_en": plan.get("series_title_en", ""),
        "theme": plan.get("theme", ""),
        "keywords": plan.get("keywords", []),
        "created": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "posts": posts,
    }

    def ktitle(p):
        return (p.get("title_ko") or p.get("title_en") or "").strip()

    def etitle(p):
        return (p.get("title_en") or p.get("title_ko") or "").strip()

    start = datetime.strptime(start_date_str, "%Y-%m-%d").date()
    interval = max(1, int(interval_days))
    assigned, skipped = [], []
    for i, post in enumerate(posts):
        ds = (start + timedelta(days=i * interval)).isoformat()
        e = data["entries"].setdefault(ds, {})
        if e.get("status") == ST_PUBLISHED:
            # 이미 발행된 날은 건너뜀(덮어쓰지 않음) — 조용히 넘기지 않고 알려줌
            skipped.append(ds)
            continue
        e["topic"] = ktitle(post) or f"{series_title} {i + 1}편"
        parts = []
        if post.get("keyword"):
            parts.append(f"핵심 키워드: {post['keyword']}")
        if post.get("summary"):
            parts.append(post["summary"])
        if post.get("hook"):
            parts.append(f"후킹: {post['hook']}")
        if parts:
            e["refs"] = " / ".join(parts)
        if per_post_folder:
            if i in folder_assignments:
                e["photo_dir"] = folder_assignments[i]
            # else: 이 편에 맞는 폴더를 못 찾음 — 사진 폴더 미지정(라이브러리 자동매칭 폴백)
        elif (photo_dir or "").strip():
            e["photo_dir"] = photo_dir.strip()
        e["series"] = {
            "sid": sid, "title": series_title, "index": i + 1, "total": total,
            "prev": ktitle(posts[i - 1]) if i > 0 else "",
            "next": ktitle(posts[i + 1]) if i < total - 1 else "",
            "next_en": etitle(posts[i + 1]) if i < total - 1 else "",
            "keyword": post.get("keyword", ""),
            "hook": post.get("hook", ""),
            "title_en": etitle(post),
        }
        e["status"] = ST_PENDING   # 시리즈 맥락으로 새로 써야 하므로 대기로
        assigned.append(ds)

    save_schedule(data)
    if skipped:
        log(f"   ⏭ 이미 발행된 날짜 {len(skipped)}개는 덮어쓰지 않고 건너뛰었습니다: "
            f"{', '.join(skipped[:5])}{' 외' if len(skipped) > 5 else ''}")
    return sid, assigned, skipped


def _future_weekday_dates(wd: int, count: int, today: date = None) -> list:
    """오늘(포함) 이후로 요일 wd(월=0..일=6)에 해당하는 날짜를 count개 ISO 문자열로."""
    today = today or date.today()
    delta = (wd - today.weekday()) % 7
    first = today + timedelta(days=delta)   # 오늘이 그 요일이면 오늘부터
    return [(first + timedelta(days=7 * i)).isoformat() for i in range(count)]


def _fetch_series_titles(theme: str, need: int, settings: dict, used: set, log=print) -> list:
    """plan_series를 배치(~12편씩)로 반복 호출해 이미 쓴 제목과 안 겹치는 새 제목을 need개까지
    모은다(plan_calendar_titles/plan_calendar_titles_range 공통 로직). 모자라도 있는 만큼 반환.
    used에는 새로 만든 제목도 계속 누적(다음 요일 호출에서도 중복 회피에 쓰임)."""
    titles = []
    guard = 0
    while len(titles) < need and guard < 8:
        guard += 1
        batch_n = min(12, need - len(titles))
        avoid = ", ".join(list(used)[-24:])
        req = (f"이미 쓴 제목과 겹치지 않는 새로운 편들로 기획하세요. "
               f"피해야 할 제목: {avoid}" if avoid else "")
        try:
            plan = plan_series(theme, max(3, batch_n), settings, log=lambda *a: None,
                               anchor_topics=[theme], request=req)
        except Exception as e:
            log(f"      ⚠️ 제목 기획 실패: {e}")
            break
        posts = plan.get("posts") or []
        if not posts:
            break
        added = 0
        for p in posts:
            tko = (p.get("title_ko") or "").strip()
            ten = (p.get("title_en") or "").strip()
            title = tko or ten
            if title and title not in used:
                titles.append({"ko": tko, "en": ten})
                used.add(title)
                added += 1
            if len(titles) >= need:
                break
        if added == 0:        # 더 못 만들면 중단(무한루프 방지)
            break
    return titles


def _collect_used_titles(data: dict) -> set:
    """중복 방지용 누적 제목(기존 발행/생성 글의 한/영 제목·주제)."""
    used = set()
    for r in past_titles(data, n=200):
        for k in ("ko", "en", "topic"):
            t = (r.get(k) or "").strip()
            if t:
                used.add(t)
    return used


def plan_calendar_titles(data: dict, settings: dict, months: int = 1,
                         log=print, progress=None, overwrite: bool = False) -> dict:
    """활성 요일마다 시리즈 흐름에 맞는 '제목'을 미리 생성해 미래 빈 날짜에 배치(제목만, 가볍게).
    로컬 LLM만 사용(Blogger API 미사용 → 발행 쿼터와 무관). 본문은 나중에 각 날짜 [지금 생성]으로.
    이미 발행됐거나 주제가 있는 날짜는 건너뜀(overwrite=False). 반환: {요일이름: 배치수}."""
    progress = progress or (lambda *a, **k: None)
    months = max(1, int(months))
    per_weekday = max(1, round(months * 4.345))   # 한 요일의 미래 발행 횟수(대략)
    active = [wd for wd in range(7) if get_weekly(data, wd).get("enabled")]
    if not active:
        raise RuntimeError("활성화된 요일이 없습니다. 주간 요일별 발행에서 요일을 켜세요.")

    used = _collect_used_titles(data)
    log(f"   🔮 제목 사전생성 — 활성 {len(active)}요일 × 약 {per_weekday}편 (≈{months}개월)")
    summary = {}
    for si, wd in enumerate(active):
        w = get_weekly(data, wd)
        theme = (w.get("topic") or "").strip() or WEEKDAY_KO[wd]
        progress(si / len(active) * 100.0, f"{WEEKDAY_KO[wd]}요일 제목 기획 중")
        log(f"   · {WEEKDAY_KO[wd]}요일 — 테마: {theme[:34]}")

        titles = _fetch_series_titles(theme, per_weekday, settings, used, log)

        # 미래 그 요일 빈 날짜에 순서대로 배치
        dates = _future_weekday_dates(wd, per_weekday * 2 + 12)
        placed, di = 0, 0
        for t in titles:
            while di < len(dates):
                ds = dates[di]; di += 1
                e = data["entries"].get(ds)
                if e and (e.get("status") == ST_PUBLISHED
                          or (e.get("topic") and not overwrite)):
                    continue   # 발행됐거나 이미 주제 있음 → 건너뜀
                entry = data["entries"].setdefault(ds, {})
                entry["topic"] = t["ko"] or t["en"]
                if t.get("en"):
                    entry["planned_title_en"] = t["en"]
                entry["status"] = ST_PENDING
                entry["planned"] = True
                placed += 1
                break
        summary[WEEKDAY_KO[wd]] = placed
        log(f"   ✅ {WEEKDAY_KO[wd]}요일 — {placed}편 배치")

    save_schedule(data)
    progress(100.0, "제목 사전생성 완료")
    return summary


def _weekday_dates_in_range(wd: int, start: date, end: date) -> list:
    """start~end(둘 다 포함) 범위 안에서 요일 wd(월=0..일=6)에 해당하는 날짜를 ISO 문자열로."""
    if start > end:
        return []
    delta = (wd - start.weekday()) % 7
    first = start + timedelta(days=delta)
    out, d = [], first
    while d <= end:
        out.append(d.isoformat())
        d += timedelta(days=7)
    return out


def plan_calendar_titles_range(data: dict, settings: dict, start_date: str, end_date: str,
                               log=print, progress=None, overwrite: bool = False) -> dict:
    """plan_calendar_titles()의 '기간 지정' 버전 — 미래가 아니라 [start_date, end_date]
    범위(과거 날짜 포함)의 그 요일 날짜에 제목을 채운다. 지난 날짜를 요일별 발행 설정에 맞춰
    한꺼번에 백필(backfill)할 때 사용. 로컬 LLM만 사용. 반환: {요일이름: 배치수}."""
    progress = progress or (lambda *a, **k: None)
    try:
        sd = datetime.strptime(start_date, "%Y-%m-%d").date()
        ed = datetime.strptime(end_date, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError("날짜 형식이 올바르지 않습니다(YYYY-MM-DD).")
    if sd > ed:
        raise ValueError("시작일이 종료일보다 늦습니다.")
    active = [wd for wd in range(7) if get_weekly(data, wd).get("enabled")]
    if not active:
        raise RuntimeError("활성화된 요일이 없습니다. 주간 요일별 발행에서 요일을 켜세요.")

    used = _collect_used_titles(data)
    log(f"   🔮 제목 사전생성(기간지정) — {start_date} ~ {end_date}, 활성 {len(active)}요일")
    summary = {}
    for si, wd in enumerate(active):
        w = get_weekly(data, wd)
        theme = (w.get("topic") or "").strip() or WEEKDAY_KO[wd]
        progress(si / len(active) * 100.0, f"{WEEKDAY_KO[wd]}요일 제목 기획 중")

        dates = _weekday_dates_in_range(wd, sd, ed)
        target_dates = [ds for ds in dates if not (
            data["entries"].get(ds) and (
                data["entries"][ds].get("status") == ST_PUBLISHED
                or (data["entries"][ds].get("topic") and not overwrite)))]
        if not target_dates:
            summary[WEEKDAY_KO[wd]] = 0
            continue
        log(f"   · {WEEKDAY_KO[wd]}요일 — 테마: {theme[:34]} ({len(target_dates)}일 채울 예정)")

        titles = _fetch_series_titles(theme, len(target_dates), settings, used, log)
        placed = 0
        for ds, t in zip(target_dates, titles):
            entry = data["entries"].setdefault(ds, {})
            entry["topic"] = t["ko"] or t["en"]
            if t.get("en"):
                entry["planned_title_en"] = t["en"]
            entry["status"] = ST_PENDING
            entry["planned"] = True
            placed += 1
        summary[WEEKDAY_KO[wd]] = placed
        log(f"   ✅ {WEEKDAY_KO[wd]}요일 — {placed}편 배치")

    save_schedule(data)
    progress(100.0, "제목 사전생성(기간지정) 완료")
    return summary


# ══════════════════════════════════════════════════════════════════════════════
#  관심 키워드 조사 — 카테고리별 1~10위
# ══════════════════════════════════════════════════════════════════════════════

# 시리즈 중심 테마를 고를 5개 카테고리(블로그의 큰 정체성 틀)
CATEGORIES = {
    "Traditional Arts": "한국 전통예술·공예(민화·한지·도자기·나전·매듭·단청·서예 등)",
    "Performances":     "한국 전통·현대 공연예술(국악·탈춤·판소리·사물놀이·전통무용·공연 관람 등)",
    "Regional Culture": "한국 지역 문화(지역 축제·전통마을·장인 공방·향토 문화유산 등)",
    "Korea 101":        "한국을 처음 접하는 외국인을 위한 문화 기초(예절·명절·한글·생활문화·전통의 의미 등)",
    "Travel":           "문화·예술 관점의 한국 여행(여행지·코스·체험)",
}

KEYWORD_SYSTEM = (
    "당신은 한국 인바운드 콘텐츠 SEO 리서처입니다. 외국인이 관심 갖고 검색하는 키워드를 "
    "카테고리별로 추려 인기·관심도 순위로 제시합니다. 지정한 JSON 하나만 출력하세요."
)


def _keyword_prompt(category, cat_desc, seeds, identity, n) -> str:
    seed_str = ", ".join(seeds[:16]) if seeds else "(없음)"
    return f"""블로그 색깔: {identity}
카테고리: {category} — {cat_desc}
참고 키워드: {seed_str}
{DIFF_RULE}

한국에 관심 있는 외국인이 '요즘 관심 갖고 검색'하는, 이 카테고리·블로그 색깔에 맞는 키워드를
인기·관심도 높은 순으로 정확히 {n}개 뽑으세요. 흔하고 뻔한 것보다 매력적이고 차별화된 것을 우선.
아래 JSON만 출력(설명·코드펜스 없이):

{{"keywords": [{{"keyword": "한국어 주제/키워드", "en": "영어 검색어", "note": "왜 관심을 끄는지 한 줄"}}]}}

규칙:
- 정확히 {n}개, 관심도 높은 순서대로.
- keyword 는 그대로 '시리즈 중심 테마'로 쓸 수 있게 구체적으로.
{JSON_SAFE}"""


def research_keywords(category, settings, log=print, progress=None, n=10):
    """카테고리별 관심 키워드를 인기순 N개로 조사해 리스트로 반환.
    각 항목: {keyword, en, note}"""
    progress = progress or (lambda *a, **k: None)
    cat_desc = CATEGORIES.get(category, category)
    engine = settings.get("llm", "gemma4")
    log(f"   🔎 키워드 조사 시작 ({engine}) — {category}")
    progress(15.0, f"{category} 관심 키워드 조사 중")
    if engine != "claude" and not ensure_ollama_running(settings, log):
        raise RuntimeError("Ollama 서버를 사용할 수 없습니다.")
    prompt = _keyword_prompt(category, cat_desc, settings.get("seed_keywords", []),
                             _identity(settings), n)
    last_err = None
    for attempt in range(3):
        if attempt:
            log(f"   ↻ 키워드 조사 재시도 {attempt + 1}/3...")
        try:
            d = _extract_json(_complete(settings, prompt, log, KEYWORD_SYSTEM))
            kws = d.get("keywords") or []
            kws = [k for k in kws if (k.get("keyword") or "").strip()]
            if kws:
                progress(100.0, "키워드 조사 완료")
                log(f"   ✅ 키워드 {len(kws)}개")
                return kws[:n]
            last_err = "keywords가 비어 있음"
        except Exception as e:
            last_err = str(e)
    raise RuntimeError(f"키워드 조사 결과를 해석하지 못했습니다: {last_err}")


# ── 카테고리(라벨) 정리 — 이미 발행된 글에 카테고리 라벨을 소급 적용 ───────────────────
# 배경: generate_post()가 category를 en_labels/ko_labels에 항상 넣도록 고치기 전에는
# (2026-07-06 버그 수정) 카테고리 값이 실제 Blogger 라벨에 반영되지 않아, 블로그에
# '카테고리' 위젯이 있어도 글들이 카테고리별로 묶이지 않았다. 이미 발행된 글은 로컬
# schedule.json에 라벨/카테고리를 저장해 두지 않으므로(가벼운 구조 유지), Blogger에서
# 직접 현재 라벨을 조회해 부족한 것만 골라 보정한다.
def fetch_relabel_candidates(data: dict, settings: dict, log=print, progress=None) -> list:
    """발행된 글들의 현재 Blogger 라벨 상태를 조회. 이미 이 블로그의 카테고리 중 하나를
    라벨로 갖고 있으면 has_category=True(정상), 아니면 False(보정 대상)로 표시.
    반환 항목: {date, topic, ko_id, ko_labels, ko_url, en_id, en_labels, en_url, has_category}"""
    progress = progress or (lambda *a, **k: None)
    import publish_today as pub
    from googleapiclient.discovery import build
    creds = pub.get_credentials()
    service = build("blogger", "v3", credentials=creds)
    blog_id, blog_url = pub.get_blog_id(service)
    log(f"   📝 블로그: {blog_url}")

    cats = set(blog_categories(data))
    published = [(d, e) for d, e in sorted(data["entries"].items())
                 if e.get("status") == ST_PUBLISHED and (e.get("ko_url") or e.get("en_url"))]
    total = len(published)
    out = []
    for idx, (date_str, e) in enumerate(published):
        progress(100.0 * idx / max(1, total), f"라벨 조회 중 ({idx + 1}/{total})")
        item = {"date": date_str, "topic": e.get("topic", "")}
        has_cat = False
        for lang, url_key, id_key, labels_key in (
                ("ko", "ko_url", "ko_id", "ko_labels"), ("en", "en_url", "en_id", "en_labels")):
            url = e.get(url_key)
            if not url:
                continue
            try:
                post = service.posts().getByUrl(blogId=blog_id, url=url).execute()
            except Exception as ex:
                log(f"   ⚠️ {date_str} ({lang}) 조회 실패: {ex}")
                continue
            labels = post.get("labels", []) or []
            item[id_key] = post.get("id")
            item[labels_key] = labels
            item[url_key] = url
            if any(c in labels for c in cats):
                has_cat = True
        item["has_category"] = has_cat
        if item.get("ko_id") or item.get("en_id"):
            out.append(item)
    progress(100.0, "라벨 조회 완료")
    log(f"   ✅ 발행된 글 {len(out)}개 조회 완료 "
        f"(카테고리 없음 {sum(1 for i in out if not i['has_category'])}개)")
    return out


def _category_prompt(items: list, categories: list, identity: str) -> str:
    cat_list = "\n".join(f"- {c}" for c in categories)
    lines = "\n".join(f'{i["date"]}: {i["topic"]}' for i in items)
    return f"""블로그 색깔: {identity}

이 블로그의 카테고리 목록:
{cat_list}

아래는 이미 발행된 글의 날짜와 주제입니다. 각 글을 위 카테고리 중 가장 알맞은 것 하나로
분류하세요.
{lines}

아래 JSON만 출력(설명·코드펜스 없이): {{"날짜": "카테고리", ...}}
카테고리 값은 반드시 위 목록에 있는 표기 그대로 정확히 사용하세요."""


def suggest_categories(items: list, data: dict, settings: dict, log=print, progress=None) -> dict:
    """카테고리 라벨이 없는 글들을 블로그의 카테고리 목록 중 하나로 LLM 분류.
    반환: {date: category}. items는 fetch_relabel_candidates()의 결과(전체 또는 일부)."""
    progress = progress or (lambda *a, **k: None)
    categories = blog_categories(data)
    need = [i for i in items if not i.get("has_category")]
    if not need:
        return {}
    engine = settings.get("llm", "gemma4")
    log(f"   🔎 카테고리 분류 시작 ({engine}) — {len(need)}개")
    if engine != "claude" and not ensure_ollama_running(settings, log):
        raise RuntimeError("Ollama 서버를 사용할 수 없습니다.")
    result = {}
    chunk = 25
    for i in range(0, len(need), chunk):
        batch = need[i:i + chunk]
        progress(100.0 * i / max(1, len(need)),
                  f"카테고리 분류 중 ({i + 1}~{i + len(batch)}/{len(need)})")
        prompt = _category_prompt(batch, categories, _identity(settings))
        try:
            d = _extract_json(_complete(settings, prompt, log, KEYWORD_SYSTEM))
            for it in batch:
                cat = d.get(it["date"])
                result[it["date"]] = cat if cat in categories else categories[0]
        except Exception as e:
            log(f"   ⚠️ 분류 실패(이 묶음은 기본 카테고리로 대체): {e}")
            for it in batch:
                result[it["date"]] = categories[0]
    progress(100.0, "카테고리 분류 완료")
    log(f"   ✅ 카테고리 분류 완료 — {len(result)}개")
    return result


def apply_category_labels(candidates: list, assignments: dict, log=print, progress=None):
    """candidates: fetch_relabel_candidates() 결과. assignments: {date: category}.
    각 날짜의 한/영 글에 카테고리 라벨을 추가(부분 patch — 라벨만 바꾸고 본문/제목은
    건드리지 않음). 반환: (성공한 날짜 목록, 실패한 '날짜(언어)' 목록)."""
    progress = progress or (lambda *a, **k: None)
    import publish_today as pub
    from googleapiclient.discovery import build
    creds = pub.get_credentials()
    service = build("blogger", "v3", credentials=creds)
    blog_id, _ = pub.get_blog_id(service)
    by_date = {c["date"]: c for c in candidates}
    done, failed = [], []
    total = len(assignments)
    for idx, (date_str, category) in enumerate(assignments.items()):
        c = by_date.get(date_str)
        if not c:
            continue
        progress(100.0 * idx / max(1, total), f"{date_str} 라벨 적용 중")
        ok_any = False
        for id_key, labels_key, lang in (
                ("ko_id", "ko_labels", "ko"), ("en_id", "en_labels", "en")):
            post_id = c.get(id_key)
            if not post_id:
                continue
            labels = list(c.get(labels_key) or [])
            if category not in labels:
                labels.append(category)
            try:
                service.posts().patch(blogId=blog_id, postId=post_id,
                                       body={"labels": labels}).execute()
                log(f"   ✅ {date_str} ({lang}) → '{category}' 라벨 적용")
                ok_any = True
            except Exception as e:
                log(f"   ❌ {date_str} ({lang}) 라벨 적용 실패: {e}")
                failed.append(f"{date_str}({lang})")
        if ok_any:
            done.append(date_str)
    progress(100.0, "라벨 적용 완료")
    return done, failed


# ── 무료 해외 키워드 리서치(구글 자동완성 + 의도 필터 조합, LLM·로그인 불필요) ──────────
_INTENT_MODIFIERS = {
    "기간": ["3-day", "7-day", "2-week", "weekend", "one week"],
    "동행": ["solo female", "family with kids", "couple", "backpacker"],
    "예산/스타일": ["budget", "luxury", "budget backpacker"],
    "계절": ["autumn", "winter", "cherry blossom", "spring"],
}


def expand_keywords_by_intent(seed: str) -> dict:
    """1단계(의도 필터): 기간/동행/예산/계절 필터를 시드와 조합해 롱테일 후보 생성.
    네트워크 불필요 — 순수 조합. 반환: {카테고리: [키워드, ...]}"""
    seed = (seed or "").strip()
    if not seed:
        return {}
    return {cat: [f"{mod} {seed}" for mod in mods] for cat, mods in _INTENT_MODIFIERS.items()}


def _autocomplete_suggest(query: str, timeout: int = 5) -> list:
    """구글 자동완성 공개 엔드포인트에서 제안어를 가져온다(로그인·API키 불필요)."""
    import urllib.request
    import urllib.parse
    import json as _json
    url = "https://suggestqueries.google.com/complete/search?" + urllib.parse.urlencode(
        {"client": "firefox", "q": query, "hl": "en"})
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = _json.loads(resp.read().decode("utf-8"))
    return data[1] if len(data) > 1 else []


def expand_keywords(seed: str, log=print) -> dict:
    """무료 해외 키워드 확장: ① 구글 자동완성 롱테일 후보 ② 의도 필터(기간/동행/예산/계절) 조합.
    반환: {"autocomplete": [키워드,...], "intent": {카테고리: [키워드,...]}}"""
    seed = (seed or "").strip()
    result = {"autocomplete": [], "intent": {}}
    if not seed:
        return result
    queries = [seed, f"how to {seed}", f"best {seed}", f"{seed} for", f"is {seed}"]
    seen = set()
    for q in queries:
        try:
            for s in _autocomplete_suggest(q):
                key = (s or "").strip().lower()
                if key and key not in seen:
                    seen.add(key)
                    result["autocomplete"].append(s)
        except Exception as e:
            log(f"   ⚠️ 자동완성 조회 실패({q}): {e}")
    result["intent"] = expand_keywords_by_intent(seed)
    return result


# ══════════════════════════════════════════════════════════════════════════════
#  사진 / 발행 — publisher 코드 재사용
# ══════════════════════════════════════════════════════════════════════════════

def current_blog():
    """현재 발행 대상 블로그 (id, url). 미설정이면 (None, None)."""
    if BLOG_ID_FILE.exists():
        lines = BLOG_ID_FILE.read_text(encoding="utf-8").strip().split("\n")
        if len(lines) >= 2 and lines[0].strip():
            return lines[0].strip(), lines[1].strip()
    return None, None


def current_blog_url(data: dict = None) -> str:
    """표시용 블로그 URL. blog_id.txt → 설정값 → 기본값 순."""
    _, url = current_blog()
    if url:
        return url
    if data:
        u = data["settings"].get("blog_url", "")
        if u:
            return u
    return DEFAULT_BLOG_URL


def list_blogs(log=print):
    """로그인한 구글 계정의 Blogger 블로그 목록을 [(id, name, url), ...]로 반환.
    최초 호출 시 OAuth 로그인 창이 뜰 수 있습니다(네트워크 필요)."""
    import publish_today as pub
    creds = pub.get_credentials()
    from googleapiclient.discovery import build
    service = build("blogger", "v3", credentials=creds)
    items = service.blogs().listByUser(userId="self").execute().get("items", [])
    return [(b["id"], b.get("name", ""), b.get("url", "")) for b in items]


def set_current_blog(blog_id: str, url: str, data: dict = None):
    """발행 대상 블로그를 변경(blog_id.txt 갱신 + 설정에 표시용 저장)."""
    BLOG_ID_FILE.write_text(f"{blog_id}\n{url}", encoding="utf-8")
    if data is not None:
        data["settings"]["blog_url"] = url
        data["settings"]["blog_id"] = blog_id
        save_schedule(data)


def list_photos_for_date(date_str: str):
    """해당 날짜 사진 폴더의 사진 목록 (없으면 빈 리스트)."""
    import publish_today as pub
    folder = pub.find_date_folder(post_date(date_str))
    return pub.find_photos(folder)


# 주제에서 지명을 뽑을 때 검색할 한국 도시·지역명 목록 (긴 것 → 짧은 것 순으로 배치)
_KR_LOCATIONS = [
    # 유명 세부 지역 (도시명보다 먼저 체크해야 오탐 방지)
    "장생포", "해운대", "광안리", "태종대", "자갈치", "남포동", "서면",
    "북촌", "인사동", "홍대", "이태원", "명동", "동대문", "남산",
    "설악산", "지리산", "한라산",
    # 광역시·특별시·특별자치시
    "서울", "부산", "인천", "대구", "광주", "대전", "울산", "세종",
    # 주요 시
    "수원", "성남", "고양", "용인", "창원", "청주", "전주", "천안",
    "포항", "경주", "안동", "구미", "진주", "여수", "순천", "목포",
    "군산", "익산", "제주", "서귀포", "속초", "강릉", "원주", "춘천",
    "평택", "안산", "안양", "남양주", "화성", "의정부", "파주",
    # 도·광역 지역
    "경기", "강원", "충북", "충남", "전북", "전남", "경북", "경남", "제주도",
]


def _extract_location_hint(topic: str) -> str:
    """주제 텍스트에서 첫 번째로 발견된 한국 지명을 반환. 없으면 빈 문자열."""
    for loc in _KR_LOCATIONS:
        if loc in topic:
            return loc
    return ""


# 글 하나에 넣는 최대 사진 장수 — 폴더에 사진이 아무리 많아도(예: 87장) 전부 올리면
# 업로드가 오래 걸리고 글도 사진첩처럼 무거워지므로 상한을 둔다(2026-07-08).
MAX_PHOTOS_PER_POST = 10


def resolve_photos(date_str: str, photo_dir: str = None, allow_auto: bool = False):
    """사진 목록을 결정(최대 MAX_PHOTOS_PER_POST장 — 이름순 앞에서부터).
    · photo_dir(직접 지정)가 있으면 그 폴더의 사진만 사용.
    · allow_auto=True 일 때만 날짜로 폴더를 자동 탐색(C:\\blogger\\YYYY\\MM\\…).
      기본값 False — 폴더를 직접 지정하지 않았으면 사진을 넣지 않습니다
      (옛 날짜 폴더의 무관한 사진이 시리즈 글 등에 끼어드는 문제 방지)."""
    import publish_today as pub
    if photo_dir:
        p = Path(photo_dir)
        photos = pub.find_photos(p) if p.exists() else []
    elif allow_auto:
        photos = list_photos_for_date(date_str)
    else:
        photos = []
    return photos[:MAX_PHOTOS_PER_POST]


def count_photos(date_str: str, data: dict = None, allow_auto: bool = False) -> int:
    pd = None
    if data:
        pd = (data["entries"].get(date_str) or {}).get("photo_dir")
    return len(resolve_photos(date_str, pd, allow_auto))


def set_photo_dir(data: dict, date_str: str, path: str):
    """그 날짜에 사용할 사진 폴더를 지정/해제."""
    path = (path or "").strip()
    e = data["entries"].get(date_str)
    if e is None:
        if not path:
            return
        e = data["entries"].setdefault(date_str, {"status": ST_PENDING})
    if path:
        e["photo_dir"] = path
    else:
        e.pop("photo_dir", None)


def has_photos(date_str: str) -> bool:
    return len(list_photos_for_date(date_str)) > 0


def _reflow_image_anchors(body: str, n: int) -> str:
    """기존 <!-- IMAGE_n --> 자리표시자를 모두 지우고, n장 기준으로 다시 배치:
    전체(히어로) 1장은 도입부 뒤, 나머지는 각 소주제(<h2>) 바로 뒤에 한 장씩."""
    body = re.sub(r"<!--\s*IMAGE_\d+[^>]*-->\s*", "", body or "")
    if n <= 0:
        return body
    parts = re.split(r"(<h2>.*?</h2>)", body, flags=re.S)
    out, idx = [], 1
    out.append(parts[0])                       # 도입부
    if idx <= n:                               # 히어로(전체 주제)
        out.append(f'\n<!-- IMAGE_{idx} alt="" -->\n'); idx += 1
    i = 1
    while i < len(parts):
        out.append(parts[i])                   # <h2>소제목</h2>
        if idx <= n:                           # 그 소주제 사진
            out.append(f'\n<!-- IMAGE_{idx} alt="" -->\n'); idx += 1
        if i + 1 < len(parts):
            out.append(parts[i + 1])           # 소주제 본문
        i += 2
    return "".join(out)


def _fill_anchor_captions(body: str, captions: list, lang: str) -> str:
    """_reflow_image_anchors가 alt=""로 비워 둔 자리표시자에 실제 캡션을 채운다.
    captions: gen_captions() 결과({ko,en} 리스트, 사진 순서와 일치)."""
    def replacer(m):
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(captions):
            cap = (captions[idx].get(lang) or "").replace('"', "'")
            return f'<!-- IMAGE_{idx + 1} alt="{cap}" -->'
        return m.group(0)
    return re.sub(r'<!--\s*IMAGE_(\d+)\s+alt="([^"]*?)"\s*-->', replacer, body)


def apply_photo_folder(date_str: str, photo_dir: str, data: dict, log=print,
                       settings: dict = None) -> int:
    """글 작성 '이후'에 사진 폴더를 지정해, 생성된 글에 사진 자리표시자를 다시 배치.
    (발행 시 그 폴더 사진이 업로드되어 삽입됩니다.) 사진 장수를 반환.
    settings를 주면 새 사진에 맞는 캡션(alt 텍스트)도 다시 생성해 채운다 — 안 주면
    자리표시자만 다시 배치되고 캡션은 빈 채로 남는다(2026-07-08 전 동작과 동일, 하위호환)."""
    set_photo_dir(data, date_str, photo_dir)
    save_schedule(data)
    cfg = load_generated(date_str)
    if not cfg:
        raise RuntimeError("생성된 글이 없습니다. 먼저 [✍ 지금 생성]으로 글을 만드세요.")
    photos = resolve_photos(date_str, photo_dir)
    n = len(photos)
    if n == 0:
        raise RuntimeError("지정한 폴더에 사진(jpg·png·webp)이 없습니다.")
    cfg["body_ko"] = _reflow_image_anchors(cfg.get("body_ko", ""), n)
    cfg["body_en"] = _reflow_image_anchors(cfg.get("body_en", ""), n)
    if settings is not None:
        topic = cfg.get("topic") or ""
        photo_names = [p.name for p in photos]
        log(f"   🖼  교체된 사진 {n}장 캡션 새로 작성 중...")
        captions = gen_captions(topic, photo_names, settings, log)
        cfg["body_ko"] = _fill_anchor_captions(cfg["body_ko"], captions, "ko")
        cfg["body_en"] = _fill_anchor_captions(cfg["body_en"], captions, "en")
    cfg["photo_dir"] = photo_dir
    out_dir = GENERATED_DIR / date_str
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "config.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "post_ko.html").write_text(cfg["body_ko"], encoding="utf-8")
    (out_dir / "post_en.html").write_text(cfg["body_en"], encoding="utf-8")
    log(f"   📂 폴더 사진 {n}장을 글에 배치(자리표시자) — 발행 시 업로드·삽입됩니다.")
    return n


def local_photo_preview(date_str: str, photo_dir: str):
    """발행 전 '로컬 사진'으로 배치 미리보기 HTML(ko, en)을 만듭니다(업로드 없이)."""
    import publish_today as pub
    cfg = load_generated(date_str)
    if not cfg:
        return None
    photos = resolve_photos(date_str, photo_dir, allow_auto=True)
    photo_uris = [(p, Path(p).as_uri()) for p in photos]
    ko = pub.inject_photos(cfg.get("body_ko", ""), photo_uris, lang="ko")
    en = pub.inject_photos(cfg.get("body_en", ""), photo_uris, lang="en")
    return {"ko_title": cfg.get("ko_title", ""), "en_title": cfg.get("en_title", ""),
            "body_ko": ko, "body_en": en, "n": len(photos)}


def published_posts(data: dict) -> list:
    """이 블로그에서 '발행 완료'된 글 목록(최근 날짜 순).
    각 항목: {key, date, time, title, ko_url, en_url, published_at}"""
    out = []
    for key, e in data["entries"].items():
        if e.get("status") != ST_PUBLISHED:
            continue
        if not (e.get("ko_url") or e.get("en_url")):
            continue
        cfg = load_generated(key)
        title = ((cfg.get("ko_title") if cfg else "") or e.get("topic", "") or "(제목 없음)")
        out.append({
            "key": key, "date": post_date(key), "time": e.get("time", ""),
            "title": title, "ko_url": e.get("ko_url", ""), "en_url": e.get("en_url", ""),
            "published_at": e.get("published_at", ""),
        })
    out.sort(key=lambda r: (r["date"], r["time"]), reverse=True)
    return out


def sync_published_status(data: dict, log=print, progress=None) -> dict:
    """블로그에서 사용자가 직접 삭제한 글을 감지해 캘린더 상태를 되돌린다.
    '발행 완료'로 표시된 각 글의 en_url/ko_url이 실제로 블로그에 살아있는지
    getByPath로 확인 — 404(삭제됨)인 URL만 비우고, 둘 다 없어지면 상태를
    '생성 완료'(재발행 가능)로 되돌린다. 네트워크 오류 등은 안전하게 건너뜀.
    반환: {"checked": n, "reverted": [date_str,...], "partial": [date_str,...]}"""
    import publish_today as pub
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from urllib.parse import urlsplit

    creds = pub.get_credentials()
    service = build("blogger", "v3", credentials=creds)
    bid, _ = pub.get_blog_id(service)

    entries = data.get("entries", {})
    dates = [d for d, e in entries.items() if e.get("status") == ST_PUBLISHED]
    reverted, partial = [], []
    total = len(dates) or 1
    for i, date_str in enumerate(dates):
        if progress:
            progress(i / total * 100, f"확인 중: {date_str}")
        e = entries[date_str]
        changed = False
        for url_key in ("en_url", "ko_url"):
            url = (e.get(url_key) or "").strip()
            if not url:
                continue
            try:
                path = urlsplit(url).path
                service.posts().getByPath(blogId=bid, path=path).execute()
            except HttpError as he:
                if getattr(he.resp, "status", None) == 404:
                    log(f"   🗑 {date_str} {url_key} — 블로그에서 삭제됨 확인")
                    e[url_key] = ""
                    changed = True
                else:
                    log(f"   ⚠️ {date_str} {url_key} 확인 실패({he.resp.status}) — 건너뜀")
            except Exception as ex:
                log(f"   ⚠️ {date_str} {url_key} 확인 중 오류 — 건너뜀: {ex}")
        if changed:
            if not (e.get("en_url") or "").strip() and not (e.get("ko_url") or "").strip():
                e["status"] = ST_GENERATED
                e.pop("published_at", None)
                reverted.append(date_str)
            else:
                partial.append(date_str)
    if reverted or partial:
        save_schedule(data)
    if progress:
        progress(100.0, "동기화 완료")
    return {"checked": len(dates), "reverted": reverted, "partial": partial}


def past_titles(data: dict, n: int = 40) -> list:
    """발행 완료(published) 또는 생성 완료(generated) 글의 한/영 제목을 최근 순으로 수집.
    중복 방지 프롬프트에 전달하기 위해 최대 n편까지만 반환."""
    rows = []
    for key, e in data["entries"].items():
        if e.get("status") not in (ST_PUBLISHED, ST_GENERATED):
            continue
        cfg = load_generated(key)
        ko = (cfg.get("ko_title") if cfg else "") or ""
        en = (cfg.get("en_title") if cfg else "") or ""
        topic = e.get("topic", "")
        if ko or en or topic:
            rows.append({
                "date": post_date(key),
                "ko": ko.strip(),
                "en": en.strip(),
                "topic": topic.strip(),
                "en_url": (e.get("en_url") or "").strip(),
                "ko_url": (e.get("ko_url") or "").strip(),
            })
    rows.sort(key=lambda r: r["date"], reverse=True)
    return rows[:n]


def _series_sibling_entry(data, sid, index):
    """같은 시리즈(sid)의 특정 회차(index) entry를 (date_key, entry)로 반환(없으면 (None, None))."""
    if not sid or not data or not index or index < 1:
        return None, None
    for key, e in (data.get("entries") or {}).items():
        s = e.get("series") or {}
        if s.get("sid") == sid and s.get("index") == index:
            return key, e
    return None, None


def _lang_link_header(other_url: str, lang: str) -> str:
    """발행 시 붙는 언어 전환 링크 헤더(업데이트 시 동일하게 재구성)."""
    if lang == "en":
        return (f'<p style="text-align:right;font-size:14px;">'
                f'🇰🇷 <a href="{other_url}">한국어로 읽기</a></p>\n')
    return (f'<p style="text-align:right;font-size:14px;">'
            f'🇺🇸 <a href="{other_url}">Read in English</a></p>\n')


def add_photos_to_published(date_str: str, photo_dir: str, data: dict, settings: dict,
                            log=print) -> int:
    """이미 '발행된' 글에 사진을 추가 — 삭제·재발행 없이 그 글(같은 URL)을 업데이트.
    폴더의 사진을 업로드해 전체(히어로)+소주제별로 배치하고, 한/영 두 글을 patch 합니다.
    반환: 업데이트된 글 수."""
    import publish_today as pub
    from googleapiclient.discovery import build

    entry = data["entries"].get(date_str) or {}
    en_url, ko_url = entry.get("en_url", ""), entry.get("ko_url", "")
    if not (en_url or ko_url):
        raise RuntimeError("아직 발행되지 않은 글입니다. 발행 후에 사진을 추가할 수 있습니다.")

    # 1) 폴더 사진을 자리표시자로 배치하고 cfg 갱신(여기서 사진 0장이면 예외).
    #    settings를 넘겨 새 사진에 맞는 캡션도 함께 다시 작성(2026-07-08).
    apply_photo_folder(date_str, photo_dir, data, log, settings=settings)
    cfg = load_generated(date_str)

    # 2) 인증 + 사진 업로드
    creds = pub.get_credentials()
    service = build("blogger", "v3", credentials=creds)
    blog_id, _ = pub.get_blog_id(service)
    photos = resolve_photos(date_str, photo_dir, allow_auto=True)
    log(f"   🖼  사진 {len(photos)}장 업로드 중...")
    photo_uris = pub.preload_photos(photos)
    if not photo_uris:
        raise RuntimeError("사진 업로드에 실패했습니다.")

    # 3) 본문에 사진 주입 + 언어 링크 헤더 재구성
    en_body = pub.inject_photos(cfg["body_en"], photo_uris, lang="en")
    ko_body = pub.inject_photos(cfg["body_ko"], photo_uris, lang="ko")
    if en_url and ko_url:
        en_body = _lang_link_header(ko_url, "en") + en_body
        ko_body = _lang_link_header(en_url, "ko") + ko_body

    # 4) 발행된 글(같은 URL) 내용만 교체(patch) — 발행일·제목·라벨은 그대로
    updated = 0
    for url, body in [(en_url, en_body), (ko_url, ko_body)]:
        if not url:
            continue
        pid = pub.post_id_by_url(service, blog_id, url)
        if pid:
            pub.patch_post_content(service, blog_id, pid, body)
            updated += 1
            log(f"   ✅ 업데이트: {url}")
    log(f"   🖼 발행된 글에 사진 {len(photo_uris)}장 추가 완료 — {updated}개 글")
    return updated


def find_series_photo_mismatches(sid: str, data: dict) -> list:
    """시리즈(sid)의 각 편에 현재 배정된 사진 폴더가, 그 편의 제목·키워드와 실제로 잘
    맞는지 다시 계산해서 비교(콘텐츠 기반 매칭 — 2026-07-08 버그 수정: 예전엔 편 순서·
    폴더 이름 가나다순으로 그냥 1:1 배정해서 '주전 몽돌해변' 편에 '대왕암' 폴더가 배정되는
    식의 불일치가 흔했음). 아무것도 바꾸지 않고 미리보기 목록만 반환 — 실제 교정은
    apply_series_photo_fixes()로.
    반환: [{"date", "title", "old_dir", "new_dir", "score", "published", "mismatch"}]"""
    series = (data.get("series") or {}).get(sid)
    if not series:
        raise ValueError(f"시리즈를 찾을 수 없습니다: {sid}")
    posts = series.get("posts") or []
    entries = [(k, v) for k, v in data["entries"].items()
               if (v.get("series") or {}).get("sid") == sid]
    entries.sort(key=lambda kv: kv[1]["series"]["index"])

    dirs = [v.get("photo_dir") for _, v in entries if v.get("photo_dir")]
    if not dirs:
        raise ValueError("이 시리즈 편들에 사진 폴더가 지정돼 있지 않습니다.")
    parent = str(Path(dirs[0]).parent)
    all_folders = _series_photo_folders(parent)
    if len(all_folders) <= 1:
        raise ValueError(f"'{parent}' 아래에 편별 하위 폴더가 없어 다시 매칭할 수 없습니다.")

    remaining = list(all_folders)
    results = []
    for (ds, entry), post in zip(entries, posts):
        text = f"{post.get('title_ko','')} {post.get('title_en','')} {post.get('keyword','')}"
        best, score = _best_matching_folder(text, remaining)
        old_dir = entry.get("photo_dir", "")
        if best:
            remaining.remove(best)
        results.append({
            "date": ds, "title": entry.get("topic", ""),
            "old_dir": old_dir, "new_dir": best or old_dir, "score": score,
            "published": entry.get("status") == ST_PUBLISHED,
            "mismatch": bool(best) and best != old_dir,
        })
    return results


def apply_series_photo_fixes(fixes: list, data: dict, settings: dict, log=print,
                             progress=None) -> tuple:
    """find_series_photo_mismatches()가 찾은 교정 목록 중 실제로 적용할 항목(fixes,
    mismatch=True인 것만 골라서 넘기면 됨)을 반영. 이미 발행된 편은 사진만 다시 올려
    라이브 글을 patch(add_photos_to_published, 삭제·재발행 없음), 아직 미발행이면
    photo_dir만 바꿔 둠(다음 생성 때 반영). 반환: (성공한 날짜 목록, 실패 목록[(날짜,사유)])."""
    progress = progress or (lambda *a, **k: None)
    done, failed = [], []
    total = len(fixes)
    for idx, r in enumerate(fixes):
        ds = r["date"]
        progress(100.0 * idx / max(1, total), f"{ds} 사진 교정 중")
        try:
            if r["published"]:
                add_photos_to_published(ds, r["new_dir"], data, settings, log=log)
            else:
                set_photo_dir(data, ds, r["new_dir"])
                save_schedule(data)
            log(f"   ✅ {ds} 교정 완료: {r['title'][:30]} → {Path(r['new_dir']).name}")
            done.append(ds)
        except Exception as e:
            log(f"   ❌ {ds} 교정 실패: {e}")
            failed.append((ds, str(e)))
    progress(100.0, "사진 교정 완료")
    return done, failed


def publish_date(date_str: str, settings: dict, data: dict, log=print, progress=None,
                 stop_check=None) -> dict:
    """
    특정 날짜의 글을 발행합니다.
      1) 생성물이 없으면 LLM으로 생성
      2) 사진 업로드(Playwright) → 본문에 삽입
      3) 한/영 쌍 발행 + 언어 링크
      4) 사진 폴더 완료 표시
      5) schedule 항목 상태 갱신
    반환: {"en_url":..., "ko_url":...}
    progress(pct, msg): 글 생성은 0~80%, 인증·업로드·발행은 80~100% 구간에 매핑.
    stop_check: 호출 시 True를 반환하면 중단(글 생성 중 + 사진 업로드 전까지만 유효 —
    기존 글 삭제·재발행 단계에 들어가면 콘텐츠 유실 위험이 있어 그 뒤로는 끝까지 진행함).
    """
    import publish_today as pub
    progress = progress or (lambda *a, **k: None)
    stop_check = stop_check or (lambda: False)

    # 개별 날짜 항목이 우선, 없으면 주간 템플릿을 사용
    plan = planned(data, date_str)
    topic = plan["topic"]
    if not topic:
        raise RuntimeError(f"{date_str} 에 지정된 주제가 없습니다(개별·주간 템플릿 모두 없음).")
    # 작성 방향 + 첨부 참고문서(.md) 내용을 합쳐 참고 텍스트로
    refs = combine_refs(plan["refs"], plan.get("md_file"))
    if plan.get("md_file"):
        log(f"   📄 참고 문서 반영: {Path(plan['md_file']).name}")

    # 주간 템플릿에서 온 날짜라도 발행 시 개별 항목으로 굳혀서 이력을 남깁니다.
    # (refs는 md 미포함 원본을 저장 — 다음 planned()에서 md가 이중 결합되지 않게)
    entry = data["entries"].setdefault(date_str, {})
    entry["topic"] = topic
    if plan["refs"]:
        entry["refs"] = plan["refs"]
    if plan.get("md_file"):
        entry["md_file"] = plan["md_file"]
    series_ctx = entry.get("series") or None
    photo_dir = entry.get("photo_dir") or None

    # 1) 글 확보 (주제·참고·시리즈·사진폴더가 캐시와 다르면 재생성) — 진행률 0~80% 매핑
    cfg = load_generated(date_str)
    stale = (not cfg or cfg.get("topic") != topic or cfg.get("refs", "") != (refs or "")
             or (cfg.get("series") or {}) != (series_ctx or {})
             or cfg.get("photo_dir", "") != (photo_dir or ""))
    if stale:
        cfg = generate_post(date_str, topic, settings, log, refs=refs,
                            progress=lambda p, m: progress(p * 0.80, m),
                            series_ctx=series_ctx, photo_dir=photo_dir, data=data,
                            stop_check=stop_check)
        entry["status"] = ST_GENERATED
        save_schedule(data)
    else:
        log("   📄 기존 생성물 사용")
        progress(80.0, "기존 생성 글 사용")

    if stop_check():
        log("   ■ 사용자 요청으로 중단했습니다(사진 업로드·발행 전이라 안전하게 멈춥니다).")
        raise StopRequested("사용자가 중단했습니다.")

    # 2) 인증
    progress(82.0, "구글 인증 확인 중")
    log("   🔐 Google 인증 확인...")
    creds = pub.get_credentials()
    from googleapiclient.discovery import build
    service = build("blogger", "v3", credentials=creds)
    blog_id, blog_url = pub.get_blog_id(service)
    log(f"   📝 블로그: {blog_url}")

    # 3) 사진 업로드
    #    단어 카드(한/영에 텍스트가 각각 박힌 이미지)가 있으면 언어별로 따로 업로드+삽입
    #    (텍스트가 그림 픽셀에 있어 공유 불가). 그 외(실사진)는 한 번만 업로드해 한·영이 URL 공유.
    photos = resolve_photos(date_str, photo_dir, settings.get("auto_date_photos", False))
    # 사진 기반 글: 생성 단계에서 사진을 '그룹 순서'로 나열해 IMAGE_1..M 번호를 매겼으므로,
    # 발행도 반드시 그 순서(photo_order)로 올려야 자리표시자와 사진이 일치한다.
    if cfg.get("photo_order"):
        ordered = [Path(p) for p in cfg["photo_order"] if Path(p).exists()]
        if ordered:
            photos = ordered
    lp_ko = cfg.get("library_photos_ko") or []
    lp_en = cfg.get("library_photos_en") or []
    if not photos and (lp_ko or lp_en):
        photos_ko = [Path(p) for p in lp_ko if Path(p).exists()]
        photos_en = [Path(p) for p in lp_en if Path(p).exists()]
        progress(85.0, f"단어 카드 {len(photos_ko) + len(photos_en)}장 업로드 중")
        log(f"   🎴 단어 카드(한/영 별도) 업로드 — 한글 {len(photos_ko)}장 / 영문 {len(photos_en)}장")
        uris_ko = pub.preload_photos(photos_ko) if photos_ko else []
        uris_en = pub.preload_photos(photos_en) if photos_en else []
        ko_body = pub.inject_photos(cfg["body_ko"], uris_ko, lang="ko")
        en_body = pub.inject_photos(cfg["body_en"], uris_en, lang="en")
        photo_uris = uris_ko or uris_en   # 아래 JSON-LD 대표이미지(og:image류)용
    else:
        if not photos and cfg.get("library_photos"):
            photos = [Path(p) for p in cfg["library_photos"] if Path(p).exists()]
            if photos:
                log(f"   📚 라이브러리 사진 {len(photos)}장 사용(폴백)")
        if photos:
            progress(85.0, f"사진 {len(photos)}장 업로드 중")
            log(f"   🖼  사진 {len(photos)}장 — CDN 업로드 시작")
            photo_uris = pub.preload_photos(photos)
        else:
            progress(88.0, "본문 준비 중 (사진 없음)")
            log("   🖼  사진 없음 — 텍스트만 발행")
            photo_uris = []
        en_body = pub.inject_photos(cfg["body_en"], photo_uris, lang="en")
        ko_body = pub.inject_photos(cfg["body_ko"], photo_uris, lang="ko")

    # 4) 발행 — 발행 표시 날짜·시각을 '예약된 날짜/시각'으로(오늘로 찍히지 않게)
    progress(92.0, "블로그에 발행 중")
    log("   🚀 발행 시작...")
    try:
        sched_dt = planned_datetime(data, date_str)
    except Exception:
        sched_dt = None

    # JSON-LD(BlogPosting) 구조화 데이터 — 사진 URL·발행일이 확정된 지금 본문에 추가
    # (Blogger가 본문 <script>를 보존해야 함 — settings.seo_schema=False 면 생략)
    if settings.get("seo_schema", True):
        author = (settings.get("author_name") or "").strip()
        img0 = photo_uris[0][1] if photo_uris else ""
        date_iso = sched_dt.isoformat() if sched_dt else date_str
        try:
            _, b_url = current_blog()
        except Exception:
            b_url = ""
        en_body += _article_jsonld(cfg.get("en_title"), cfg.get("en_meta"),
                                   date_iso, img0, author, "en", b_url)
        ko_body += _article_jsonld(cfg.get("ko_title"), cfg.get("ko_meta"),
                                   date_iso, img0, author, "ko", b_url)

    if stop_check():
        log("   ■ 사용자 요청으로 중단했습니다(사진은 이미 올라갔지만 글은 아직 안 건드림).")
        raise StopRequested("사용자가 중단했습니다.")

    # 재발행 방지: 이 엔트리가 이미 발행된 적이 있으면(en_url/ko_url 보유) 옛 글을 먼저 삭제.
    # 그래야 같은 슬러그가 회수돼 깨끗한 URL로 새로 올라가고, 중복 글(_2 접미사)이 생기지 않음.
    # (이 지점 이후로는 삭제→재발행이 원자적 단위라 stop_check를 넣지 않음 — 중간에 멈추면
    #  옛 글은 지워졌는데 새 글은 안 올라간 상태가 될 위험이 있음)
    if (entry.get("en_url") or "").strip() or (entry.get("ko_url") or "").strip():
        log("   🗑 재발행 — 기존에 발행된 같은 글을 먼저 삭제(중복·_2 URL 방지)")
        try:
            delete_blog_posts(entry, log=log)
            time.sleep(2)   # 슬러그 회수 안정화 대기
        except Exception as e:
            log(f"   ⚠️ 기존 글 삭제 실패(계속 진행): {e}")

    en_url, ko_url = pub.publish_pair(service, blog_id, cfg, en_body, ko_body, base_dt=sched_dt)
    pub.mark_done(date_str)

    # 5) 상태 갱신
    entry["status"] = ST_PUBLISHED
    entry["en_url"] = en_url
    entry["ko_url"] = ko_url
    entry["published_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_schedule(data)

    # 시리즈 양방향 연결 완성: 이전 편 라이브 글에 '다음 편 ▶' 링크 역주입(실패해도 발행엔 무영향)
    if series_ctx:
        try:
            _inject_next_into_prev(service, blog_id, data, series_ctx, cfg,
                                   en_url, ko_url, log)
        except Exception as e:
            log(f"   ⚠️ 다음 편 링크 역주입 생략: {e}")

    progress(100.0, "발행 완료")
    log(f"   ✅ 발행 완료\n      🇺🇸 {en_url}\n      🇰🇷 {ko_url}")
    return {"en_url": en_url, "ko_url": ko_url}


def _static_pages_content(blog_name: str, email: str, blog_url: str) -> list:
    """애드센스 필수 정적 페이지(개인정보처리방침·소개·문의) HTML을 한/영 병기로 생성.
    반환: [(title, html), ...]."""
    name = blog_name or "This blog"
    mail = email or "(이메일 주소를 설정하세요)"
    ga = ('<h3>Google AdSense &amp; Cookies</h3>'
          '<p>This site may use third-party advertising companies, including '
          '<strong>Google AdSense</strong>, to serve ads. Google, as a third-party vendor, '
          'uses cookies (including the DoubleClick DART cookie) to serve ads based on your '
          'prior visits to this and other websites. You may opt out of personalized advertising '
          'by visiting <a href="https://www.google.com/settings/ads" target="_blank" '
          'rel="noopener">Google Ads Settings</a> or '
          '<a href="https://www.aboutads.info" target="_blank" rel="noopener">www.aboutads.info</a>. '
          'See Google\'s <a href="https://policies.google.com/technologies/partner-sites" '
          'target="_blank" rel="noopener">policies</a> for how data is used.</p>'
          '<p>구글 애드센스 등 제3자 광고 업체가 쿠키(DoubleClick DART 쿠키 포함)를 사용해, '
          '귀하의 이 사이트 및 다른 사이트 방문 기록을 바탕으로 광고를 게재할 수 있습니다. '
          '<a href="https://www.google.com/settings/ads" target="_blank" rel="noopener">구글 광고 설정</a>'
          '에서 맞춤 광고를 해제할 수 있습니다.</p>')

    privacy = (
        f'<p>Your privacy is important to us. This page explains what information '
        f'<strong>{name}</strong> collects and how it is used.</p>'
        '<h3>Information We Collect</h3>'
        '<p>Like most websites, this site automatically receives standard log data '
        '(IP address, browser type, pages visited) and may use cookies to improve your '
        'experience. We do not require you to submit personal information to read our content.</p>'
        + ga +
        '<h3>Analytics</h3>'
        '<p>We may use analytics services (such as Google Analytics) to understand how '
        'visitors use the site. These services may set cookies.</p>'
        '<h3>Your Consent</h3>'
        '<p>By using this website, you consent to this privacy policy. Policies may be updated; '
        'changes will be posted on this page.</p>'
        f'<p>Questions? Contact us at <a href="mailto:{mail}">{mail}</a>.</p>'
        '<hr>'
        f'<p><strong>[한국어]</strong> {name}는 방문자의 개인정보를 소중히 다룹니다. '
        '본 사이트는 콘텐츠 열람을 위해 개인정보 제출을 요구하지 않으며, 표준 접속 기록(IP·브라우저·방문 페이지)과 '
        '경험 개선용 쿠키를 사용할 수 있습니다. 위 \'Google AdSense &amp; Cookies\' 항목과 같이 제3자 광고 쿠키가 '
        f'사용될 수 있습니다. 문의: <a href="mailto:{mail}">{mail}</a></p>')

    about = (
        f'<p><strong>{name}</strong> is a bilingual (English &amp; Korean) blog dedicated to '
        'Korean arts, traditional and contemporary culture, performance, and travel. '
        'We pair every story in both languages so readers worldwide can explore Korea in depth.</p>'
        '<p>Our goal is to share original, carefully researched writing on Korean dance, '
        'painting, stage craft, heritage, and the landscapes of Korea — content you will not '
        'find elsewhere.</p>'
        '<hr>'
        f'<p><strong>[한국어]</strong> {name}는 한국의 예술·전통/현대 문화·공연·여행을 '
        '영어와 한국어로 함께 다루는 블로그입니다. 직접 조사하고 깊이 있게 쓴 독창적인 글로, '
        f'전 세계 독자에게 한국을 소개합니다. 문의: <a href="mailto:{mail}">{mail}</a></p>')

    contact = (
        '<p>We would love to hear from you — questions, feedback, or collaboration.</p>'
        f'<p><strong>Email:</strong> <a href="mailto:{mail}">{mail}</a></p>'
        f'<p><strong>Website:</strong> <a href="{blog_url}">{blog_url}</a></p>'
        '<p>We usually reply within a few days.</p>'
        '<hr>'
        f'<p><strong>[한국어]</strong> 문의·피드백·제휴는 이메일 <a href="mailto:{mail}">{mail}</a>로 '
        '연락 주세요. 보통 며칠 내에 답변드립니다.</p>')

    return [
        ("Privacy Policy (개인정보처리방침)", privacy),
        ("About (소개)", about),
        ("Contact (문의)", contact),
    ]


def publish_static_pages(settings: dict, log=print) -> dict:
    """애드센스 필수 정적 페이지(개인정보처리방침·소개·문의)를 현재 활성 블로그에
    Blogger '페이지'로 생성. 같은 제목이 이미 있으면 건너뜀. 반환: {title: url}."""
    import publish_today as pub
    from googleapiclient.discovery import build
    creds = pub.get_credentials()
    service = build("blogger", "v3", credentials=creds)
    blog_id, blog_url = pub.get_blog_id(service)
    blog_name = blog_url
    try:
        blog_name = service.blogs().get(blogId=blog_id, fields="name").execute().get("name", blog_url)
    except Exception:
        pass
    log(f"   📄 필수 페이지 대상: {blog_name} ({blog_url})")

    existing = {}
    try:
        items = service.pages().list(blogId=blog_id, fields="items(title,url)").execute().get("items", [])
        for p in items:
            existing[(p.get("title") or "").strip()] = p.get("url", "")
    except Exception as e:
        log(f"   ⚠️ 기존 페이지 조회 실패(계속): {e}")

    email = (settings.get("contact_email") or "").strip()
    out = {}
    for title, html in _static_pages_content(blog_name, email, blog_url):
        if title in existing:
            out[title] = existing[title]
            log(f"   · 이미 있음 — 건너뜀: {title}")
            continue
        try:
            res = service.pages().insert(
                blogId=blog_id, body={"title": title, "content": html}, isDraft=False).execute()
            out[title] = res.get("url", "")
            log(f"   ✅ 생성: {title} → {res.get('url','')}")
        except Exception as e:
            log(f"   ❌ 실패({title}): {e}")
    return out


def delete_blog_posts(entry: dict, log=print) -> dict:
    """발행된 글(entry)의 en_url / ko_url 포스트를 Blogger에서 삭제합니다.
    반환: {"en": "ok"|"fail"|"skip", "ko": "ok"|"fail"|"skip"}
    skip = URL 없음, fail = API 오류."""
    import publish_today as pub
    from googleapiclient.discovery import build
    from urllib.parse import urlsplit

    creds = pub.get_credentials()
    service = build("blogger", "v3", credentials=creds)
    bid, burl = pub.get_blog_id(service)
    log(f"   🎯 삭제 대상 블로그: {burl}")

    result = {}
    for lang, url_key in (("en", "en_url"), ("ko", "ko_url")):
        url = (entry.get(url_key) or "").strip()
        flag = "🇺🇸" if lang == "en" else "🇰🇷"
        if not url:
            log(f"   {flag} URL 없음 — 건너뜀")
            result[lang] = "skip"
            continue
        try:
            path = urlsplit(url).path
            post = service.posts().getByPath(blogId=bid, path=path).execute()
            post_id = post["id"]
            service.posts().delete(blogId=bid, postId=post_id).execute()
            log(f"   {flag} 블로그 삭제 완료: {url}")
            result[lang] = "ok"
        except Exception as e:
            log(f"   {flag} 삭제 실패: {e}")
            result[lang] = "fail"
    return result


def publish_curation(blog_id: str, cfg: dict, log=print) -> dict:
    """큐레이션 cfg를 지정 블로그(blog_id, 예: k-culture-now)에 발행. 반환 {en_url, ko_url}.
    사진 없이 본문만 발행하며, 호출 후 활성 블로그를 원래대로 복구합니다."""
    import publish_today as pub
    prev = load_registry().get("active")
    set_active_blog(blog_id, persist=False)
    try:
        creds = pub.get_credentials()
        from googleapiclient.discovery import build
        service = build("blogger", "v3", credentials=creds)
        bid, burl = pub.get_blog_id(service)
        log(f"   📝 큐레이션 발행 대상: {burl}")
        en_body = pub.inject_photos(cfg["body_en"], [], lang="en")
        ko_body = pub.inject_photos(cfg["body_ko"], [], lang="ko")
        en_url, ko_url = pub.publish_pair(service, bid, cfg, en_body, ko_body)
        log(f"   ✅ 큐레이션 발행 완료\n      🇺🇸 {en_url}\n      🇰🇷 {ko_url}")
        return {"en_url": en_url, "ko_url": ko_url}
    finally:
        if prev:
            set_active_blog(prev, persist=False)


# ══════════════════════════════════════════════════════════════════════════════
#  스케줄러 — 다음 발행 시각 / 발행 대상 판정
# ══════════════════════════════════════════════════════════════════════════════

def _parse_time(hhmm: str):
    try:
        h, m = hhmm.split(":")
        return int(h), int(m)
    except Exception:
        return 9, 0


def next_publish_datetime(data: dict, now: datetime = None, horizon_days: int = 120):
    """가장 이른 '다음 발행' 일시를 반환. 개별 날짜 항목 + 주간 템플릿을 모두 고려.
    과거에 밀린 개별 항목이 있으면 그게 가장 이른 값으로 잡혀 '지금 발행 대상'이 됩니다.
    반환: (datetime, date_str) 또는 None
    """
    now = now or datetime.now()
    cand = []

    # 1) 개별 날짜 항목 (미발행, 주제 있음) — 과거 catch-up 포함. 키는 'date' 또는 'date#N'
    for key, e in data["entries"].items():
        if e.get("status") == ST_PUBLISHED or not e.get("topic"):
            continue
        try:
            cand.append((planned_datetime(data, key), key))
        except Exception:
            continue

    # 2) 주간 템플릿 — 오늘부터 horizon_days 까지 (과거는 제외해 밀린 발행 폭주 방지)
    base = now.date()
    for i in range(0, horizon_days + 1):
        ds = (base + timedelta(days=i)).isoformat()
        if has_explicit_post(data, ds):
            continue  # 이 날짜에 개별 글이 있으면 1)에서 처리 — 주간 템플릿은 건너뜀
        if weekly_for_date(data, ds):
            cand.append((planned_datetime(data, ds), ds))

    if not cand:
        return None
    cand.sort()
    return cand[0]


def due_dates(data: dict, now: datetime = None):
    """지금 자동 발행해야 하는 날짜 목록.
    - 개별 날짜 항목: 시각이 지난 미발행이면 과거 것도 포함(의도적 예약이므로 catch-up)
    - 주간 템플릿: 오늘 것만 포함(과거 요일 폭주 방지)
    """
    now = now or datetime.now()
    out = set()

    for key, e in data["entries"].items():
        if e.get("status") == ST_PUBLISHED or not e.get("topic"):
            continue
        try:
            if planned_datetime(data, key) <= now:
                out.add(key)
        except Exception:
            continue

    today = now.date().isoformat()
    if weekly_for_date(data, today) and not has_explicit_post(data, today):
        if planned_datetime(data, today) <= now:
            out.add(today)

    return sorted(out)


# ── 단독 실행 (콘솔 점검용) ────────────────────────────────────────────────────
if __name__ == "__main__":
    data = load_schedule()
    print("설정:", json.dumps(data["settings"], ensure_ascii=False, indent=2))
    print("항목 수:", len(data["entries"]))
    nxt = next_publish_datetime(data)
    print("다음 발행:", nxt)
