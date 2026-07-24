# -*- coding: utf-8 -*-
"""
블로그 스튜디오 — GUI
=====================
- 발행 캘린더(월별 보기): 날짜별 주제 입력 + 상태 색상
- 다음 발행 시각 표시 + 카운트다운
- 매일 고정 시각 자동 발행 (프로그램이 켜져 있을 때)
- [지금 생성] / [지금 발행] / [미리보기] 버튼
- LLM 선택: 로컬 gemma4(Ollama) 또는 Claude(API 키)

핵심 로직은 blog_core.py 를 import 해서 재사용합니다(중복 구현 금지).
"""

import sys
import os
import string
import time
import queue
import threading
import calendar
import io
import re
import urllib.request
import webbrowser
import concurrent.futures
from pathlib import Path
from datetime import date, datetime, timedelta

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext, filedialog, simpledialog

import blog_core as core
import image_finder as imgf
import events_db as evdb
import collector as evcol
import curator as evcur
import trigger as evtrig
import photo_plan as pplan
import sheets_export
import image_gen as imgen
import photo_library as photolib
import photo_vision
import photo_xmp
import keyword_pool as kwpool
import photo_wishlist as wishlist
import photo_intake as intake
import stock_upload as stock

try:                      # 썸네일 미리보기용(없어도 동작 — 텍스트+브라우저로 대체)
    from PIL import Image, ImageTk
    HAVE_PIL = True
except Exception:
    HAVE_PIL = False

# ── 상태 색상 (모던 다크 톤) ──────────────────────────────────────────────────
# 상태 = ◉ 색 원형 배지 + 텍스트. 라이트 모드도 같은 톤 유지(약간 톤다운).
STATUS_COLOR = {
    core.ST_PUBLISHED: "#4caf50",   # 초록 ✓ 발행 완료
    core.ST_GENERATED: "#4499ff",   # 파랑 ▶ 글 생성됨
    core.ST_PENDING:   "#ffd54f",   # 노랑 ⋯ 주제만 있음(대기)
    core.ST_ERROR:     "#ef5350",   # 빨강 ! 오류
}
# 캘린더 셀 배경(흐릿한 톤) — 다크에선 색의 어두운 톤, 라이트에선 파스텔.
COLORS = {
    core.ST_PUBLISHED: "#c8e6c9",   # (라이트 모드 셀 배경)
    core.ST_GENERATED: "#bbdefb",
    core.ST_PENDING:   "#ffe0b2",
    core.ST_ERROR:     "#ffcdd2",
    "none":            "#fafafa",
}
COLORS_DARK = {                     # 다크 모드 셀 배경(상태색의 어두운 톤)
    core.ST_PUBLISHED: "#1f3a22",
    core.ST_GENERATED: "#1f2a3d",
    core.ST_PENDING:   "#3d3520",
    core.ST_ERROR:     "#3d1f1f",
    "none":            "#18181b",
}
STATUS_KO = {
    core.ST_PUBLISHED: "발행 완료",
    core.ST_GENERATED: "글 생성됨 (발행 전)",
    core.ST_PENDING:   "대기 (주제만 있음)",
    core.ST_ERROR:     "오류",
}
STATUS_GLYPH = {core.ST_PUBLISHED: "✓", core.ST_GENERATED: "▶",
                core.ST_PENDING: "⋯", core.ST_ERROR: "!"}
WEEK_HEADERS = ["일", "월", "화", "수", "목", "금", "토"]

# ── 테마 (모던 다크 모드 우선) ────────────────────────────────────────────────
# 다크 팔레트:
#   bg(메인 #0e0e10) → panel(카드 1단 #18181b) → panel2(카드 2단 #1f1f23)
#   field(입력 #1f1f23) · border(#2a2a2e) · text(#eeeeee) · sub(#999)
# 라이트는 같은 구조의 밝은 톤(눈 부시지 않은 #f5f5f7 베이스).
NEUTRAL_BG = {"systembuttonface", "systemwindow", "", "white",
              "#f0f0f0", "#ffffff", "#fff", "#fafafa", "#f5f5f5", "#eeeeee",
              "#23272e", "#2b2f36", "#1b1e24", "#3a3f48"}     # 옛 테마 색도 중립으로
THEMES = {
    "light": {"bg": "#f5f5f7", "panel": "#ffffff", "panel2": "#fafafa",
              "field": "#ffffff", "border": "#e3e3e6",
              "text": "#1a1a1a", "sub": "#666666",
              "btn": "#eeeeef", "btn_text": "#1a1a1a",
              "sel": "#dbe9ff", "accent": "#4499ff"},
    "dark":  {"bg": "#0e0e10", "panel": "#18181b", "panel2": "#1f1f23",
              "field": "#1f1f23", "border": "#2a2a2e",
              "text": "#eeeeee", "sub": "#999999",
              "btn": "#2a2a2e", "btn_text": "#eeeeee",
              "sel": "#2d3748", "accent": "#4499ff"},
}
_DEFAULT_FG = {"black", "#000000", "#000", "#222", "#222222", "#333", "#333333",
               "#444", "#444444", "#1a1a1a", "systemwindowtext", "systembuttontext", ""}
_SUB_FG = {"#555", "#555555", "#666", "#666666", "#888", "#888888", "#999",
           "#9aa0a6", "#aaa", "#aaaaaa"}
# 정보·링크용 파란 글자(라이트 모드 전제로 고른 진한 남색) — 다크 패널 위에서는
# 대비가 약해 강조색(accent)으로 치환.
_INFO_FG = {"#1565c0", "#0277bd"}


def _is_dark_color(hexc: str) -> bool:
    """배경색이 어두운지(흰 글자가 어울리는지) 판정. hex가 아니면 어둡지 않은 것으로."""
    h = (hexc or "").strip().lstrip("#")
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)
    if len(h) != 6:
        return False
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return False
    return (0.299 * r + 0.587 * g + 0.114 * b) < 140


def _blend_hex(bg_hex: str, target_hex: str, t: float) -> str:
    """bg_hex에서 target_hex 쪽으로 t(0~1)만큼 섞은 색. 버튼 배경이 무슨 색이든
    그 배경 대비 충분히 보이는 '톤다운된' 글자색을 만들 때 씁니다(고정 회색 대신)."""
    def _rgb(h):
        h = (h or "").strip().lstrip("#")
        if len(h) == 3:
            h = "".join(ch * 2 for ch in h)
        if len(h) != 6:
            return (0, 0, 0)
        try:
            return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
        except ValueError:
            return (0, 0, 0)
    r1, g1, b1 = _rgb(bg_hex)
    r2, g2, b2 = _rgb(target_hex)
    r = round(r1 + (r2 - r1) * t); g = round(g1 + (g2 - g1) * t); b = round(b1 + (b2 - b1) * t)
    return f"#{r:02x}{g:02x}{b:02x}"


def _disabled_fg_for(bg_hex: str) -> str:
    """버튼이 비활성화됐을 때 '회색이라 안 보이는' 문제 방지 — 배경이 어떤 색이든
    그 배경과 충분히 대비되는 톤(밝은 배경↔어둡게, 어두운 배경↔밝게)으로 흐리게."""
    base = "#ffffff" if _is_dark_color(bg_hex) else "#1a1a1a"
    return _blend_hex(bg_hex, base, 0.6)


class QueueWriter:
    """print 출력을 로그 큐로 보냅니다 (publisher의 print도 캡처).
    실제 파일 스트림처럼 보이도록 encoding 등 속성을 갖춥니다 — 일부 모듈이
    sys.stdout.encoding / reconfigure 를 호출해도 안전하게 동작합니다."""
    encoding = "utf-8"
    errors = "replace"

    def __init__(self, q):
        self.q = q
    def write(self, s):
        if s:
            self.q.put(s)
    def flush(self):
        pass
    def reconfigure(self, *args, **kwargs):
        pass
    def isatty(self):
        return False


class BlogStudio:
    def __init__(self, root):
        self.root = root
        reg = core.ensure_initialized()          # 멀티 블로그 프로필 준비(최초 1회 마이그레이션)
        self.active_blog = reg["active"]
        self.data = core.load_schedule()         # 활성 블로그 기준
        self.log_q = queue.Queue()
        self.prog_q = queue.Queue()
        self.busy = False
        self.busy_lock = threading.Lock()
        self.current_msg = "대기 중"
        self.task_start = None
        self.stop_requested = False   # 🛑 지금 작업 중단 — generate_post/publish_date가 안전한
                                       # 지점(소주제 사이·업로드 전)마다 확인해 멈춘다

        today = date.today()
        self.view_year = today.year
        self.view_month = today.month
        self.selected = today.isoformat()        # 활성 글 키(날짜 또는 날짜#번호)
        self.selected_date = today.isoformat()   # 선택한 캘린더 날짜
        self.view_mode = "month"                 # 월간/주간/일간 보기
        self.dark_mode = bool(self.data["settings"].get("dark_mode", False))
        self.day_buttons = {}   # date_str -> tk.Button
        self.multi_selected_dates = set()   # Ctrl+클릭으로 다중선택한 날짜(여러 날짜 한번에 삭제용)

        root.title("블로그 스튜디오 — 자동 발행")
        # 화면 크기에 맞춰 충분히 크게(가운데 정렬). 배율/작은 모니터에서도 하단(진행률·로그)이 보이도록.
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        w = min(1280, max(1040, sw - 80))
        h = min(1000, max(740, sh - 120))
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 2 - 20)
        root.geometry(f"{w}x{h}+{x}+{y}")
        root.minsize(980, 640)

        self._build_ui()
        self.refresh_calendar()
        self.load_day(self.selected)
        self._update_blog_label()
        self.apply_theme(self.dark_mode)   # 라이트도 적용(비활성 버튼 가독성 등)

        # 루프: 로그 펌프 / 헤더 카운트다운 / 작업 활성표시 / 자동발행 체크
        self.root.after(150, self._pump_log)
        self.root.after(500, self._tick_header)
        self.root.after(1000, self._tick_activity)
        self.root.after(5000, self._tick_scheduler)
        # 시작 시 1회: 이 PC에 쓸 모델이 있는지 조용히 점검(없으면 다운로드 제안)
        self.root.after(1800, lambda: self.check_and_pull_model(quiet=True))

    # ── UI 구성 ───────────────────────────────────────────────────────────────
    def _build_ui(self):
        # 헤더 (2줄: 다음 발행 / 발행 블로그)
        header = tk.Frame(self.root, bg="#263238", height=88)
        header.pack(fill="x")
        header.pack_propagate(False)

        top = tk.Frame(header, bg="#263238"); top.pack(fill="x", pady=(8, 0))
        self.header_lbl = tk.Label(
            top, text="다음 발행 정보 불러오는 중...",
            bg="#263238", fg="white", font=("맑은 고딕", 13, "bold"),
            anchor="w", padx=16)
        self.header_lbl.pack(side="left", fill="both", expand=True)
        self.auto_lbl = tk.Label(
            top, text="", bg="#263238", fg="#80cbc4",
            font=("맑은 고딕", 11), padx=16)
        self.auto_lbl.pack(side="right")

        bot = tk.Frame(header, bg="#263238"); bot.pack(fill="x", pady=(2, 8))
        tk.Label(bot, text="📍 발행 블로그:", bg="#263238", fg="#ffd54f",
                 font=("맑은 고딕", 11, "bold"), padx=16).pack(side="left")
        self.blog_sel_var = tk.StringVar()
        self.blog_combo = ttk.Combobox(bot, textvariable=self.blog_sel_var,
                                       state="readonly", width=34)
        self.blog_combo.pack(side="left")
        self.blog_combo.bind("<<ComboboxSelected>>", self._on_blog_selected)
        tk.Button(bot, text="+ 블로그 추가", command=self.add_blog_dialog,
                  font=("맑은 고딕", 9)).pack(side="left", padx=8)
        tk.Button(bot, text="🔐 로그인 관리", command=self.login_manager_dialog,
                  font=("맑은 고딕", 9)).pack(side="left")
        tk.Button(bot, text="⚙️ 설정", command=self.toggle_settings,
                  font=("맑은 고딕", 9)).pack(side="left", padx=8)
        tk.Button(bot, text="🏷 카테고리 정리", command=self.open_category_cleanup,
                  font=("맑은 고딕", 9)).pack(side="left")
        self.dark_btn = tk.Button(bot, text="🌙 다크", command=self.toggle_dark,
                                  font=("맑은 고딕", 9))
        self.dark_btn.pack(side="left")
        self.blog_lbl = tk.Label(bot, text="", bg="#263238", fg="#80cbc4",
                                 font=("맑은 고딕", 9), padx=8)
        self.blog_lbl.pack(side="right", padx=12)

        # ── 하단 고정 영역(설정 바 + 진행률) — 아래에 먼저 고정 ──────────────
        botwrap = tk.Frame(self.root)
        botwrap.pack(side="bottom", fill="x")
        self._build_settings_bar(botwrap)
        progf = tk.Frame(botwrap)
        progf.pack(fill="x", padx=10, pady=(2, 6))
        self._progf = progf
        # 설정 바는 기본 숨김 — 헤더의 [⚙️ 설정] 버튼으로 열고 닫음(로그·정보 공간 확보)
        if getattr(self, "settings_box", None):
            self.settings_box.pack_forget()
        self.status_lbl = tk.Label(progf, text="대기 중", anchor="w",
                                   font=("맑은 고딕", 10, "bold"), fg="#1565c0")
        self.status_lbl.pack(fill="x")
        barrow = tk.Frame(progf); barrow.pack(fill="x", pady=(2, 0))
        self.progress_var = tk.DoubleVar(value=0)
        self.progressbar = ttk.Progressbar(barrow, orient="horizontal",
                                           mode="determinate", maximum=100,
                                           variable=self.progress_var)
        self.progressbar.pack(side="left", fill="x", expand=True)
        self.pct_lbl = tk.Label(barrow, text="0%", width=6,
                                font=("맑은 고딕", 10, "bold"))
        self.pct_lbl.pack(side="left", padx=(8, 0))
        tk.Button(barrow, text="🛑 지금 작업 중단", command=self.stop_current_job,
                  bg="#c62828", fg="white", font=("맑은 고딕", 9, "bold")).pack(side="left", padx=(10, 0))

        # ── 가운데: 탭 ↕ 로그 (경계선을 위아래로 끌어 높이 조절) ───────────────
        main = ttk.PanedWindow(self.root, orient="vertical")
        main.pack(side="top", fill="both", expand=True, padx=10, pady=(8, 4))
        nb_frame = tk.Frame(main)
        logf = tk.LabelFrame(main, text="진행 상황 로그  (경계선을 끌어 크기 조절)")
        main.add(nb_frame, weight=4)
        main.add(logf, weight=1)

        nb = ttk.Notebook(nb_frame)
        nb.pack(fill="both", expand=True)
        self.nb = nb                      # 탭 전환용(발행 계획 → 날짜별 이동)
        series_tab = tk.Frame(nb)
        weekly_tab = tk.Frame(nb)
        plan_tab = tk.Frame(nb)
        date_tab = tk.Frame(nb)
        self.date_tab = date_tab
        events_tab = tk.Frame(nb)
        photos_tab = tk.Frame(nb)
        nb.add(series_tab, text="  🎬 시리즈 기획  ")
        nb.add(weekly_tab, text="  📅 주간 요일별 발행  ")
        nb.add(plan_tab, text="  📆 발행 계획  ")
        nb.add(date_tab, text="  🗓 날짜별 발행  ")
        nb.add(events_tab, text="  🗄 이벤트 (공연·전시)  ")
        nb.add(photos_tab, text="  📚 내 사진  ")
        self._build_series_tab(series_tab)
        self._build_weekly_tab(weekly_tab)
        self._build_plan_tab(plan_tab)
        self._build_date_tab(date_tab)
        self._build_events_tab(events_tab)
        self._build_photos_tab(photos_tab)

        self.log = scrolledtext.ScrolledText(
            logf, height=10, font=("맑은 고딕", 10), state="disabled",
            bg="#ffffff", fg="#1a1a1a", wrap="word", spacing1=1, spacing3=1)
        self.log.pack(fill="both", expand=True, padx=4, pady=4)

    def _build_date_tab(self, parent):
        # 캘린더 ↔ 사이드 패널: 경계선을 좌우로 끌어 너비 조절(가로 sash)
        pw = ttk.PanedWindow(parent, orient="horizontal")
        pw.pack(fill="both", expand=True)
        left = tk.Frame(pw)
        right = tk.Frame(pw)
        pw.add(left, weight=3)
        pw.add(right, weight=1)
        self._build_calendar_frame(left)
        self._build_side_panel(right)

    def _build_series_tab(self, parent):
        self.last_plan = None
        s = self.data["settings"]
        # 설명·정체성은 토글(기본 숨김) — 버튼으로 열고 닫아 미리보기 공간 확보
        toprow = tk.Frame(parent); toprow.pack(fill="x", padx=14, pady=(8, 0))
        tk.Button(toprow, text="ℹ️ 설명·정체성 보기/숨기기", command=self.toggle_series_info,
                  font=("맑은 고딕", 9)).pack(side="left")

        self.series_info = tk.Frame(parent)
        tk.Label(self.series_info,
                 text="이 블로그의 색깔(정체성)과 평소 주제 안에서, 외국인이 관심 있을 '연결성 있는 시리즈'를 기획합니다.\n"
                      "① 기획하기 → 미리보기 확인 → ② 캘린더에 넣기 순서로 진행하세요.\n"
                      "각 글은 제목·후킹 들어가는말·본문·다음 편을 예고하는 맺음말로 작성되고, 구글 SEO를 고려합니다.",
                 font=("맑은 고딕", 11), fg="#333", justify="left").pack(anchor="w", pady=(8, 6))
        idbox = tk.LabelFrame(self.series_info,
                              text="이 블로그의 색깔(정체성) — 기획·작성이 이 틀 안에서 이뤄집니다",
                              font=("맑은 고딕", 10, "bold"))
        idbox.pack(fill="x", pady=(0, 8))
        self.identity_text = tk.Text(idbox, height=4, font=("맑은 고딕", 9), wrap="word")
        self.identity_text.pack(fill="x", padx=6, pady=(6, 2))
        self.identity_text.insert("1.0", s.get("blog_identity", ""))
        tk.Label(idbox, text="※ 수정 후 기획하면 바뀐 색깔이 반영됩니다. [🗓 날짜별] 글 생성에도 함께 적용돼요.",
                 fg="#888", font=("맑은 고딕", 8)).pack(anchor="w", padx=6, pady=(0, 4))
        tk.Button(idbox, text="📋 이 블로그에 k-arts-now(시의성 큐레이션) 전략 적용",
                  command=self.apply_karts_now, font=("맑은 고딕", 8)).pack(anchor="w", padx=6, pady=(0, 6))

        ctrl = tk.LabelFrame(parent, text="기획 조건", font=("맑은 고딕", 10, "bold"))
        ctrl.pack(fill="x", padx=14, pady=(0, 8))
        self._series_ctrl = ctrl
        self.series_info.pack(fill="x", padx=14)
        self.series_info.pack_forget()        # 기본 숨김

        r = tk.Frame(ctrl); r.pack(fill="x", padx=8, pady=4)
        tk.Label(r, text="카테고리", width=14, anchor="w").pack(side="left")
        self.series_category_var = tk.StringVar()
        # 이 블로그의 요일별 발행 주제 + 직접 추가한 주제로 채움(편집 가능: 직접 입력도 OK).
        self.series_category_combo = ttk.Combobox(
            r, textvariable=self.series_category_var, width=24,
            values=core.blog_categories(self.data))
        self.series_category_combo.pack(side="left")
        tk.Button(r, text="+ 추가", command=self.add_series_category,
                  font=("맑은 고딕", 8)).pack(side="left", padx=(4, 0))
        self.btn_research = tk.Button(r, text="🔎 관심 키워드 조사(1~10위)",
                                      command=self.run_research_keywords,
                                      bg="#0277bd", fg="white")
        self.btn_research.pack(side="left", padx=8)
        tk.Button(r, text="🏆 키워드 풀", command=self.open_keyword_pool,
                  bg="#6a1b9a", fg="white").pack(side="left")
        self._refresh_categories(select_first=True)
        tk.Label(ctrl, text="※ 카테고리는 [📅 요일별 발행]의 주제에서 자동으로 채워집니다. "
                            "새 주제는 입력 후 [+ 추가] (블로그마다 따로 저장).",
                 fg="#888", font=("맑은 고딕", 8)).pack(anchor="w", padx=12, pady=(0, 2))
        tk.Label(ctrl, text="※ [🏆 키워드 풀]: 무료 신호(LLM 관심도 순위 + 구글 자동완성)로 "
                            "키워드를 미리 모아 점수 매겨 두고, 시리즈 기획할 때 상위 점수부터 "
                            "꺼내 쓰는 대기열입니다(같은 키워드를 두 번 안 쓰게 자동 관리).",
                 fg="#888", font=("맑은 고딕", 8), wraplength=560, justify="left").pack(
            anchor="w", padx=12, pady=(0, 2))

        r = tk.Frame(ctrl); r.pack(fill="x", padx=8, pady=4)
        tk.Label(r, text="중심 테마/키워드", width=14, anchor="w").pack(side="left")
        self.series_theme_var = tk.StringVar()
        tk.Entry(r, textvariable=self.series_theme_var).pack(side="left", fill="x", expand=True)
        tk.Label(r, text="조사에서 선택되거나 직접 입력(비우면 자동)", fg="#888",
                 font=("맑은 고딕", 8)).pack(side="left", padx=4)

        r = tk.Frame(ctrl); r.pack(fill="x", padx=8, pady=(4, 0))
        tk.Label(r, text="기획 요청사항", width=14, anchor="nw").pack(side="left", anchor="n")
        rc = tk.Frame(r); rc.pack(side="left", fill="x", expand=True)
        self.series_request_text = tk.Text(rc, height=3, font=("맑은 고딕", 9), wrap="word")
        self.series_request_text.pack(fill="x")
        tk.Label(rc, text="원하는 방향을 자유롭게 적으면 그대로 기획에 반영됩니다 "
                          "(예: '발레 입문자용 5편, 공연장 방문 팁과 관람 매너 포함').",
                 fg="#888", font=("맑은 고딕", 8), wraplength=520, justify="left").pack(anchor="w")

        # 사진 폴더(선택) — 지정하면 그 사진들을 실제로 분석해 시리즈 소재를 그 내용에 맞춤
        pr = tk.Frame(ctrl); pr.pack(fill="x", padx=8, pady=(6, 0))
        tk.Label(pr, text="사진 폴더(선택)", width=14, anchor="w").pack(side="left")
        self.series_photo_dir_var = tk.StringVar()
        tk.Entry(pr, textvariable=self.series_photo_dir_var, font=("맑은 고딕", 8)).pack(
            side="left", fill="x", expand=True, padx=(0, 4))
        tk.Button(pr, text="찾기", width=5, command=self.pick_series_photo_dir).pack(side="left")
        tk.Button(pr, text="📚 내 사진에서 찾기", command=self.pick_series_photo_from_library
                 ).pack(side="left", padx=(4, 0))
        tk.Button(pr, text="해제", width=4, command=self.clear_series_photo_dir).pack(
            side="left", padx=(2, 0))
        tk.Label(ctrl, text="실제 촬영한 사진 폴더를 지정하면, 그 사진들의 실제 내용을 파악해 "
                            "시리즈 소재가 사진과 무관하게 흘러가지 않도록 기획합니다. "
                            "캘린더에 넣을 때 각 편에도 이 폴더가 함께 지정됩니다. "
                            "하위 폴더가 여럿이면(장소별로 나뉜 시리즈) 편마다 다른 하위 폴더가 "
                            "자동으로 하나씩 배정됩니다.",
                 fg="#888", font=("맑은 고딕", 8), wraplength=520, justify="left").pack(
            anchor="w", padx=8)

        r = tk.Frame(ctrl); r.pack(fill="x", padx=8, pady=4)
        tk.Label(r, text="편수", width=14, anchor="w").pack(side="left")
        self.series_count_var = tk.StringVar(value=str(s.get("series_count", 5)))
        tk.Spinbox(r, from_=3, to=12, width=4, textvariable=self.series_count_var).pack(side="left")
        tk.Label(r, text="   시작 날짜", anchor="w").pack(side="left", padx=(12, 4))
        self.series_start_var = tk.StringVar(
            value=(date.today() + timedelta(days=1)).isoformat())
        tk.Entry(r, textvariable=self.series_start_var, width=12).pack(side="left")
        tk.Label(r, text="   발행 간격(일)", anchor="w").pack(side="left", padx=(12, 4))
        self.series_interval_var = tk.StringVar(value="1")
        tk.Spinbox(r, from_=1, to=14, width=4, textvariable=self.series_interval_var).pack(side="left")

        brow = tk.Frame(ctrl); brow.pack(fill="x", padx=8, pady=(2, 8))
        self.btn_plan = tk.Button(brow, text="① 시리즈 기획하기", command=self.run_plan_series,
                                  bg="#6a1b9a", fg="white", font=("맑은 고딕", 10, "bold"))
        self.btn_plan.pack(side="left")
        self.btn_apply = tk.Button(brow, text="② 캘린더에 넣기", command=self.apply_planned_series,
                                   bg="#2e7d32", fg="white", font=("맑은 고딕", 10, "bold"),
                                   state="disabled")
        self.btn_apply.pack(side="left", padx=8)
        tk.Button(brow, text="■ 기획 중단", command=self.stop_plan_series,
                  fg="#c62828").pack(side="left", padx=(8, 0))
        tk.Label(brow, text="(편수를 잘못 입력하는 등 실수했을 때 — 지금 진행 중인 응답 1회는 "
                            "끝까지 기다리고 그다음 재시도부터 멈춥니다)",
                 fg="#888", font=("맑은 고딕", 8)).pack(side="left", padx=6)

        prevf = tk.LabelFrame(parent, text="기획 미리보기", font=("맑은 고딕", 10, "bold"))
        prevf.pack(fill="both", expand=True, padx=14, pady=(0, 10))
        self.series_preview = scrolledtext.ScrolledText(
            prevf, height=10, font=("맑은 고딕", 10), state="disabled", wrap="word",
            bg="#ffffff", fg="#1a1a1a")
        self.series_preview.pack(fill="both", expand=True, padx=4, pady=4)

    def _build_weekly_tab(self, parent):
        tk.Label(parent,
                 text="요일을 선택하면 오른쪽에서 주제·발행시각·작성 방향·참고문서를 크게 보며 편집할 수 있어요.\n"
                      "체크(✅)한 요일은 [🗓 날짜별 발행] 캘린더에 자동으로 채워지고, 정한 시각에 자동 발행됩니다.",
                 font=("맑은 고딕", 11), fg="#333", justify="left").pack(anchor="w", padx=14, pady=(12, 8))

        body = tk.Frame(parent); body.pack(fill="both", expand=True, padx=14, pady=(0, 10))

        # 왼쪽: 요일 목록 ──────────────────────────────────────────────
        leftf = tk.Frame(body, width=240); leftf.pack(side="left", fill="y", padx=(0, 12))
        leftf.pack_propagate(False)
        tk.Label(leftf, text="요일 선택", font=("맑은 고딕", 10, "bold")).pack(anchor="w", pady=(0, 4))
        self.wk_list_frame = tk.Frame(leftf); self.wk_list_frame.pack(fill="both", expand=True)

        # 오른쪽: 상세 편집 패널 ───────────────────────────────────────
        rightf = tk.Frame(body, relief="groove", bd=1); rightf.pack(side="left", fill="both", expand=True)
        pad = tk.Frame(rightf); pad.pack(fill="both", expand=True, padx=14, pady=12)
        self.wk_title_lbl = tk.Label(pad, text="", font=("맑은 고딕", 14, "bold"))
        self.wk_title_lbl.pack(anchor="w")
        self.wk_enabled_var = tk.BooleanVar(value=False)
        tk.Checkbutton(pad, text="이 요일 자동 발행 사용", variable=self.wk_enabled_var,
                       font=("맑은 고딕", 10)).pack(anchor="w", pady=(4, 8))

        r = tk.Frame(pad); r.pack(fill="x", pady=3)
        tk.Label(r, text="주제(시리즈 테마)", width=16, anchor="w").pack(side="left")
        self.wk_topic_var = tk.StringVar()
        tk.Entry(r, textvariable=self.wk_topic_var, font=("맑은 고딕", 10)).pack(
            side="left", fill="x", expand=True)

        r = tk.Frame(pad); r.pack(fill="x", pady=3)
        tk.Label(r, text="발행시각(비우면 기본)", width=16, anchor="w").pack(side="left")
        self.wk_time_var = tk.StringVar()
        tk.Entry(r, textvariable=self.wk_time_var, width=12).pack(side="left")

        tk.Label(pad, text="참고 사이트 · 작성 방향", anchor="w",
                 font=("맑은 고딕", 10, "bold")).pack(anchor="w", pady=(8, 2))
        tf = tk.Frame(pad); tf.pack(fill="both", expand=True)
        self.wk_refs_text = tk.Text(tf, height=10, wrap="word", font=("맑은 고딕", 10),
                                    relief="solid", bd=1)
        self.wk_refs_text.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.wk_refs_text.yview)
        self.wk_refs_text.configure(yscrollcommand=sb.set); sb.pack(side="left", fill="y")

        r = tk.Frame(pad); r.pack(fill="x", pady=(8, 2))
        tk.Label(r, text="참고 .md 파일:", anchor="w").pack(side="left")
        self.wk_md_var = tk.StringVar()
        self.wk_md_lbl = tk.Label(r, font=("맑은 고딕", 9), fg="#1565c0")
        tk.Button(r, text="📄 선택", command=lambda: self._pick_weekly_md(
            self.wk_md_var, self.wk_md_lbl)).pack(side="left", padx=(6, 2))
        tk.Button(r, text="✕ 제거", command=lambda: self._clear_weekly_md(
            self.wk_md_var, self.wk_md_lbl)).pack(side="left")
        self.wk_md_lbl.pack(side="left", padx=6)

        btnr = tk.Frame(pad); btnr.pack(fill="x", pady=(12, 0))
        tk.Button(btnr, text="💾 이 요일 저장", command=self.save_weekday,
                  bg="#2e7d32", fg="white", font=("맑은 고딕", 11, "bold")).pack(side="left")
        tk.Label(btnr, text="• 다른 요일로 옮기기 전에 저장하세요. .md를 붙이면 글 쓸 때 함께 참고합니다.",
                 fg="#888", font=("맑은 고딕", 9)).pack(side="left", padx=10)

        self.wk_selected_wd = None
        self._refresh_weekly_list()
        self._load_weekday_into_panel(0)   # 월요일부터

    def _refresh_weekly_list(self):
        """왼쪽 요일 목록을 (사용여부·주제 미리보기·md 표시) 새로 그림. 선택 요일 하이라이트."""
        fr = getattr(self, "wk_list_frame", None)
        if fr is None:
            return
        for w in fr.winfo_children():
            w.destroy()
        for wd in range(7):
            wdata = core.get_weekly(self.data, wd)
            enabled = wdata["enabled"]
            selected = (wd == self.wk_selected_wd)
            # 세 상태를 배경·글자색으로 또렷이 구분:
            #  선택됨 = 진한 파랑 + 흰 글씨 / 사용(켜짐) = 연녹 + 진녹 / 미사용 = 회색 + 흐린 글씨
            if selected:
                bg, name_fg, sub_fg, rel, bd = "#1565c0", "#ffffff", "#d6e4f7", "solid", 2
            elif enabled:
                bg, name_fg, sub_fg, rel, bd = "#e8f5e9", "#1b5e20", "#4e7350", "flat", 1
            else:
                bg, name_fg, sub_fg, rel, bd = "#eeeeee", "#9e9e9e", "#bdbdbd", "flat", 1
            row = tk.Frame(fr, bg=bg, padx=10, pady=7, cursor="hand2", relief=rel, bd=bd)
            row.pack(fill="x", pady=2)
            on = "✅" if enabled else "⬜"
            name = tk.Label(row, text=f"{on}  {core.WEEKDAY_KO[wd]}요일",
                            font=("맑은 고딕", 11, "bold"), fg=name_fg, bg=bg)
            name.pack(anchor="w")
            prev = (wdata["topic"] or "(주제 없음)")[:22]
            md = "  📄" if wdata.get("md_file") else ""
            sub = tk.Label(row, text=prev + md, font=("맑은 고딕", 9),
                           fg=sub_fg, bg=bg)
            sub.pack(anchor="w")
            for wdg in (row, name, sub):
                wdg.bind("<Button-1>", lambda e, d=wd: self._load_weekday_into_panel(d))

    def _load_weekday_into_panel(self, wd):
        """선택한 요일 데이터를 오른쪽 패널에 로드(+목록 하이라이트 갱신)."""
        self.wk_selected_wd = wd
        wdata = core.get_weekly(self.data, wd)
        fg = "#1565c0" if wd == 5 else ("#d32f2f" if wd == 6 else "#222")
        self.wk_title_lbl.config(text=f"{core.WEEKDAY_KO[wd]}요일 설정", fg=fg)
        self.wk_enabled_var.set(wdata["enabled"])
        self.wk_topic_var.set(wdata["topic"])
        self.wk_time_var.set(wdata["time"])
        self.wk_refs_text.delete("1.0", "end")
        self.wk_refs_text.insert("1.0", wdata["refs"])
        self.wk_md_var.set(wdata.get("md_file", ""))
        self._set_md_label(self.wk_md_lbl, self.wk_md_var.get())
        self._refresh_weekly_list()

    def save_weekday(self):
        """오른쪽 패널에서 편집한 '현재 요일'을 저장."""
        wd = getattr(self, "wk_selected_wd", None)
        if wd is None:
            return
        core.set_weekly(self.data, wd,
                        self.wk_enabled_var.get(),
                        self.wk_topic_var.get(),
                        self.wk_refs_text.get("1.0", "end").strip(),
                        self.wk_time_var.get(),
                        self.wk_md_var.get())
        core.save_schedule(self.data)
        self._refresh_weekly_list()
        self.refresh_calendar()
        self._tick_header()
        self._refresh_categories()   # 요일 주제가 시리즈 카테고리에 바로 반영
        if hasattr(self, "plan_upcoming"):
            self.refresh_plan_lists()
        self._log(f"💾 {core.WEEKDAY_KO[wd]}요일 주간 템플릿 저장 완료\n")

    # ── 발행 계획 탭 — 발행 완료/예정 흐름 + 제목 사전생성 ──────────────────────
    def _build_plan_tab(self, parent):
        tk.Label(parent,
                 text="앞으로의 발행 흐름을 한눈에. 활성 요일의 제목을 미리 생성해 캘린더를 채우고,\n"
                      "발행 완료/예정을 확인하세요. 더블클릭하면 그 날짜 편집으로 이동합니다.",
                 font=("맑은 고딕", 11), fg="#333", justify="left").pack(anchor="w", padx=14, pady=(12, 8))

        bar = tk.Frame(parent); bar.pack(fill="x", padx=14, pady=(0, 6))
        tk.Label(bar, text="범위:").pack(side="left")
        self.plan_months_var = tk.StringVar(value="1개월")
        ttk.Combobox(bar, textvariable=self.plan_months_var, width=8, state="readonly",
                     values=["1개월", "3개월", "6개월", "12개월"]).pack(side="left", padx=(4, 10))
        tk.Button(bar, text="🔮 앞으로 제목 미리 생성", command=self.run_plan_titles,
                  bg="#6a1b9a", fg="white", font=("맑은 고딕", 10, "bold")).pack(side="left")
        tk.Button(bar, text="🔄 새로고침", command=self.refresh_plan_lists).pack(side="left", padx=6)
        tk.Button(bar, text="🔗 블로그와 동기화", command=self.run_sync_published).pack(side="left", padx=6)
        tk.Label(bar, text="(제목 생성은 로컬 AI만 사용 — 발행 쿼터와 무관)",
                 fg="#888", font=("맑은 고딕", 9)).pack(side="left", padx=8)

        # 과거 날짜 채우기(백필) — 요일별 발행 설정을 바꾼 뒤 지난 날짜까지 소급 적용할 때
        barpast = tk.Frame(parent); barpast.pack(fill="x", padx=14, pady=(0, 6))
        tk.Label(barpast, text="과거 채우기: 시작일").pack(side="left")
        self.plan_past_start_var = tk.StringVar(
            value=(datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d"))
        tk.Entry(barpast, textvariable=self.plan_past_start_var, width=11).pack(side="left", padx=(4, 8))
        tk.Label(barpast, text="~ 종료일(선택한 날짜)").pack(side="left")
        self.plan_past_end_var = tk.StringVar(value=datetime.now().strftime("%Y-%m-%d"))
        tk.Entry(barpast, textvariable=self.plan_past_end_var, width=11).pack(side="left", padx=(4, 8))
        tk.Button(barpast, text="📌 캘린더 선택일 채우기", font=("맑은 고딕", 8),
                  command=self._plan_use_selected_date).pack(side="left")
        tk.Button(barpast, text="🔮 과거 날짜 제목 채우기", command=self.run_plan_titles_past,
                  bg="#6a1b9a", fg="white", font=("맑은 고딕", 10, "bold")).pack(side="left", padx=(10, 0))
        tk.Label(barpast, text="(YYYY-MM-DD 직접 입력 가능, 시작~종료 날짜 안의 요일 템플릿을 소급 배치)",
                 fg="#888", font=("맑은 고딕", 9)).pack(side="left", padx=8)

        # 묶음 생성·발행 바(아래 '발행 대기·예정' 목록에서 여러 개 선택)
        bar2 = tk.Frame(parent); bar2.pack(fill="x", padx=14, pady=(0, 6))
        tk.Label(bar2, text="선택 글 간격:").pack(side="left")
        self.batch_gap_var = tk.StringVar(value="1시간")
        ttk.Combobox(bar2, textvariable=self.batch_gap_var, width=9, state="readonly",
                     values=["즉시 연속", "10분", "30분", "1시간", "2시간"]).pack(side="left", padx=(4, 10))
        tk.Button(bar2, text="▶ 선택 글 차례로 생성·발행", command=self.run_batch_publish,
                  bg="#2e7d32", fg="white", font=("맑은 고딕", 10, "bold")).pack(side="left")
        tk.Button(bar2, text="■ 중단", command=self.stop_batch).pack(side="left", padx=6)
        tk.Label(bar2, text="(왼쪽 목록에서 Ctrl/Shift로 여러 개 선택 → 차례로 하나씩. 진행 중 프로그램 켜 두세요)",
                 fg="#888", font=("맑은 고딕", 9)).pack(side="left", padx=8)

        body = tk.Frame(parent); body.pack(fill="both", expand=True, padx=14, pady=(0, 4))
        # 발행 대기·예정(좌) — 여러 개 선택 가능
        leftf = tk.Frame(body); leftf.pack(side="left", fill="both", expand=True, padx=(0, 6))
        lhead = tk.Frame(leftf); lhead.pack(fill="x")
        tk.Label(lhead, text="📅 발행 대기·예정 (여러 개 선택 가능)", font=("맑은 고딕", 11, "bold"),
                 fg="#6a1b9a").pack(side="left")
        tk.Button(lhead, text="☑ 전체선택", font=("맑은 고딕", 8),
                  command=self._plan_select_all_upcoming).pack(side="right", padx=(4, 0))
        tk.Button(lhead, text="🗑 선택 삭제", font=("맑은 고딕", 8, "bold"), fg="#c62828",
                  command=self._plan_delete_selected_upcoming).pack(side="right")
        up = ttk.Treeview(leftf, columns=("date", "wd", "title", "status"),
                          show="headings", height=18, selectmode="extended")
        for c, t, w in [("date", "날짜", 92), ("wd", "요일", 44),
                        ("title", "제목", 300), ("status", "상태", 70)]:
            up.heading(c, text=t); up.column(c, width=w, anchor=("w" if c == "title" else "center"))
        up.pack(side="left", fill="both", expand=True)
        sbu = ttk.Scrollbar(leftf, orient="vertical", command=up.yview)
        up.configure(yscrollcommand=sbu.set); sbu.pack(side="left", fill="y")
        up.bind("<Double-1>", self._plan_open_selected)
        self.plan_upcoming = up
        # 발행 완료(우)
        rightf = tk.Frame(body); rightf.pack(side="left", fill="both", expand=True, padx=(6, 0))
        rhead = tk.Frame(rightf); rhead.pack(fill="x")
        tk.Label(rhead, text="✅ 발행 완료", font=("맑은 고딕", 11, "bold"),
                 fg="#2e7d32").pack(side="left")
        tk.Button(rhead, text="☑ 전체선택", font=("맑은 고딕", 8),
                  command=self._plan_select_all_done).pack(side="right", padx=(4, 0))
        tk.Button(rhead, text="🗑 선택 삭제", font=("맑은 고딕", 8, "bold"), fg="#c62828",
                  command=self._plan_delete_selected_done).pack(side="right")
        dn = ttk.Treeview(rightf, columns=("date", "wd", "title"),
                          show="headings", height=18, selectmode="extended")
        for c, t, w in [("date", "날짜", 92), ("wd", "요일", 44), ("title", "제목", 320)]:
            dn.heading(c, text=t); dn.column(c, width=w, anchor=("w" if c == "title" else "center"))
        dn.pack(side="left", fill="both", expand=True)
        sbd = ttk.Scrollbar(rightf, orient="vertical", command=dn.yview)
        dn.configure(yscrollcommand=sbd.set); sbd.pack(side="left", fill="y")
        dn.bind("<Double-1>", self._plan_open_selected)
        self.plan_done = dn

        self.plan_count_lbl = tk.Label(parent, text="", fg="#555", font=("맑은 고딕", 9))
        self.plan_count_lbl.pack(anchor="w", padx=14, pady=(0, 8))
        self.refresh_plan_lists()

    def refresh_plan_lists(self):
        up = getattr(self, "plan_upcoming", None)
        dn = getattr(self, "plan_done", None)
        if up is None or dn is None:
            return
        for tv in (up, dn):
            for i in tv.get_children():
                tv.delete(i)
        today = datetime.now().strftime("%Y-%m-%d")
        upcoming, done = [], []
        for key, e in (self.data.get("entries") or {}).items():
            ds = core.post_date(key)
            topic = (e.get("topic") or "").strip()
            if e.get("status") == core.ST_PUBLISHED:
                done.append((ds, key, e, topic))
            elif topic:                      # 미발행 + 주제 있음 = 발행 대기(과거 날짜 포함)
                upcoming.append((ds, key, e, topic))
        upcoming.sort(key=lambda x: x[0])
        done.sort(key=lambda x: x[0], reverse=True)
        stmap = {core.ST_PENDING: "대기", core.ST_GENERATED: "생성됨"}
        for ds, key, e, topic in upcoming:
            wd = core.WEEKDAY_KO[datetime.strptime(ds, "%Y-%m-%d").weekday()]
            mark = "🔮 " if e.get("planned") else ""
            st_txt = stmap.get(e.get("status"), "대기")
            if ds < today and e.get("status") != core.ST_GENERATED:
                st_txt = "지남·" + st_txt
            up.insert("", "end", iid=key,
                      values=(ds, wd, mark + (topic or "(제목 없음)"), st_txt))
        for ds, key, e, topic in done:
            wd = core.WEEKDAY_KO[datetime.strptime(ds, "%Y-%m-%d").weekday()]
            dn.insert("", "end", iid=key, values=(ds, wd, topic or "(제목 없음)"))
        self.plan_count_lbl.config(text=f"발행 대기·예정 {len(upcoming)}편 · 발행 완료 {len(done)}편")

    def _plan_select_all_upcoming(self):
        up = getattr(self, "plan_upcoming", None)
        if up is not None:
            up.selection_set(up.get_children())

    def _plan_delete_selected_upcoming(self):
        """발행 대기·예정 목록에서 선택한 항목을 한 번에 삭제(생성 캐시 포함).
        전부 미발행 상태라 블로그 API 호출 없이 스케줄에서만 지우면 됨."""
        up = getattr(self, "plan_upcoming", None)
        if up is None:
            return
        sel = list(up.selection())
        if not sel:
            messagebox.showinfo("삭제", "지울 항목을 하나 이상 선택하세요(전체선택 버튼도 있습니다).")
            return
        if not messagebox.askyesno(
                "삭제 확인",
                f"선택한 {len(sel)}편을 발행 계획에서 삭제할까요?\n"
                "(생성된 초안 캐시도 함께 삭제되어, 다음엔 새로 생성됩니다)"):
            return
        for key in sel:
            self.data["entries"].pop(key, None)
            core.delete_generated(core.post_date(key))
        core.save_schedule(self.data)
        self.refresh_calendar()
        self.refresh_plan_lists()
        self._log(f"🗑 발행 대기·예정 {len(sel)}편을 삭제했습니다(생성 캐시 포함).\n")

    def _plan_select_all_done(self):
        dn = getattr(self, "plan_done", None)
        if dn is not None:
            dn.selection_set(dn.get_children())

    def _plan_delete_selected_done(self):
        """발행 완료 목록에서 선택한 항목을 삭제 — 블로그 글도 지울지 스케줄만 지울지 선택."""
        dn = getattr(self, "plan_done", None)
        if dn is None:
            return
        sel = list(dn.selection())
        if not sel:
            messagebox.showinfo("삭제", "지울 항목을 하나 이상 선택하세요(전체선택 버튼도 있습니다).")
            return
        ans = messagebox.askyesnocancel(
            "발행 완료 글 삭제",
            f"선택한 {len(sel)}편을 삭제합니다.\n\n"
            "[예]      → 블로그 + 스케줄 모두 삭제\n"
            "[아니오] → 스케줄(캘린더)만 삭제 (블로그 글은 유지)\n"
            "[취소]   → 아무것도 하지 않음")
        if ans is None:
            return
        if ans:
            self._set_buttons(False)
            entries_to_del = {k: (self.data["entries"].get(k) or {}) for k in sel}

            def job():
                for key, e in entries_to_del.items():
                    self._log(f"🗑 블로그 삭제 중: {key}\n")
                    try:
                        res = core.delete_blog_posts(e, log=self._log)
                        failed = [l for l, s in res.items() if s == "fail"]
                        if failed:
                            self._log(f"   ⚠️ {key} 일부 삭제 실패: {failed}\n")
                    except Exception as ex:
                        self._log(f"   ❌ {key} 오류: {ex}\n")
                self.root.after(0, _finish)

            def _finish():
                for key in entries_to_del:
                    self.data["entries"].pop(key, None)
                core.save_schedule(self.data)
                self.refresh_calendar()
                self.refresh_plan_lists()
                self._set_buttons(True)
                self._log(f"✅ {len(entries_to_del)}편 삭제 완료(블로그+스케줄)\n")

            threading.Thread(target=job, daemon=True).start()
        else:
            for key in sel:
                self.data["entries"].pop(key, None)
            core.save_schedule(self.data)
            self.refresh_calendar()
            self.refresh_plan_lists()
            self._log(f"🧹 발행 기록 {len(sel)}편을 스케줄에서만 삭제했습니다(블로그 글 유지).\n")

    def _plan_open_selected(self, event):
        sel = event.widget.selection()
        if not sel:
            return
        key = sel[0]
        try:
            self.nb.select(self.date_tab)
        except Exception:
            pass
        self.load_day(core.post_date(key))

    def run_plan_titles(self):
        months = {"1개월": 1, "3개월": 3, "6개월": 6, "12개월": 12}.get(
            self.plan_months_var.get(), 1)
        if months >= 6 and not messagebox.askyesno(
                "제목 사전생성",
                f"{months}개월치 제목을 미리 생성합니다.\n"
                "활성 요일 수에 따라 수십 편을 AI로 기획하므로 몇 분 걸릴 수 있어요.\n"
                "(로컬 AI만 사용 — 발행 쿼터와 무관)\n\n계속할까요?"):
            return
        settings = self._collect_settings()

        def job():
            res = core.plan_calendar_titles(self.data, settings, months=months,
                                            log=self.log_q.put, progress=self._progress_cb)
            total = sum(res.values()) if res else 0
            self.log_q.put(f"\n🔮 제목 사전생성 완료 — 총 {total}편 배치: {res}\n")

        def done():
            self.refresh_plan_lists()
            self.refresh_calendar()

        self._start_worker(job, f"앞으로 제목 미리 생성 ({months}개월)", on_done=done)

    def _plan_use_selected_date(self):
        """캘린더에서 선택한 날짜(🗓 날짜별 발행 탭)를 종료일 칸에 채워 넣는다."""
        ds = getattr(self, "selected_date", None)
        if not ds:
            messagebox.showinfo("선택한 날짜 없음", "먼저 🗓 날짜별 발행 탭에서 날짜를 클릭해 선택하세요.")
            return
        self.plan_past_end_var.set(ds)

    def run_plan_titles_past(self):
        """요일별 발행 설정에 맞춰 [시작일, 종료일] 범위(과거 날짜 포함)의 빈 날짜에
        제목을 소급 배치. 제목만 채우며, 실제 생성·발행은 아래 '선택 글 차례로 생성·발행'로."""
        start = self.plan_past_start_var.get().strip()
        end = self.plan_past_end_var.get().strip()
        try:
            sd = datetime.strptime(start, "%Y-%m-%d").date()
            ed = datetime.strptime(end, "%Y-%m-%d").date()
        except ValueError:
            messagebox.showwarning("날짜 형식 오류", "시작일·종료일을 YYYY-MM-DD 형식으로 입력하세요.")
            return
        if sd > ed:
            messagebox.showwarning("날짜 범위 오류", "시작일이 종료일보다 늦습니다.")
            return
        ndays = (ed - sd).days + 1
        if ndays > 400 and not messagebox.askyesno(
                "긴 기간 확인",
                f"{start} ~ {end} — {ndays}일치 범위입니다. 활성 요일 수에 따라 꽤 많은 편을 "
                "AI로 기획하므로 시간이 걸릴 수 있어요.\n(로컬 AI만 사용 — 발행 쿼터와 무관)\n\n계속할까요?"):
            return
        settings = self._collect_settings()

        def job():
            res = core.plan_calendar_titles_range(
                self.data, settings, start, end,
                log=self.log_q.put, progress=self._progress_cb)
            total = sum(res.values()) if res else 0
            self.log_q.put(f"\n🔮 과거 제목 채우기 완료({start}~{end}) — 총 {total}편 배치: {res}\n")

        def done():
            self.refresh_plan_lists()
            self.refresh_calendar()

        self._start_worker(job, f"과거 날짜 제목 채우기 ({start} ~ {end})", on_done=done)

    def run_sync_published(self):
        if not messagebox.askyesno(
                "블로그와 동기화",
                "발행 완료로 표시된 글이 실제 블로그에 남아 있는지 확인합니다.\n"
                "블로그에서 직접 삭제한 글은 캘린더에서 '생성됨(재발행 가능)'으로 되돌립니다.\n\n계속할까요?"):
            return
        result = {}

        def job():
            result.update(core.sync_published_status(
                self.data, log=self.log_q.put, progress=self._progress_cb))

        def done():
            self.refresh_plan_lists()
            self.refresh_calendar()
            n = result.get("checked", 0)
            reverted = result.get("reverted", [])
            partial = result.get("partial", [])
            msg = f"확인 {n}편 · 되돌림 {len(reverted)}편"
            if partial:
                msg += f" · 한쪽 언어만 삭제됨 {len(partial)}편"
            self.log_q.put(f"\n🔗 동기화 완료 — {msg}\n")

        self._start_worker(job, "블로그와 동기화", on_done=done)

    def run_batch_publish(self):
        """발행 대기·예정 목록에서 선택한 여러 글을 날짜순으로 차례차례 생성·발행(간격 옵션)."""
        up = getattr(self, "plan_upcoming", None)
        sel = list(up.selection()) if up else []
        if not sel:
            messagebox.showinfo("선택 없음",
                                "왼쪽 '발행 대기·예정' 목록에서 글을 하나 이상 선택하세요.\n"
                                "(Ctrl 클릭=개별 추가, Shift 클릭=범위 선택)")
            return
        keys = sorted(sel, key=lambda k: core.post_date(k))
        gap_label = self.batch_gap_var.get()
        gap_min = {"즉시 연속": 0, "10분": 10, "30분": 30, "1시간": 60, "2시간": 120}.get(gap_label, 0)
        warn = ("\n\n⚠️ 한꺼번에 너무 많이 '발행'하면 SEO·광고에 좋지 않습니다. "
                "간격을 두고 하루 몇 개씩 권장.") if gap_min == 0 and len(keys) > 3 else ""
        if not messagebox.askyesno(
                "차례로 생성·발행",
                f"선택한 {len(keys)}개 글을 날짜순으로 차례차례 생성하고 발행합니다.\n"
                f"글 사이 간격: {gap_label}\n\n"
                "※ 진행 동안 프로그램을 켜 두세요. [■ 중단]으로 멈출 수 있습니다."
                f"{warn}\n\n계속할까요?"):
            return
        settings = self._collect_settings()
        self._batch_stop = False

        def job():
            import time as _t
            total = len(keys)
            ok = 0
            for i, key in enumerate(keys, 1):
                if getattr(self, "_batch_stop", False):
                    self.log_q.put("\n■ 중단됨 — 남은 글은 처리하지 않았습니다.\n")
                    break
                ds = core.post_date(key)
                self.selected = key
                self.log_q.put(f"\n{'='*50}\n▶ [{i}/{total}] {ds} 생성·발행\n{'='*50}\n")
                try:
                    core.publish_date(ds, settings, self.data, log=self.log_q.put,
                                      progress=self._progress_cb,
                                      stop_check=lambda: getattr(self, "_batch_stop", False))
                    ok += 1
                except Exception as ex:
                    if self._handle_quota_error(ex, f"{ds} 발행 중"):
                        self.log_q.put("   ■ 남은 글은 발행하지 않고 중단합니다.\n")
                        break        # 할당량 소진 — 다음 글도 100% 실패하므로 전체 중단
                    self.log_q.put(f"   ❌ {ds} 실패: {ex}\n")
                # 마지막이 아니면 간격 대기(5초 단위로 쪼개 중단에 반응)
                if i < total and gap_min > 0 and not getattr(self, "_batch_stop", False):
                    self.log_q.put(f"   ⏳ 다음 글까지 {gap_min}분 대기... (중단하려면 [■ 중단])\n")
                    for _ in range(gap_min * 12):
                        if getattr(self, "_batch_stop", False):
                            break
                        _t.sleep(5)
            self.log_q.put(f"\n✅ 차례 발행 종료 — 성공 {ok}/{total}\n")

        def done():
            self.refresh_plan_lists()

        self._start_worker(job, f"선택 {len(keys)}개 차례로 생성·발행 ({gap_label})", on_done=done)

    def stop_batch(self):
        self._batch_stop = True
        self._log("■ 중단 요청 — 현재 글을 마친 뒤 멈춥니다.\n")

    def run_static_pages(self):
        """애드센스 필수 페이지(개인정보처리방침·소개·문의)를 현재 블로그에 생성."""
        email = self.contactemail_var.get().strip()
        if not email and not messagebox.askyesno(
                "문의 이메일 없음",
                "설정의 '문의 이메일'이 비어 있어 문의·개인정보 페이지에 이메일이 빠집니다.\n"
                "그래도 만들까요? (나중에 Blogger에서 직접 수정 가능)"):
            return
        if not messagebox.askyesno(
                "필수 페이지 만들기",
                "현재 활성 블로그에 애드센스 필수 페이지 3개를 만듭니다:\n"
                "• Privacy Policy (개인정보처리방침)\n• About (소개)\n• Contact (문의)\n\n"
                "같은 제목이 이미 있으면 건너뜁니다. 만든 뒤 Blogger 레이아웃에서 "
                "상단 메뉴·사이드바에 링크로 노출하세요.\n\n계속할까요?"):
            return
        settings = self._collect_settings()

        def job():
            res = core.publish_static_pages(settings, log=self.log_q.put)
            urls = "\n".join(f"  · {t}: {u}" for t, u in (res or {}).items())
            self.log_q.put(f"\n📄 필수 페이지 완료:\n{urls}\n"
                           "→ Blogger [레이아웃]에서 상단 메뉴/사이드바에 링크로 추가하세요.\n")

        self._start_worker(job, "애드센스 필수 페이지 생성")

    # ── 이벤트(공연·전시) 탭 — k-culture-now 큐레이션용 ───────────────────────
    def _build_events_tab(self, parent):
        tk.Label(parent,
                 text="k-culture-now(시의성 큐레이션)용 공연·전시 이벤트 DB입니다.\n"
                      "공연API로 수집하거나 직접 추가하면, 시기별(D-30~당일)로 발행 후보가 자동 분류됩니다.",
                 font=("맑은 고딕", 11), fg="#333", justify="left").pack(anchor="w", padx=14, pady=(12, 6))

        bar = tk.Frame(parent); bar.pack(fill="x", padx=14, pady=(0, 6))
        tk.Button(bar, text="📥 공연API로 수집", command=self.run_collect_events,
                  bg="#0277bd", fg="white", font=("맑은 고딕", 10, "bold")).pack(side="left")
        tk.Button(bar, text="＋ 직접 추가", command=self.add_event_dialog).pack(side="left", padx=6)
        tk.Button(bar, text="🔄 새로고침", command=self.refresh_events_list).pack(side="left")
        tk.Button(bar, text="🗑 끝난 이벤트 정리", command=self.archive_events).pack(side="left", padx=6)
        tk.Button(bar, text="📅 시기별 발행 후보", command=self.show_due_summary,
                  bg="#6a1b9a", fg="white").pack(side="right")
        tk.Button(bar, text="✍ 큐레이션 글 생성", command=self.run_curation,
                  bg="#2e7d32", fg="white", font=("맑은 고딕", 10, "bold")).pack(side="right", padx=6)
        tk.Button(bar, text="🗓 오늘 트리거", command=self.run_trigger_today,
                  bg="#ef6c00", fg="white", font=("맑은 고딕", 10, "bold")).pack(side="right")

        cols = ("title", "type", "cat", "start", "end", "region", "price", "imp")
        heads = {"title": "제목", "type": "구분", "cat": "장르", "start": "시작",
                 "end": "종료", "region": "지역", "price": "요금", "imp": "중요도"}
        widths = {"title": 280, "type": 70, "cat": 70, "start": 90, "end": 90,
                  "region": 70, "price": 60, "imp": 50}
        tv = ttk.Treeview(parent, columns=cols, show="headings", height=16)
        for c in cols:
            tv.heading(c, text=heads[c])
            tv.column(c, width=widths[c], anchor=("w" if c == "title" else "center"))
        tv.pack(fill="both", expand=True, padx=14, pady=(0, 4))
        sb = ttk.Scrollbar(parent, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        self.events_tv = tv
        self.events_count_lbl = tk.Label(parent, text="", fg="#555", font=("맑은 고딕", 9))
        self.events_count_lbl.pack(anchor="w", padx=14, pady=(0, 8))
        self.refresh_events_list()

    def refresh_events_list(self):
        tv = getattr(self, "events_tv", None)
        if tv is None:
            return
        for i in tv.get_children():
            tv.delete(i)
        try:
            rows = evdb.all_events()
        except Exception as e:
            self._log(f"⚠️ 이벤트 목록 로드 실패: {e}\n")
            rows = []
        for e in rows:
            tv.insert("", "end", iid=e["id"], values=(
                e.get("title_ko", ""), e.get("type", ""), e.get("category", ""),
                e.get("start_date", ""), e.get("end_date", "") or "",
                e.get("region", "") or "", e.get("price", "") or "",
                e.get("importance", "") if e.get("importance") is not None else ""))
        self.events_count_lbl.config(text=f"총 {len(rows)}건 (끝난 이벤트 제외)")

    def run_collect_events(self):
        settings = self._collect_settings()
        if not settings.get("culture_api_key"):
            messagebox.showinfo(
                "공연API 키 필요",
                "[⚙️ 설정]의 '공연API 키'에 서비스키를 입력한 뒤 저장하세요.\n"
                "(data.go.kr / culture.go.kr에서 '문화체육관광부 문화예술공연' 무료 발급)")
            return

        def job():
            n = evcol.collect(settings, log=self.log_q.put)
            self._collected_n = n

        def done():
            self.refresh_events_list()
            messagebox.showinfo("수집 완료",
                                f"이벤트 {getattr(self, '_collected_n', 0)}건을 저장했습니다.")
        self._start_worker(job, "공연 이벤트 수집", on_done=done)

    def archive_events(self):
        try:
            n = evdb.archive_past_events()
        except Exception as e:
            messagebox.showwarning("정리 실패", str(e)); return
        self.refresh_events_list()
        self._log(f"🗑 끝난 이벤트 {n}건 정리(아카이브)\n")

    def show_due_summary(self):
        try:
            due = evdb.scan_all_due()
        except Exception as e:
            messagebox.showwarning("조회 실패", str(e)); return
        lines = ["오늘 기준 시기별 발행 후보:\n"]
        for cat in evdb.CATEGORY_KEYS:
            evs = due.get(cat, [])
            lines.append(f"● {evdb.CATEGORY_LABEL[cat]} — {len(evs)}건")
            for e in evs[:5]:
                lines.append(f"    · {e.get('title_ko','')} ({e.get('start_date','')})")
        messagebox.showinfo("시기별 발행 후보", "\n".join(lines))

    # ── 내 사진 라이브러리 탭 ────────────────────────────────────────────────
    def _build_photos_tab(self, parent):
        tk.Label(parent,
                 text="내가 찍은 사진을 한 번 등록해 두면 글 주제·장소로 자동 매칭해서 글에 넣습니다.\n"
                      "폴더의 모든 사진을 재귀 스캔해서 EXIF·폴더명·파일명을 키워드로 자동 등록.",
                 font=("맑은 고딕", 11), fg="#333", justify="left").pack(anchor="w", padx=14, pady=(12, 6))

        bar = tk.Frame(parent); bar.pack(fill="x", padx=14, pady=(0, 6))
        tk.Button(bar, text="📂 폴더 추가·스캔", command=self.scan_photo_folder,
                  bg="#0277bd", fg="white", font=("맑은 고딕", 10, "bold")).pack(side="left")
        tk.Button(bar, text="🔄 새로고침", command=self.refresh_photos_list).pack(side="left", padx=6)
        tk.Button(bar, text="🌐 GPS→지명 변환", command=self.enrich_photo_places,
                  bg="#6a1b9a", fg="white").pack(side="left", padx=(0, 6))
        tk.Button(bar, text="👁 AI 자동 태깅", command=self.auto_tag_photos,
                  bg="#00838f", fg="white").pack(side="left", padx=(0, 6))
        tk.Button(bar, text="💾 사진에 태그 쓰기", command=self.sync_photo_xmp,
                  bg="#4caf50", fg="white").pack(side="left", padx=(0, 6))
        tk.Button(bar, text="📝 촬영 위시리스트", command=self.open_wishlist_dialog,
                  bg="#e65100", fg="white", font=("맑은 고딕", 10, "bold")).pack(side="left", padx=(0, 6))
        tk.Button(bar, text="📥 반입 사진 반영", command=self.open_intake_dialog,
                  bg="#ad1457", fg="white", font=("맑은 고딕", 10, "bold")).pack(side="left", padx=(0, 6))
        tk.Button(bar, text="📤 스톡 업로드", command=self.open_stock_dialog,
                  bg="#455a64", fg="white", font=("맑은 고딕", 10, "bold")).pack(side="left", padx=(0, 6))
        self.photo_only_untagged = tk.BooleanVar(value=False)
        tk.Checkbutton(bar, text="태깅 안 된 것만",
                       variable=self.photo_only_untagged, font=("맑은 고딕", 9),
                       command=self.refresh_photos_list).pack(side="left", padx=(8, 0))
        tk.Label(bar, text="검색:", font=("맑은 고딕", 9)).pack(side="left", padx=(12, 4))
        self.photo_search_var = tk.StringVar()
        ent = tk.Entry(bar, textvariable=self.photo_search_var, width=24)
        ent.pack(side="left")
        ent.bind("<Return>", lambda e: self.refresh_photos_list())
        tk.Button(bar, text="🔍", command=self.refresh_photos_list).pack(side="left", padx=(2, 0))
        self.photo_stats_lbl = tk.Label(bar, text="", fg="#555", font=("맑은 고딕", 9))
        self.photo_stats_lbl.pack(side="right")

        # 폴더트리 | 사진목록 | 상세 — 경계선을 끌어서 폭 조절 가능(PanedWindow)
        body = ttk.PanedWindow(parent, orient="horizontal")
        body.pack(fill="both", expand=True, padx=14, pady=(0, 8))

        # 좌: 폴더 트리(선택한 폴더의 사진만 목록에 표시) — 가로·세로 스크롤 + 폭 드래그 조절
        tree_panel = tk.Frame(body, width=320, height=560)
        tree_panel.grid_propagate(False)   # 안의 트리 내용(가로 900px)이 패널을 늘리지 않게
        tk.Label(tree_panel, text="📁 폴더  (안 보이면 가로 스크롤바나 경계선을 끌어보세요)",
                font=("맑은 고딕", 9, "bold")).grid(row=0, column=0, columnspan=2, sticky="w")
        tree_panel.grid_rowconfigure(1, weight=1)
        tree_panel.grid_columnconfigure(0, weight=1)
        ftv = ttk.Treeview(tree_panel, show="tree", selectmode="browse")
        ftv.grid(row=1, column=0, sticky="nsew")
        fsb_v = ttk.Scrollbar(tree_panel, orient="vertical", command=ftv.yview)
        fsb_v.grid(row=1, column=1, sticky="ns")
        fsb_h = ttk.Scrollbar(tree_panel, orient="horizontal", command=ftv.xview)
        fsb_h.grid(row=2, column=0, sticky="ew")
        ftv.configure(yscrollcommand=fsb_v.set, xscrollcommand=fsb_h.set)
        ftv.column("#0", width=900, stretch=False)  # 깊은 폴더도 가로 스크롤로 끝까지 읽을 수 있게
        ftv.bind("<<TreeviewSelect>>", self.on_photo_folder_selected)
        ftv.bind("<<TreeviewOpen>>", self.on_photo_tree_open)
        body.add(tree_panel, weight=2)
        self.photo_tree = ftv
        self.photo_selected_folder = None
        self._photo_registered_dirs = set()

        # 중: 사진 목록
        left = tk.Frame(body)
        body.add(left, weight=3)

        # 선택한 사진 여러 장에 한꺼번에 적용하는 일괄 작업(Ctrl/Shift로 다중 선택)
        bulk_bar = tk.Frame(left); bulk_bar.pack(side="bottom", fill="x", pady=(6, 0))
        tk.Label(bulk_bar, text="선택한 사진들:", font=("맑은 고딕", 9)).pack(side="left")
        tk.Button(bulk_bar, text="🏷️ 태그 추가", command=self.bulk_add_tags
                 ).pack(side="left", padx=(6, 0))
        tk.Button(bulk_bar, text="🗑️ 태그 지우기", command=self.bulk_clear_tags,
                  bg="#b71c1c", fg="white").pack(side="left", padx=(6, 0))

        cols = ("ai", "file", "place", "region", "tags", "used")
        heads = {"ai": "✓", "file": "파일명", "place": "장소", "region": "지역",
                 "tags": "태그", "used": "쓰임"}
        widths = {"ai": 30, "file": 200, "place": 110, "region": 80, "tags": 200, "used": 50}
        tv = ttk.Treeview(left, columns=cols, show="headings", height=18, selectmode="extended")
        for c in cols:
            tv.heading(c, text=heads[c])
            tv.column(c, width=widths[c], anchor=("w" if c in ("file", "tags") else "center"))
        tv.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(left, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set); sb.pack(side="left", fill="y")
        tv.bind("<<TreeviewSelect>>", self._on_photo_selected)
        self.photos_tv = tv

        # 우: 상세(썸네일 + 태그 편집) — 경계선을 끌어 폭 조절 가능(PanedWindow)
        # 사진 표시 영역을 넓게: 너비 460px, 사진은 expand로 공간 가득
        right = tk.Frame(body, width=460)
        body.add(right, weight=2)

        # 사진 — 위쪽 절반을 가득 채우게(빈 패널·사진 모두)
        self.photo_detail_thumb = tk.Label(right, bg="#0e0e10",
                                            text="(사진 선택)", fg="#666",
                                            font=("맑은 고딕", 10))
        self.photo_detail_thumb.pack(fill="both", expand=True, pady=(0, 6))
        # 정보·태그는 사진 아래 고정 영역
        bottom = tk.Frame(right); bottom.pack(fill="x", side="bottom")
        tk.Label(bottom,
                 text="• '백운호수, 새벽, 안개' 처럼 구체적인 단어를 ','로 구분.\n"
                      "• 글 주제에 매칭되면 자동으로 글에 들어갑니다.",
                 fg="#888", font=("맑은 고딕", 8), wraplength=440, justify="left",
                 anchor="w").pack(fill="x", pady=(4, 0), side="bottom")
        tk.Button(bottom, text="💾 태그 저장", command=self.save_photo_tags,
                  bg="#2e7d32", fg="white", font=("맑은 고딕", 10, "bold")
                  ).pack(fill="x", side="bottom", pady=(4, 0))
        self.photo_tags_text = tk.Text(bottom, height=3, font=("맑은 고딕", 10), wrap="word")
        self.photo_tags_text.pack(fill="x", side="bottom", pady=(2, 0))
        tk.Label(bottom, text="태그(쉼표 구분):", font=("맑은 고딕", 9)
                 ).pack(anchor="w", side="bottom", pady=(6, 0))
        self.photo_detail_info = tk.Label(bottom, text="", anchor="w", justify="left",
                                           font=("맑은 고딕", 9), fg="#999", wraplength=440)
        self.photo_detail_info.pack(fill="x", side="bottom", pady=(6, 0))

        self._photo_rows = {}        # iid → dict(row)
        self._photo_thumb_ref = None  # GC 방지
        self.populate_folder_tree()
        self.refresh_photos_list()

        # 초기 분할 비율 명시(가중치만으로는 트리 칸이 너무 좁게 잡혀서 직접 지정).
        # [내 사진] 탭은 처음엔 숨겨진 상태라 폭을 알 수 없으므로, 탭이 실제로 화면에
        # 보이는(Map) 첫 순간에 한 번만 설정 — 그 다음엔 사용자가 끌어도 안 건드림.
        self._photo_sash_set = False

        def _set_sash(event=None):
            if self._photo_sash_set:
                return
            self._photo_sash_set = True
            try:
                body.sashpos(0, 320)
                body.sashpos(1, 320 + 640)
            except Exception:
                pass
        body.bind("<Map>", _set_sash)

    # ── 촬영 위시리스트 (사진 완성도 워크플로우 1단계) ──────────────────────
    def open_wishlist_dialog(self):
        """세 블로그 발행글을 스캔해 '직접 찍어 채우면 좋은 사진' 목록을 보여준다.
        스톡·검색 사진 글과 사진 없는 글에서 구체적 소재를 뽑아 권장 파일명까지 제시."""
        win = tk.Toplevel(self.root)
        win.title("📝 촬영 위시리스트 — 직접 찍어 채울 사진 목록")
        win.geometry("980x620")
        self._wishlist_win = win

        top = tk.Label(win, justify="left", fg="#333", font=("맑은 고딕", 10),
                       text="무료 이미지·관광 공공데이터로 못 찾는 소재를 직접 찍어 채우기 위한 목록입니다.\n"
                            "권장 파일명으로 저장하면 나중에 자동으로 해당 글에 반영됩니다.")
        top.pack(anchor="w", padx=14, pady=(12, 6))

        bar = tk.Frame(win); bar.pack(fill="x", padx=14, pady=(0, 6))
        tk.Button(bar, text="🔍 위시리스트 생성·갱신", command=self._wishlist_build,
                  bg="#e65100", fg="white", font=("맑은 고딕", 10, "bold")).pack(side="left")
        tk.Button(bar, text="📄 마크다운 저장", command=self._wishlist_export_md).pack(side="left", padx=6)
        tk.Button(bar, text="📋 클립보드 복사", command=self._wishlist_copy).pack(side="left")
        self.wishlist_stats_lbl = tk.Label(bar, text="", fg="#555", font=("맑은 고딕", 9))
        self.wishlist_stats_lbl.pack(side="right")

        cols = ("blog", "topic", "slot", "heading", "guide", "filename")
        heads = {"blog": "블로그", "topic": "글 주제", "slot": "구분",
                 "heading": "찍을 소재", "guide": "촬영 가이드", "filename": "권장 파일명"}
        widths = {"blog": 60, "topic": 200, "slot": 60, "heading": 200,
                  "guide": 220, "filename": 200}
        frame = tk.Frame(win); frame.pack(fill="both", expand=True, padx=14, pady=(0, 12))
        tv = ttk.Treeview(frame, columns=cols, show="headings", selectmode="browse")
        for c in cols:
            tv.heading(c, text=heads[c])
            tv.column(c, width=widths[c], anchor="w")
        vsb = ttk.Scrollbar(frame, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=vsb.set)
        tv.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")
        self.wishlist_tree = tv
        self._wishlist_refresh_list()

    def _wishlist_refresh_list(self):
        tv = getattr(self, "wishlist_tree", None)
        if tv is None or not tv.winfo_exists():
            return
        for i in tv.get_children():
            tv.delete(i)
        items = [it for it in wishlist.load_wishlist() if it.get("status") == "needed"]
        for it in items:
            tv.insert("", "end", values=(
                it.get("blog", ""), it.get("topic", "")[:40], it.get("slot", ""),
                it.get("heading", ""), it.get("guide", "")[:60],
                it.get("recommended_filename", "")))
        s = wishlist.wishlist_summary()
        self.wishlist_stats_lbl.config(
            text=f"필요 {s['needed']} · 찍음 {s['shot']} · 반영됨 {s['done']} / 전체 {s['total']}")

    def _wishlist_build(self):
        if not messagebox.askyesno(
                "위시리스트 생성",
                "세 블로그의 발행글을 스캔해 '직접 찍어 채울 사진' 목록을 만듭니다.\n"
                "글마다 AI로 소재를 분석하므로 발행글이 많으면 몇 분 걸릴 수 있어요.\n"
                "(로컬 AI만 사용 — 발행 쿼터와 무관)\n\n계속할까요?"):
            return
        settings = self._collect_settings()

        def job():
            wishlist.build_wishlist(settings=settings, log=self.log_q.put,
                                    on_progress=lambda p, m: self._progress_cb(p, m))

        def done():
            self._wishlist_refresh_list()

        self._start_worker(job, "촬영 위시리스트 생성", on_done=done)

    def _wishlist_export_md(self):
        md = wishlist.export_wishlist_markdown()
        if not md.strip() or "##" not in md:
            messagebox.showinfo("위시리스트 없음",
                                "먼저 [🔍 위시리스트 생성·갱신]을 눌러 목록을 만들어 주세요.")
            return
        path = filedialog.asksaveasfilename(
            parent=getattr(self, "_wishlist_win", self.root),
            title="촬영 위시리스트 저장", defaultextension=".md",
            initialfile="촬영_위시리스트.md",
            filetypes=[("Markdown", "*.md"), ("모든 파일", "*.*")])
        if not path:
            return
        Path(path).write_text(md, encoding="utf-8")
        messagebox.showinfo("저장 완료", f"위시리스트를 저장했습니다.\n{path}")

    def _wishlist_copy(self):
        md = wishlist.export_wishlist_markdown()
        if not md.strip() or "##" not in md:
            messagebox.showinfo("위시리스트 없음",
                                "먼저 [🔍 위시리스트 생성·갱신]을 눌러 목록을 만들어 주세요.")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(md)
        messagebox.showinfo("복사됨", "위시리스트를 클립보드에 복사했습니다.\n"
                                     "구글 문서·메모 등에 붙여넣어 휴대폰에서 보세요.")

    # ── 반입 사진 반영 (사진 완성도 워크플로우 2단계) ────────────────────────
    def open_intake_dialog(self):
        """반입 폴더의 사진을 위시 항목과 매칭해 보여주고, 확인한 것만 실제 글에 반영."""
        win = tk.Toplevel(self.root)
        win.title("📥 반입 사진 반영 — 찍은 사진을 글에 자동 반영")
        win.geometry("1040x640")
        self._intake_win = win
        self._intake_matches = {}      # iid → match dict

        settings = self._collect_settings()
        folder = intake.intake_dir(settings)
        tk.Label(win, justify="left", fg="#333", font=("맑은 고딕", 10),
                 text=f"반입 폴더: {folder}\n"
                      "이 폴더에 위시리스트 권장 파일명(또는 주제·소재가 담긴 이름)으로 사진을 저장한 뒤 "
                      "[스캔]을 누르세요.\n매칭된 사진을 선택(Ctrl/Shift로 여러 개)하고 [반영]을 누르면 "
                      "같은 URL로 글에 반영됩니다.").pack(anchor="w", padx=14, pady=(12, 6))

        bar = tk.Frame(win); bar.pack(fill="x", padx=14, pady=(0, 6))
        tk.Button(bar, text="🔍 반입 폴더 스캔", command=self._intake_scan,
                  bg="#ad1457", fg="white", font=("맑은 고딕", 10, "bold")).pack(side="left")
        self.intake_vision_var = tk.BooleanVar(value=True)
        tk.Checkbutton(bar, text="파일명으로 못 찾으면 AI로 사진 내용 분석",
                       variable=self.intake_vision_var,
                       font=("맑은 고딕", 9)).pack(side="left", padx=(10, 0))
        tk.Button(bar, text="✅ 선택한 사진 반영", command=self._intake_apply,
                  bg="#2e7d32", fg="white", font=("맑은 고딕", 10, "bold")).pack(side="left", padx=(12, 0))
        self.intake_stats_lbl = tk.Label(bar, text="", fg="#555", font=("맑은 고딕", 9))
        self.intake_stats_lbl.pack(side="right")

        cols = ("photo", "conf", "blog", "topic", "heading", "reason")
        heads = {"photo": "반입 사진", "conf": "신뢰도", "blog": "블로그",
                 "topic": "글", "heading": "매칭된 소재", "reason": "근거"}
        widths = {"photo": 210, "conf": 64, "blog": 56, "topic": 190,
                  "heading": 210, "reason": 200}
        frame = tk.Frame(win); frame.pack(fill="both", expand=True, padx=14, pady=(0, 12))
        tv = ttk.Treeview(frame, columns=cols, show="headings", selectmode="extended")
        for c in cols:
            tv.heading(c, text=heads[c])
            tv.column(c, width=widths[c], anchor="w")
        tv.tag_configure("nomatch", foreground="#999")
        tv.tag_configure("exact", foreground="#1b5e20")
        vsb = ttk.Scrollbar(frame, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=vsb.set)
        tv.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")
        self.intake_tree = tv

    def _intake_scan(self):
        settings = self._collect_settings()
        use_vision = bool(self.intake_vision_var.get())

        def job():
            res = intake.scan_intake(settings, use_vision=use_vision,
                                     log=self.log_q.put,
                                     on_progress=lambda p, m: self._progress_cb(p, m))
            self._intake_scan_result = res

        def done():
            self._intake_fill(getattr(self, "_intake_scan_result", []))

        self._start_worker(job, "반입 폴더 스캔·매칭", on_done=done)

    def _intake_fill(self, results):
        tv = getattr(self, "intake_tree", None)
        if tv is None or not tv.winfo_exists():
            return
        for i in tv.get_children():
            tv.delete(i)
        self._intake_matches = {}
        matched_iids = []
        for r in results:
            conf = r.get("confidence")
            label = {"exact": "정확", "filename": "파일명", "vision": "AI분석"}.get(conf, "미매칭")
            tag = "exact" if conf == "exact" else ("nomatch" if not conf else "")
            iid = tv.insert("", "end", values=(
                r["photo_name"][:40], label, r.get("blog", ""),
                (r.get("topic") or "")[:36], (r.get("heading") or "")[:40],
                (r.get("reason") or "")[:40]), tags=(tag,) if tag else ())
            self._intake_matches[iid] = r
            if conf:
                matched_iids.append(iid)
        if matched_iids:
            tv.selection_set(matched_iids)     # 매칭된 것 기본 선택
        n_match = len(matched_iids)
        self.intake_stats_lbl.config(
            text=f"반입 {len(results)}장 · 매칭 {n_match}장 (선택한 것만 반영됩니다)")

    def _intake_apply(self):
        tv = getattr(self, "intake_tree", None)
        if tv is None:
            return
        sel = tv.selection()
        chosen = [self._intake_matches[i] for i in sel
                  if self._intake_matches.get(i, {}).get("confidence")]
        if not chosen:
            messagebox.showinfo("선택 없음",
                                "반영할 '매칭된' 사진을 목록에서 선택하세요(미매칭 행은 반영 불가).")
            return
        posts = len({(m["item"].get("blog_id"), m["item"].get("url")) for m in chosen})
        if not messagebox.askyesno(
                "반입 사진 반영",
                f"선택한 사진 {len(chosen)}장을 글 {posts}개에 반영합니다.\n"
                "각 글은 같은 URL을 유지한 채 사진이 추가·재배치되고, 위시리스트에서 '반영됨'으로 "
                "표시됩니다. 원본 사진은 반입 폴더의 _반영완료로 옮겨집니다.\n\n계속할까요?"):
            return
        settings = self._collect_settings()

        def job():
            res = intake.apply_matches(chosen, settings, log=self.log_q.put,
                                       on_progress=lambda p, m: self._progress_cb(p, m))
            self.log_q.put(f"\n📥 반영 완료 — 글 {res['posts']}개 · 사진 {res['photos']}장"
                           + (f" · 실패 {len(res['errors'])}" if res["errors"] else "") + "\n")
            for name, why in res.get("errors", []):
                self.log_q.put(f"   ❌ {name}: {why}\n")

        def done():
            self._intake_scan()                # 반영 후 재스캔(반영된 것 빠짐)
            if hasattr(self, "_wishlist_refresh_list"):
                try:
                    self._wishlist_refresh_list()
                except Exception:
                    pass

        self._start_worker(job, "반입 사진 글에 반영", on_done=done)

    # ── 스톡 업로드 (사진 완성도 워크플로우 3단계) ──────────────────────────
    def open_stock_dialog(self):
        """반영 완료 사진의 스톡 판매용 메타데이터를 만들고, 등록 사이트에 자동 입력(제출은 직접)."""
        win = tk.Toplevel(self.root)
        win.title("📤 스톡 업로드 — 촬영본을 스톡 사이트에 올리기")
        win.geometry("1060x640")
        self._stock_win = win

        tk.Label(win, justify="left", fg="#333", font=("맑은 고딕", 10),
                 text="반영 완료된 촬영본에 판매용 영문 제목·키워드를 만들어 사진 파일에 XMP 태그로 새깁니다.\n"
                      "Unsplash·Pexels는 업로더를 열고 파일을 자동 선택하며, 사이트가 XMP 태그를 자동으로 읽습니다"
                      "(크라우드픽은 심사형이라 페이지만 열어 드리니 직접 올려주세요).\n"
                      "⚠️ 외부 사이트 공개 게시라, 파일·태그 준비까지만 하고 최종 '제출'은 직접 확인해 누르세요.")\
            .pack(anchor="w", padx=14, pady=(12, 4))

        bar = tk.Frame(win); bar.pack(fill="x", padx=14, pady=(0, 4))
        tk.Button(bar, text="📝 반영 사진 메타 생성·큐 담기", command=self._stock_enqueue,
                  bg="#455a64", fg="white", font=("맑은 고딕", 10, "bold")).pack(side="left")
        tk.Button(bar, text="🔄 새로고침", command=self._stock_refresh).pack(side="left", padx=6)
        self.stock_stats_lbl = tk.Label(bar, text="", fg="#555", font=("맑은 고딕", 9))
        self.stock_stats_lbl.pack(side="right")

        sbar = tk.Frame(win); sbar.pack(fill="x", padx=14, pady=(0, 6))
        tk.Label(sbar, text="올릴 사이트:", font=("맑은 고딕", 9)).pack(side="left")
        self.stock_site_vars = {}
        for s in stock.SITES:
            v = tk.BooleanVar(value=True)
            self.stock_site_vars[s] = v
            tk.Checkbutton(sbar, text=s, variable=v, font=("맑은 고딕", 9)).pack(side="left", padx=(6, 0))
        tk.Button(sbar, text="🌐 업로더 열기·파일 자동선택(제출은 직접)", command=self._stock_fill,
                  bg="#00695c", fg="white", font=("맑은 고딕", 10, "bold")).pack(side="left", padx=(14, 0))

        cols = ("photo", "topic", "title", "kw", "크라우드픽", "Unsplash", "Pexels")
        heads = {"photo": "사진", "topic": "글", "title": "영문 제목", "kw": "키워드",
                 "크라우드픽": "크라우드픽", "Unsplash": "Unsplash", "Pexels": "Pexels"}
        widths = {"photo": 200, "topic": 160, "title": 220, "kw": 50,
                  "크라우드픽": 80, "Unsplash": 80, "Pexels": 70}
        frame = tk.Frame(win); frame.pack(fill="both", expand=True, padx=14, pady=(0, 12))
        tv = ttk.Treeview(frame, columns=cols, show="headings", selectmode="extended")
        for c in cols:
            tv.heading(c, text=heads[c])
            tv.column(c, width=widths[c], anchor="w")
        vsb = ttk.Scrollbar(frame, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=vsb.set)
        tv.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")
        self.stock_tree = tv
        self._stock_rows = {}
        self._stock_refresh()

    def _stock_refresh(self):
        tv = getattr(self, "stock_tree", None)
        if tv is None or not tv.winfo_exists():
            return
        for i in tv.get_children():
            tv.delete(i)
        self._stock_rows = {}
        q = stock.load_queue()
        for it in q:
            st = it.get("sites", {})
            def m(s):
                return {"pending": "대기", "uploaded": "완료", "failed": "실패",
                        "skipped": "건너뜀"}.get(st.get(s, ""), "-")
            iid = tv.insert("", "end", values=(
                it.get("photo_name", "")[:38], (it.get("topic") or "")[:28],
                (it.get("title_en") or "")[:40], len(it.get("keywords_en") or []),
                m("크라우드픽"), m("Unsplash"), m("Pexels")))
            self._stock_rows[iid] = it
        s = stock.queue_summary()
        ps = s["per_site"]
        self.stock_stats_lbl.config(
            text=f"큐 {s['total']}장 · 대기 크라우드픽 {ps['크라우드픽']['pending']}"
                 f"/Unsplash {ps['Unsplash']['pending']}/Pexels {ps['Pexels']['pending']}")

    def _stock_enqueue(self):
        settings = self._collect_settings()

        def job():
            n = stock.enqueue_applied_photos(settings, log=self.log_q.put,
                                             on_progress=lambda p, m: self._progress_cb(p, m))
            self.log_q.put(f"\n📤 업로드 큐에 {n}장 추가(메타 생성·XMP 태깅 완료)\n")

        self._start_worker(job, "스톡 메타 생성·큐 담기", on_done=self._stock_refresh)

    def _stock_fill(self):
        tv = getattr(self, "stock_tree", None)
        if tv is None:
            return
        rows = [self._stock_rows[i] for i in tv.selection() if i in self._stock_rows]
        if not rows:
            messagebox.showinfo("선택 없음", "자동 입력할 사진을 목록에서 선택하세요.")
            return
        sites = [s for s, v in self.stock_site_vars.items() if v.get()]
        if not sites:
            messagebox.showinfo("사이트 없음", "올릴 사이트를 1개 이상 선택하세요.")
            return
        if not messagebox.askyesno(
                "스톡 업로더 열기",
                f"사진 {len(rows)}장 × 사이트 {len(sites)}곳({', '.join(sites)})의 업로더를 순서대로 엽니다.\n"
                "각 창에서 파일이 자동 선택되고, 사이트가 파일의 XMP 키워드를 자동으로 읽습니다.\n"
                "처음이면 로그인해 주세요. ⚠️ 최종 '제출'은 직접 확인해 누르셔야 합니다.\n\n계속할까요?"):
            return

        def job():
            import stock_sites
            for it in rows:
                for site in sites:
                    if it.get("sites", {}).get(site) == "uploaded":
                        continue
                    self.log_q.put(f"\n▶ [{site}] {it.get('photo_name','')}\n")
                    try:
                        r = stock_sites.fill_upload(site, it, submit=False,
                                                    log=self.log_q.put)
                        status = "uploaded" if r.get("submitted") else "pending"
                        stock.set_status(it["photo_path"], site, status)
                        self.log_q.put(f"   {r.get('note','')}\n")
                    except Exception as ex:
                        stock.set_status(it["photo_path"], site, "failed")
                        self.log_q.put(f"   ❌ {site} 실패: {ex}\n")

        self._start_worker(job, "스톡 사이트 자동 입력", on_done=self._stock_refresh)

    def populate_folder_tree(self):
        """좌측 트리 = 컴퓨터의 실제 폴더 구조(탐색기처럼). 스캔 안 한 폴더도 보이고,
        펼치기 전까지는 하위 폴더를 읽지 않음(느려지지 않게, 클릭할 때만 읽음)."""
        tv = getattr(self, "photo_tree", None)
        if tv is None:
            return
        prev = tv.selection()
        prev_sel = prev[0] if prev else "__ALL__"
        for i in tv.get_children():
            tv.delete(i)
        try:
            self._photo_registered_dirs = photolib.registered_dirs()
        except Exception:
            self._photo_registered_dirs = set()
        tv.insert("", "end", iid="__ALL__", text="📁 전체 사진(라이브러리)", open=True)
        tv.insert("", "end", iid="__BROWSE__", text="💻 폴더 찾아보기", open=True)
        for d in string.ascii_uppercase:
            drive = f"{d}:\\"
            if os.path.exists(drive):
                self._insert_dir_node("__BROWSE__", drive, f"{d}:")
        if tv.exists(prev_sel):
            tv.selection_set(prev_sel)
        else:
            tv.selection_set("__ALL__")

    def _dir_label(self, full_path: str, name: str, own: int) -> str:
        mark = "✅ " if full_path.lower().rstrip("\\/") in self._photo_registered_dirs else ""
        suffix = f"  ({own}장)" if own else ""
        return f"{mark}{name}{suffix}"

    def _insert_dir_node(self, parent_iid, full_path: str, label_name: str):
        tv = self.photo_tree
        if tv.exists(full_path):
            return
        own = 0
        try:
            with os.scandir(full_path) as it:
                own = sum(1 for e in it if e.is_file()
                         and Path(e.name).suffix.lower() in photolib.PHOTO_EXTS)
        except Exception:
            pass
        tv.insert(parent_iid, "end", iid=full_path,
                 text=self._dir_label(full_path, label_name, own), open=False)
        tv.insert(full_path, "end", iid=full_path + "\x00dummy", text="")  # 펼치기 화살표용

    def on_photo_tree_open(self, event=None):
        """폴더를 펼칠 때만 그 안의 하위 폴더를 디스크에서 읽음(지연 로딩)."""
        tv = self.photo_tree
        iid = tv.focus()
        children = tv.get_children(iid)
        if len(children) != 1 or not children[0].endswith("\x00dummy"):
            return
        tv.delete(children[0])
        try:
            subs = photolib.list_subdirs(iid)
        except Exception as e:
            self.log_q.put(f"⚠️ 폴더 읽기 실패: {e}\n")
            subs = []
        for sd in subs:
            cid = sd["path"]
            if tv.exists(cid):
                continue
            tv.insert(iid, "end", iid=cid, text=self._dir_label(cid, sd["name"], sd["own"]),
                     open=False)
            if sd.get("has_sub"):
                tv.insert(cid, "end", iid=cid + "\x00dummy", text="")

    def on_photo_folder_selected(self, event=None):
        sel = self.photo_tree.selection()
        iid = sel[0] if sel else "__ALL__"
        if iid == "__BROWSE__" or iid.endswith("\x00dummy"):
            return
        self.photo_selected_folder = None if iid == "__ALL__" else iid
        self.refresh_photos_list()

    def scan_photo_folder(self):
        d = filedialog.askdirectory(title="사진 폴더 선택(하위 폴더 포함 스캔)")
        if not d:
            return

        def job():
            self._scan_result = photolib.scan_folder(d, log=self.log_q.put)

        def done():
            res = getattr(self, "_scan_result", {}) or {}
            self.populate_folder_tree()
            self.refresh_photos_list()
            messagebox.showinfo("스캔 완료",
                                f"신규 {res.get('added',0)} / 갱신 {res.get('updated',0)} "
                                f"/ 실패 {res.get('skipped',0)}")
        self._start_worker(job, f"사진 폴더 스캔: {Path(d).name}", on_done=done)

    def enrich_photo_places(self):
        st = photolib.stats()
        if not st.get("with_gps"):
            messagebox.showinfo("GPS 없음",
                                "GPS가 있는 사진이 없습니다. EXIF GPS가 포함된 사진을 스캔하세요.")
            return
        if not messagebox.askyesno(
                "GPS→지명 변환",
                f"GPS가 있는 사진들을 OSM Nominatim에 조회해 '의왕시 학의동' 같은 정확한 지명으로 갱신합니다.\n"
                "(약관 준수를 위해 1초당 1건씩 — 100장이면 약 2분)\n\n계속할까요?"):
            return

        def job():
            self._enrich = photolib.enrich_places(limit=200, rate=1.1, log=self.log_q.put)

        def done():
            r = getattr(self, "_enrich", {}) or {}
            self.refresh_photos_list()
            messagebox.showinfo("완료",
                                f"갱신 {r.get('updated',0)} / 실패 {r.get('failed',0)} / "
                                f"대상 {r.get('total',0)}")
        self._start_worker(job, "GPS→지명 변환", on_done=done)

    def auto_tag_photos(self):
        settings = self._collect_settings()
        if not photo_vision.is_available(settings):
            messagebox.showinfo(
                "비전 모델 필요",
                "사진 내용을 보고 태그를 만드는 비전 모델이 아직 설치되지 않았어요.\n\n"
                "한 번만 설치하면 됩니다(약 6GB, 5~10분):\n"
                "  1) 터미널(cmd) 열기\n"
                "  2) 다음 명령 실행:\n     ollama pull qwen2.5vl:7b\n"
                "  3) 완료되면 이 버튼을 다시 누르세요.")
            return
        st = photolib.stats()
        n = st.get("total", 0)
        if not n:
            messagebox.showinfo("사진 없음", "먼저 [📂 폴더 추가·스캔]으로 사진을 등록하세요.")
            return
        todo = photolib.count_untagged()
        if not todo:
            messagebox.showinfo("태깅 완료", "태깅할 사진이 없습니다(모두 처리됨).")
            return
        est_min = max(1, round(todo * 8 / 60))
        if not messagebox.askyesno(
                "AI 자동 태깅",
                f"태그가 비어 있는 사진 {todo}장을 AI가 한 장씩 보고 태그를 만듭니다.\n"
                f"(한 장당 약 5~15초, 전체 약 {est_min}분 예상)\n\n진행할까요?"):
            return

        def job():
            self._tag_res = photo_vision.auto_tag(
                settings, limit=todo, only_untagged=True, log=self.log_q.put)

        def done():
            r = getattr(self, "_tag_res", {}) or {}
            self.refresh_photos_list()
            messagebox.showinfo(
                "자동 태깅 완료",
                f"성공 {r.get('updated',0)} / 실패 {r.get('failed',0)} / 대상 {r.get('total',0)}")
        self._start_worker(job, "AI 자동 태깅", on_done=done)

    def sync_photo_xmp(self):
        """DB의 태그를 사진 파일에 표준 XMP/IPTC로 저장(영구 보존)."""
        if not messagebox.askyesno(
                "사진 파일에 태그 쓰기",
                "DB의 태그·캡션을 사진 파일에 표준 메타데이터(XMP/IPTC)로 저장합니다.\n\n"
                "• Lightroom·Adobe Bridge·Windows·구글 포토에서 그대로 인식\n"
                "• 사진 파일을 옮겨도 태그가 따라다님\n"
                "• 처음 한 번은 ExifTool 자동 설치 시도(약 6MB, 안 되면 안내 메시지로 직접 설치)\n"
                "• 사진 파일이 수정되므로 중요한 사진은 백업 권장\n\n계속할까요?"):
            return

        def job():
            self._xmp_res = photo_xmp.sync_library(only_unsynced=True, limit=500,
                                                    log=self.log_q.put)

        def done():
            r = getattr(self, "_xmp_res", {}) or {}
            self.refresh_photos_list()
            messagebox.showinfo("XMP 동기화 완료",
                                f"성공 {r.get('ok',0)} / 실패 {r.get('fail',0)} / "
                                f"대상 {r.get('total',0)}\n\n"
                                "이제 목록 ✓ 표시 = 사진 파일에도 태그 저장됨.")
        self._start_worker(job, "사진에 태그 쓰기(XMP)", on_done=done)

    def refresh_photos_list(self):
        tv = getattr(self, "photos_tv", None)
        if tv is None:
            return
        for i in tv.get_children():
            tv.delete(i)
        self._photo_rows.clear()
        q = (self.photo_search_var.get() or "").strip()
        only_untagged = bool(getattr(self, "photo_only_untagged",
                                      tk.BooleanVar(value=False)).get())
        folder_filter = getattr(self, "photo_selected_folder", None)
        rows = []
        truncated = False
        try:
            photolib.init_db()
            if folder_filter:
                # 폴더 트리에서 실제 폴더 선택 — 등록(스캔) 여부와 무관하게
                # 디스크에 실제로 있는 사진을 그대로 보여줌(등록된 건 태그도 같이 표시)
                files = photolib.list_image_files(folder_filter, recursive=True, limit=2000)
                truncated = len(files) >= 2000
                db_map = photolib.photos_by_paths(files)
                for f in files:
                    r = db_map.get(f.lower()) or {
                        "id": None, "path": f, "filename": Path(f).name,
                        "place": "", "region": "", "user_tags": "", "auto_caption": "",
                        "xmp_synced_at": "", "use_count": 0, "folder_tags": "",
                        "taken_at": None, "width": None, "height": None,
                        "gps_lat": None, "gps_lng": None,
                    }
                    rows.append(r)
                if q:
                    ql = q.lower()
                    rows = [r for r in rows if ql in (r.get("filename") or "").lower()
                           or ql in (r.get("user_tags") or "").lower()
                           or ql in (r.get("auto_caption") or "").lower()]
            elif q:
                rows = photolib.search(q, n=500)
            else:
                from contextlib import closing
                with closing(photolib._conn()) as c:
                    rows = [dict(r) for r in c.execute(
                        "SELECT * FROM photos ORDER BY added_at DESC LIMIT 500").fetchall()]
            if only_untagged:               # 처리 완료된 건 숨김(태그 비어 있는 것만)
                rows = [r for r in rows if not (r.get("user_tags") or "").strip()]
        except Exception as e:
            try: self.log_q.put(f"⚠️ 사진 목록 로드 실패: {e}\n")
            except Exception: print(f"사진 목록 로드 실패: {e}")
        for r in rows:
            iid = r.get("path") or str(r.get("id"))
            # AI 컬럼: 사진 파일에 저장됨=✓ / 태그만 있고 미저장=· / AI 인식 실패=✗ / 아예 없음(미등록 포함)=빈칸
            if (r.get("xmp_synced_at") or "").strip():
                ai_mark = "✓"
            elif (r.get("user_tags") or "").strip():
                ai_mark = "·"
            elif (r.get("auto_caption") or "").strip():
                ai_mark = "✗"
            else:
                ai_mark = ""
            tv.insert("", "end", iid=iid, values=(
                ai_mark,
                Path(r.get("path", "")).name, r.get("place", "") or "",
                r.get("region", "") or "", (r.get("user_tags") or "")[:40],
                r.get("use_count", 0)))
            self._photo_rows[iid] = r
        st = photolib.stats()
        where = f"📂 {folder_filter}" if folder_filter else "📁 전체 라이브러리"
        extra = "  ⚠️ 2000장 넘어 일부만 표시" if truncated else ""
        self.photo_stats_lbl.config(
            text=f"{where} · 표시 {len(rows)}{extra} / 라이브러리 전체 {st['total']} "
                 f"· GPS {st['with_gps']} · 태그 {st['with_user_tags']}")

    def _on_photo_selected(self, _evt=None):
        sel = self.photos_tv.selection()
        if not sel:
            return
        r = self._photo_rows.get(sel[0]) or {}
        info = []
        info.append(f"📁 {r.get('path','')}")
        if r.get("taken_at"): info.append(f"📅 촬영: {r['taken_at']}")
        if r.get("width"):    info.append(f"📐 {r['width']}×{r['height']}")
        if r.get("gps_lat"):  info.append(f"🌐 GPS: {r['gps_lat']:.5f}, {r['gps_lng']:.5f}")
        if r.get("folder_tags"): info.append(f"📂 {r['folder_tags']}")
        self.photo_detail_info.config(text="\n".join(info))
        self.photo_tags_text.delete("1.0", "end")
        self.photo_tags_text.insert("1.0", r.get("user_tags") or "")

        # 썸네일 — 라벨 실제 크기에 맞춰 크게(빈 공간 가득)
        path = r.get("path", "")
        if HAVE_PIL and path and Path(path).exists():
            try:
                self.photo_detail_thumb.update_idletasks()
                w = max(360, self.photo_detail_thumb.winfo_width() - 8)
                h = max(280, self.photo_detail_thumb.winfo_height() - 8)
                im = Image.open(path)
                from PIL import ImageOps
                im = ImageOps.exif_transpose(im)
                im.thumbnail((w, h))
                ph = ImageTk.PhotoImage(im)
                self.photo_detail_thumb.config(image=ph, text="")
                self._photo_thumb_ref = ph
            except Exception as e:
                self.photo_detail_thumb.config(image="", text=f"썸네일 오류: {e}")
        else:
            self.photo_detail_thumb.config(image="", text="(미리보기 불가)")

    def save_photo_tags(self):
        sel = self.photos_tv.selection()
        if not sel:
            messagebox.showinfo("태그", "사진을 먼저 선택하세요.")
            return
        iid = sel[0]
        row = self._photo_rows.get(iid, {})
        tags = self.photo_tags_text.get("1.0", "end").strip()
        try:
            pid = row.get("id")
            if pid is None:                  # 아직 스캔 안 된 폴더에서 직접 태그 입력 → 즉석 등록
                pid = photolib.register_file(row.get("path") or iid)
            photolib.add_user_tags(pid, tags)
        except Exception as e:
            messagebox.showwarning("저장 실패", str(e)); return
        # 목록 셀 갱신 — 컬럼: ai/file/place/region/tags/used
        cur = self.photos_tv.item(iid, "values")
        new_ai = "·" if tags.strip() else ""    # 태그 바뀌면 미저장 상태로 표시
        self.photos_tv.item(iid, values=(new_ai, cur[1], cur[2], cur[3], tags[:40], cur[5]))
        if iid in self._photo_rows:
            self._photo_rows[iid]["user_tags"] = tags
            self._photo_rows[iid]["xmp_synced_at"] = None
            self._photo_rows[iid]["id"] = pid
        self._log(f"🏷️ 사진 태그 저장: {tags[:50]}\n")

    def bulk_add_tags(self):
        """선택한 여러 사진에 태그를 한꺼번에 추가(기존 태그는 보존, 중복 제거)."""
        sel = self.photos_tv.selection()
        if not sel:
            messagebox.showinfo("태그 추가", "사진을 먼저 선택하세요(Ctrl/Shift로 여러 장 선택 가능).")
            return
        add_str = simpledialog.askstring(
            "태그 추가", f"선택한 {len(sel)}장에 추가할 태그를 입력하세요(쉼표로 구분):")
        if not add_str or not add_str.strip():
            return
        new_tags = [t.strip() for t in add_str.split(",") if t.strip()]
        ok = fail = 0
        for iid in sel:
            row = self._photo_rows.get(iid, {})
            try:
                pid = row.get("id")
                if pid is None:                 # 미등록 사진 → 즉석 등록
                    pid = photolib.register_file(row.get("path") or iid)
                merged = photolib.merge_tags(row.get("user_tags") or "", new_tags)
                photolib.add_user_tags(pid, merged)
                ok += 1
            except Exception as e:
                fail += 1
                self._log(f"   ⚠️ 태그 추가 실패({Path(iid).name}): {e}\n")
        self.refresh_photos_list()
        self._log(f"🏷️ {ok}장에 태그 추가 완료" + (f" / 실패 {fail}" if fail else "")
                  + f": {add_str.strip()}\n")

    def bulk_clear_tags(self):
        """선택한 여러 사진의 태그를 한꺼번에 모두 지움(등록된 사진만 대상)."""
        sel = self.photos_tv.selection()
        if not sel:
            messagebox.showinfo("태그 지우기", "사진을 먼저 선택하세요(Ctrl/Shift로 여러 장 선택 가능).")
            return
        if not messagebox.askyesno(
                "태그 지우기",
                f"선택한 {len(sel)}장의 태그를 모두 지울까요?\n(되돌릴 수 없습니다)"):
            return
        ok = 0
        for iid in sel:
            row = self._photo_rows.get(iid, {})
            pid = row.get("id")
            if pid is None:        # 등록 안 된 사진은 태그도 없음 → 건너뜀
                continue
            try:
                photolib.add_user_tags(pid, "")
                ok += 1
            except Exception as e:
                self._log(f"   ⚠️ 태그 지우기 실패({Path(iid).name}): {e}\n")
        self.refresh_photos_list()
        self._log(f"🗑️ {ok}장의 태그를 지웠습니다.\n")

    def add_event_dialog(self):
        win = tk.Toplevel(self.root); win.title("이벤트 직접 추가"); win.transient(self.root)
        win.bind("<Map>", lambda e, w=win: self._theme_walk(w, self._theme, self.dark_mode))
        win.geometry("460x440")
        fields = [
            ("title_ko", "제목(한글) *", ""),
            ("title_en", "제목(영문)", ""),
            ("type", "구분(performance/exhibition/festival)", "performance"),
            ("category", "장르(dance/music/theater/art 등)", "dance"),
            ("start_date", "시작일 (YYYY-MM-DD) *", ""),
            ("end_date", "종료일 (YYYY-MM-DD)", ""),
            ("venue", "장소", ""),
            ("region", "지역(서울/경기 등)", ""),
            ("price", "요금(free/paid/mixed)", ""),
            ("booking_url", "예매/안내 URL", ""),
            ("importance", "중요도 1~5", "3"),
        ]
        vars_ = {}
        for key, label, default in fields:
            r = tk.Frame(win); r.pack(fill="x", padx=12, pady=2)
            tk.Label(r, text=label, width=22, anchor="w", font=("맑은 고딕", 9)).pack(side="left")
            v = tk.StringVar(value=default)
            tk.Entry(r, textvariable=v).pack(side="left", fill="x", expand=True)
            vars_[key] = v

        def save():
            ev = {k: v.get().strip() for k, v in vars_.items()}
            if not ev["title_ko"] or not ev["start_date"]:
                messagebox.showwarning("입력 확인", "제목(한글)과 시작일은 필수입니다.")
                return
            try:
                ev["importance"] = int(ev.get("importance") or 3)
            except ValueError:
                ev["importance"] = 3
            ev["id"] = evcol._mk_id(ev["title_ko"], ev["start_date"], ev.get("booking_url", ""))
            ev["source"] = "manual"
            try:
                evdb.upsert_event(ev)
            except Exception as e:
                messagebox.showwarning("저장 실패", str(e)); return
            self.refresh_events_list()
            self._log(f"＋ 이벤트 추가: {ev['title_ko']}\n")
            win.destroy()
        tk.Button(win, text="저장", command=save, bg="#2e7d32", fg="white",
                  font=("맑은 고딕", 10, "bold")).pack(pady=10)

    # ── 이벤트 → 시기별 큐레이션 글 생성 (미리보기) ───────────────────────────
    def run_curation(self):
        win = tk.Toplevel(self.root); win.title("큐레이션 글 생성"); win.transient(self.root)
        win.bind("<Map>", lambda e, w=win: self._theme_walk(w, self._theme, self.dark_mode))
        win.geometry("440x210")
        tk.Label(win, text="이벤트 DB로 k-culture-now용 시기별 큐레이션 글을 만들어 미리봅니다.",
                 font=("맑은 고딕", 9), fg="#555", wraplength=410, justify="left").pack(anchor="w", padx=14, pady=(12, 8))
        labels = [(evdb.CATEGORY_LABEL[k], k) for k in evdb.CATEGORY_KEYS]
        r = tk.Frame(win); r.pack(fill="x", padx=14, pady=4)
        tk.Label(r, text="카테고리", width=10, anchor="w").pack(side="left")
        cat_var = tk.StringVar(value=labels[3][0])   # This Week 기본
        ttk.Combobox(r, textvariable=cat_var, state="readonly", width=28,
                     values=[l for l, _ in labels]).pack(side="left")
        r = tk.Frame(win); r.pack(fill="x", padx=14, pady=4)
        tk.Label(r, text="기준 날짜", width=10, anchor="w").pack(side="left")
        date_var = tk.StringVar(value=date.today().isoformat())
        tk.Entry(r, textvariable=date_var, width=14).pack(side="left")
        tk.Label(r, text="(YYYY-MM-DD)", fg="#888", font=("맑은 고딕", 8)).pack(side="left", padx=4)

        def go():
            key = dict((l, k) for l, k in labels)[cat_var.get()]
            try:
                refd = datetime.strptime(date_var.get().strip(), "%Y-%m-%d").date()
            except ValueError:
                messagebox.showwarning("날짜 형식", "기준 날짜를 YYYY-MM-DD로 입력하세요."); return
            win.destroy()
            self._do_curation(key, cat_var.get(), refd)
        tk.Button(win, text="✍ 생성 후 미리보기", command=go, bg="#2e7d32", fg="white",
                  font=("맑은 고딕", 10, "bold")).pack(pady=12)

    def _do_curation(self, key, label, refd):
        ref_iso = refd.isoformat()
        events = evdb.due_for_category(key, refd)
        if not events:
            if not messagebox.askyesno(
                    "후보 없음",
                    f"'{label}' 시기에 해당하는 이벤트가 없습니다.\n"
                    "다가오는 이벤트 전체로 글을 만들어 볼까요?"):
                return
            events = [e for e in evdb.all_events() if (e.get("start_date") or "") >= ref_iso][:8]
            if not events:
                messagebox.showinfo("이벤트 없음",
                                    "먼저 [＋ 직접 추가]나 [📥 공연API로 수집]으로 이벤트를 넣으세요.")
                return
        settings = self._collect_settings()

        def job():
            self._curation_cfg = evcur.generate_curation_post(
                key, events, settings, ref_iso, log=self.log_q.put)

        def done():
            self._preview_curation()
        self._start_worker(job, f"큐레이션 글 생성 ({label})", on_done=done)

    def _preview_curation(self):
        cfg = getattr(self, "_curation_cfg", None)
        if not cfg:
            return
        ko_lbl = ", ".join(cfg.get("ko_labels", []))
        en_lbl = ", ".join(cfg.get("en_labels", []))
        meta = (f"<p style='color:#888'>큐레이션 미리보기 — {cfg.get('topic','')} · 기준 {cfg.get('date','')}</p>"
                f"<p style='color:#0277bd;font-size:13px'>🏷 {ko_lbl} / {en_lbl}</p>")
        html = (f"<html><head><meta charset='utf-8'><title>{cfg.get('ko_title','')}</title>"
                f"<style>body{{font-family:'맑은 고딕';max-width:760px;margin:40px auto;"
                f"line-height:1.7;padding:0 16px}}h1{{border-bottom:2px solid #eee}}</style></head><body>"
                f"{meta}<h1>🇰🇷 {cfg.get('ko_title','')}</h1>{cfg.get('body_ko','')}"
                f"<hr style='margin:40px 0'>"
                f"<h1>🇺🇸 {cfg.get('en_title','')}</h1>{cfg.get('body_en','')}</body></html>")
        out = core.GENERATED_DIR / "curation_preview.html"
        out.write_text(html, encoding="utf-8")
        webbrowser.open(out.as_uri())
        self._log("👁 큐레이션 글 미리보기 열림 (발행 연동은 다음 단계)\n")

    # ── 촬영 목록(샷 리스트) ─────────────────────────────────────────────────
    def run_shot_list(self):
        cfg = core.load_generated(self.selected)
        if not cfg:
            messagebox.showinfo("촬영 목록",
                                "먼저 [✍ 지금 생성]으로 글을 만든 뒤,\n촬영 목록을 만들 수 있습니다.")
            return
        settings = self._collect_settings()

        def job():
            self._shots = pplan.generate_shot_list(cfg, settings, log=self.log_q.put)

        def done():
            self._show_shot_list(cfg)
        self._start_worker(job, "촬영 목록 생성", on_done=done)

    def _show_shot_list(self, cfg):
        shots = getattr(self, "_shots", None)
        if not shots:
            return
        blog = self._blog_name(self.active_blog)
        ds = self.selected
        topic = cfg.get("topic", "")
        plain = pplan.shot_list_plain(blog, ds, topic, shots)

        win = tk.Toplevel(self.root); win.title(f"촬영 목록 — {blog}"); win.transient(self.root)
        win.bind("<Map>", lambda e, w=win: self._theme_walk(w, self._theme, self.dark_mode))
        win.geometry("620x560")
        tk.Label(win, text=f"📸 {blog} · {ds} · {topic}", font=("맑은 고딕", 11, "bold")).pack(anchor="w", padx=12, pady=(12, 4))
        tk.Label(win, text="직접 찍을 사진 안내입니다. 복사하거나 파일로 저장해 들고 다니며 촬영하세요.",
                 fg="#666", font=("맑은 고딕", 9)).pack(anchor="w", padx=12)
        txt = scrolledtext.ScrolledText(win, font=("맑은 고딕", 10), wrap="word")
        txt.pack(fill="both", expand=True, padx=12, pady=8)
        txt.insert("1.0", plain)
        txt.config(state="disabled")

        bar = tk.Frame(win); bar.pack(fill="x", padx=12, pady=(0, 12))

        def copy():
            self.root.clipboard_clear(); self.root.clipboard_append(plain)
            self._log("📋 촬영 목록 클립보드 복사 완료\n")
            messagebox.showinfo("복사 완료", "촬영 목록을 클립보드에 복사했습니다.\n엑셀·메모장 등에 붙여넣기 하세요.")

        def save(kind):
            ext = ".csv" if kind == "csv" else ".md"
            default = f"촬영목록_{blog}_{ds}{ext}".replace(" ", "_")
            f = filedialog.asksaveasfilename(defaultextension=ext, initialfile=default,
                                             filetypes=[(kind.upper(), f"*{ext}"), ("모든 파일", "*.*")])
            if not f:
                return
            content = (pplan.shot_list_csv(blog, ds, topic, shots) if kind == "csv"
                       else pplan.shot_list_markdown(blog, ds, topic, shots))
            # CSV는 엑셀 한글 호환을 위해 BOM 포함
            enc = "utf-8-sig" if kind == "csv" else "utf-8"
            Path(f).write_text(content, encoding=enc)
            self._log(f"💾 촬영 목록 저장: {f}\n")
            messagebox.showinfo("저장 완료", f"저장했습니다:\n{f}")

        tk.Button(bar, text="📋 복사", command=copy, bg="#1565c0", fg="white",
                  font=("맑은 고딕", 10, "bold")).pack(side="left")
        tk.Button(bar, text="💾 CSV(엑셀) 저장", command=lambda: save("csv")).pack(side="left", padx=6)
        tk.Button(bar, text="💾 Markdown 저장", command=lambda: save("md")).pack(side="left")
        tk.Button(bar, text="📊 구글 시트로 전송",
                  command=lambda: self._export_sheets(blog, ds, topic, shots),
                  bg="#0f9d58", fg="white", font=("맑은 고딕", 10, "bold")).pack(side="left", padx=6)
        tk.Button(bar, text="🎨 ComfyUI로 이미지 생성",
                  command=lambda w=win: self._gen_shot_images(ds, shots, w),
                  bg="#6a1b9a", fg="white", font=("맑은 고딕", 10, "bold")).pack(side="left")
        tk.Button(bar, text="닫기", command=win.destroy).pack(side="right")

    def _gen_shot_images(self, ds, shots, dialog=None):
        settings = self._collect_settings()
        if not imgen.is_available(settings):
            if not imgen.find_launcher(settings):
                messagebox.showwarning(
                    "ComfyUI 경로 필요",
                    "ComfyUI가 켜져 있지 않고, 설치 경로도 못 찾았습니다.\n"
                    "[⚙️ 설정]의 'ComfyUI 경로'에 ComfyUI 폴더를 지정한 뒤 다시 시도하세요.")
                return
            if not messagebox.askyesno(
                    "ComfyUI 자동 실행",
                    "ComfyUI가 꺼져 있어요. 지금 자동으로 켜고 이미지를 생성할까요?\n"
                    "(첫 부팅은 모델 로딩 때문에 1~2분 걸릴 수 있습니다.\n"
                    " 켜진 상태로 두면 다음부터는 즉시 생성됩니다.)"):
                return
        elif not messagebox.askyesno(
                "AI 이미지 생성",
                f"촬영목록 {len(shots)}장을 ComfyUI로 생성합니다(몇 분 걸릴 수 있음).\n\n"
                "※ AI 이미지는 실제 특정 장소·문화재를 정확히 그리진 못합니다(분위기·개념용).\n"
                "생성 후 [📂 폴더 사진 글에 반영]으로 글에 넣을 수 있어요.\n\n계속할까요?"):
            return
        if dialog:
            dialog.destroy()
        out_dir = core.GENERATED_DIR / core.post_date(ds) / "ai_images"

        def job():
            self._ai_paths = imgen.generate_shot_images(shots, settings, out_dir, log=self.log_q.put)

        def done():
            paths = getattr(self, "_ai_paths", [])
            if not paths:
                messagebox.showwarning("생성 실패", "이미지를 생성하지 못했습니다. ComfyUI 로그를 확인하세요.")
                return
            self.photo_dir_var.set(str(out_dir))
            self._refresh_photo_count()
            messagebox.showinfo(
                "AI 이미지 생성 완료",
                f"{len(paths)}장을 생성했습니다:\n{out_dir}\n\n"
                "사진 폴더로 지정해 두었습니다. [📂 폴더 사진 글에 반영]을 누르면\n"
                "전체+소주제별로 글에 들어갑니다.")
        self._start_worker(job, "ComfyUI 이미지 생성", on_done=done)

    def _export_sheets(self, blog, ds, topic, shots):
        if self.busy:
            messagebox.showinfo("진행 중", "다른 작업이 끝난 뒤 시도하세요.")
            return
        if not messagebox.askyesno(
                "구글 시트 전송",
                "촬영 목록을 새 구글 시트로 만듭니다.\n\n"
                "처음 한 번은 브라우저에서 구글 로그인·권한 동의가 필요합니다\n"
                "(기존 Blogger 인증과 별개, 시트 권한만).\n\n계속할까요?"):
            return

        def job():
            try:
                self._sheet_url = sheets_export.export_shot_list(
                    blog, ds, topic, shots, log=self.log_q.put)
                self._sheet_err = None
            except Exception as e:
                self._sheet_url = None
                self._sheet_err = str(e)

        def done():
            u = getattr(self, "_sheet_url", None)
            if u:
                if messagebox.askyesno("구글 시트 전송 완료",
                                       f"시트를 만들었습니다:\n{u}\n\n브라우저로 열까요?"):
                    webbrowser.open(u)
                return
            # 실패(주로 인증 거부) — CSV 우회 안내
            err = getattr(self, "_sheet_err", "") or ""
            denied = "access_denied" in err or "denied" in err.lower()
            msg = ("구글 시트 전송에 실패했습니다.\n\n"
                   + ("구글 인증이 거부되었습니다(access_denied).\n\n" if denied else f"{err[:160]}\n\n")
                   + "지금 바로 쓰는 방법: [💾 CSV(엑셀) 저장]으로 받은 뒤,\n"
                   "구글 시트에서 [파일 → 가져오기]로 그 CSV를 열면 똑같이 표로 들어갑니다.\n\n"
                   "‘구글 시트로 바로 보내기’를 쓰려면 Google Cloud Console에서:\n"
                   "① Google Sheets API ‘사용 설정’\n"
                   "② OAuth 동의 화면에 spreadsheets 범위 추가\n"
                   "③ (테스트 앱이면) 테스트 사용자에 본인 계정 추가\n"
                   "후 다시 시도하세요.")
            messagebox.showwarning("구글 시트 전송 실패", msg)
        self._start_worker(job, "구글 시트 전송", on_done=done)

    # ── 소주제별 이미지 자동 채우기 (전체 + 소주제별) ─────────────────────────
    def run_autofill_images(self):
        cfg = core.load_generated(self.selected)
        if not cfg:
            messagebox.showinfo("이미지 자동 채우기",
                                "먼저 [✍ 지금 생성]으로 글을 만든 뒤 사용하세요.")
            return
        if not messagebox.askyesno(
                "소주제별 이미지 자동 채우기",
                "글의 전체 주제 1장 + 소주제별 1장을 무료·저작권 안전 이미지에서 찾아\n"
                "출처와 함께 넣습니다.\n\n"
                "(촬영 목록의 영어 검색어를 활용 — 시간이 좀 걸립니다.)\n계속할까요?"):
            return
        settings = self._collect_settings()

        def job():
            shots = pplan.generate_shot_list(cfg, settings, log=self.log_q.put)
            items = []
            for s in shots:
                q = s.get("search_en") or s.get("heading") or cfg.get("en_title", "")
                self.log_q.put(f"   🔎 이미지 검색: {q}\n")
                found = imgf.find_images(q, n=1, settings=settings)
                items.append(found[0] if found else None)
            self._autofill_n = core.insert_found_images_by_section(
                self.selected, items, settings=settings, log=self.log_q.put)

        def done():
            self.preview()
            n = getattr(self, "_autofill_n", 0)
            messagebox.showinfo(
                "이미지 자동 채우기",
                f"{n}장을 전체·소주제별로 넣었습니다. 미리보기를 확인하세요.\n\n"
                "마음에 안 드는 이미지는 [🖼 이미지 찾기]로 바꾸거나,\n"
                "직접 찍은 사진을 [📂 폴더 사진 글에 반영]으로 교체할 수 있습니다.")
        self._start_worker(job, "소주제별 이미지 자동 채우기", on_done=done)

    # ── 폴더 사진을 글 작성 이후에 반영 ───────────────────────────────────────
    def apply_folder_photos(self):
        ds = self.selected
        if not core.load_generated(ds):
            messagebox.showinfo("폴더 사진 반영",
                                "먼저 [✍ 지금 생성]으로 글을 만든 뒤 사진을 반영할 수 있습니다.")
            return
        pd = self.photo_dir_var.get().strip()
        if not pd:
            pd = filedialog.askdirectory(title="찍은 사진이 있는 폴더 선택")
            if not pd:
                return
            self.photo_dir_var.set(pd)
        entry = self.data["entries"].get(ds) or {}
        published = entry.get("status") == core.ST_PUBLISHED

        # 이미 발행된 글이면: 삭제·재발행 없이 '그 글을 바로 업데이트' 제안
        if published and (entry.get("en_url") or entry.get("ko_url")):
            if messagebox.askyesno(
                    "발행된 글에 사진 추가",
                    "이 글은 이미 발행됐습니다.\n\n블로그의 그 글에 사진을 바로 추가(업데이트)할까요?\n"
                    "→ 삭제·재발행 없이 같은 글(URL 유지)에 사진이 들어갑니다.\n"
                    "[아니오] = 추가하지 않음"):
                settings = self._collect_settings()

                def job():
                    self._photo_upd = core.add_photos_to_published(
                        ds, pd, self.data, settings, log=self.log_q.put)

                def done():
                    core.save_schedule(self.data)
                    self.load_day(ds)
                    messagebox.showinfo(
                        "사진 추가 완료",
                        f"발행된 글 {getattr(self, '_photo_upd', 0)}개에 사진을 추가했습니다.\n"
                        "블로그에서 확인하세요(같은 URL 유지).")
                self._start_worker(job, "발행된 글에 사진 추가", on_done=done)
            return

        try:
            n = core.apply_photo_folder(ds, pd, self.data, log=self.log_q.put)
        except Exception as e:
            messagebox.showwarning("반영 실패", str(e)); return
        # 로컬 사진으로 배치 미리보기
        prev = core.local_photo_preview(ds, pd)
        if prev:
            html = (f"<html><head><meta charset='utf-8'><title>사진 미리보기</title>"
                    f"<style>body{{font-family:'맑은 고딕';max-width:760px;margin:40px auto;"
                    f"line-height:1.7;padding:0 16px}}h1{{border-bottom:2px solid #eee}}</style></head><body>"
                    f"<p style='color:#888'>사진 배치 미리보기 — {ds} (로컬 사진, 발행 시 업로드됨)</p>"
                    f"<h1>🇰🇷 {prev['ko_title']}</h1>{prev['body_ko']}"
                    f"<hr style='margin:40px 0'>"
                    f"<h1>🇺🇸 {prev['en_title']}</h1>{prev['body_en']}</body></html>")
            out = core.GENERATED_DIR / ds / "photo_preview.html"
            out.write_text(html, encoding="utf-8")
            webbrowser.open(out.as_uri())
        self.refresh_calendar(); self.load_day(ds)
        msg = (f"사진 {n}장을 글에 배치했습니다 (전체 1장 + 소주제별).\n"
               "미리보기로 위치를 확인하세요.\n\n"
               "이제 [🚀 지금 발행]을 누르면 사진이 업로드되어 발행됩니다.")
        if published:
            msg += ("\n\n⚠️ 이 글은 이미 발행됨 — 다시 [🚀 지금 발행]하면 "
                    "사진 포함본이 새로 발행됩니다(기존 글은 블로그에서 삭제 필요).")
        messagebox.showinfo("폴더 사진 반영 완료", msg)

    # ── 발행된 글 목록에서 고르기 ─────────────────────────────────────────────
    def show_published_list(self):
        rows = core.published_posts(self.data)
        win = tk.Toplevel(self.root)
        win.title(f"발행된 글 목록 — {self._blog_name(self.active_blog)}")
        win.transient(self.root); win.geometry("760x520")
        win.bind("<Map>", lambda e, w=win: self._theme_walk(w, self._theme, self.dark_mode))
        tk.Label(win, text="• [선택]하면 그 글로 이동(사진 추가 등).  "
                           "• 블로그에서 이미 지운 글은 여러 개 선택(Ctrl/Shift) 후 "
                           "[🗑 스케줄에서 삭제]로 정리하세요 → 그 날짜에 새 글을 쓸 수 있습니다.",
                 font=("맑은 고딕", 9), fg="#555", wraplength=720, justify="left").pack(
            anchor="w", padx=12, pady=(12, 6))
        if not rows:
            tk.Label(win, text="아직 발행된 글이 없습니다.", fg="#888",
                     font=("맑은 고딕", 11)).pack(padx=12, pady=20)
            tk.Button(win, text="닫기", command=win.destroy).pack(pady=8)
            return

        cols = ("date", "time", "title")
        tv = ttk.Treeview(win, columns=cols, show="headings", height=16, selectmode="extended")
        for c, h, w in (("date", "날짜", 100), ("time", "시각", 70), ("title", "제목", 540)):
            tv.heading(c, text=h)
            tv.column(c, width=w, anchor=("w" if c == "title" else "center"))
        tv.pack(fill="both", expand=True, padx=12, pady=4)
        sb = ttk.Scrollbar(win, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        for r in rows:
            tv.insert("", "end", iid=r["key"],
                      values=(r["date"], r["time"] or "--:--", r["title"][:70]))
        self._pub_rows = {r["key"]: r for r in rows}

        def go_to():
            sel = tv.selection()
            if not sel:
                messagebox.showinfo("선택", "목록에서 글을 하나 선택하세요."); return
            key = sel[0]
            win.destroy()
            self.load_day(key)
            self._log(f"📋 발행글 선택: {key} — {self._pub_rows[key]['title'][:30]}\n")

        def open_blog():
            sel = tv.selection()
            if not sel:
                return
            r = self._pub_rows[sel[0]]
            url = r.get("ko_url") or r.get("en_url")
            if url:
                webbrowser.open(url)

        def delete_selected(from_blog: bool):
            sel = list(tv.selection())
            if not sel:
                messagebox.showinfo("삭제", "지울 글을 하나 이상 선택하세요(Ctrl/Shift로 여러 개).")
                return
            action = "블로그 + 스케줄 모두" if from_blog else "스케줄(달력)에서"
            if not messagebox.askyesno(
                    "삭제 확인",
                    f"선택한 {len(sel)}개 글을 {action} 삭제합니다.\n계속할까요?"):
                return
            if from_blog:
                win.destroy()
                self._set_buttons(False)
                entries_to_del = {k: (self.data["entries"].get(k) or {}) for k in sel}

                def job():
                    for key, e in entries_to_del.items():
                        self._log(f"🗑 블로그 삭제 중: {key}\n")
                        try:
                            core.delete_blog_posts(e, log=self._log)
                        except Exception as ex:
                            self._log(f"   ❌ {key} 오류: {ex}\n")
                    self.root.after(0, _finish_blog)

                def _finish_blog():
                    for key in entries_to_del:
                        self.data["entries"].pop(key, None)
                    core.save_schedule(self.data)
                    self.refresh_calendar()
                    self.load_day(self.selected_date)
                    self._set_buttons(True)
                    self._log(f"✅ {len(entries_to_del)}개 삭제 완료\n")

                threading.Thread(target=job, daemon=True).start()
            else:
                for key in sel:
                    self.data["entries"].pop(key, None)
                    tv.delete(key)
                core.save_schedule(self.data)
                self.refresh_calendar()
                self.load_day(self.selected_date)
                self._log(f"🧹 발행 기록 {len(sel)}개를 스케줄에서 삭제\n")
                messagebox.showinfo("삭제 완료",
                                    f"{len(sel)}개를 스케줄에서 지웠습니다. 이제 그 날짜에 새 글을 쓸 수 있어요.")

        tv.bind("<Double-1>", lambda e: go_to())
        bar = tk.Frame(win); bar.pack(fill="x", padx=12, pady=(0, 12))
        tk.Button(bar, text="✅ 이 글 선택", command=go_to,
                  bg="#2e7d32", fg="white", font=("맑은 고딕", 10, "bold")).pack(side="left")
        tk.Button(bar, text="🌐 블로그에서 열기", command=open_blog).pack(side="left", padx=6)
        tk.Button(bar, text="🗑 블로그+스케줄 삭제", command=lambda: delete_selected(True),
                  bg="#c62828", fg="white", font=("맑은 고딕", 10, "bold")).pack(side="left")
        tk.Button(bar, text="📋 스케줄만 삭제", command=lambda: delete_selected(False),
                  font=("맑은 고딕", 10)).pack(side="left", padx=4)
        tk.Button(bar, text="닫기", command=win.destroy).pack(side="right")

    # ── 시기별 자동 발행 트리거 ───────────────────────────────────────────────
    def _find_now_blog(self):
        """등록된 블로그 중 시의성(k-culture-now/k-arts-now) 블로그 id를 찾음(없으면 None)."""
        reg = core.load_registry()
        for bid, b in reg.get("blogs", {}).items():
            if core.is_karts_now(b.get("url", "")):
                return bid
        return None

    def run_trigger_today(self):
        cats = evtrig.due_categories(date.today())
        if not cats:
            nd = evtrig.next_due()
            msg = "오늘은 예정된 트리거가 없습니다.\n\n다가오는 예정:\n"
            for ds, cs in nd[:6]:
                msg += f"  {ds} — {', '.join(evdb.CATEGORY_LABEL[c] for c in cs)}\n"
            messagebox.showinfo("오늘 트리거", msg)
            return
        cat_txt = ", ".join(evdb.CATEGORY_LABEL[c] for c in cats)
        now_bid = self._find_now_blog()
        publish = False
        if now_bid:
            publish = messagebox.askyesno(
                "오늘 트리거",
                f"오늘 예정: {cat_txt}\n\n"
                "k-culture-now에 바로 발행할까요?\n"
                "[아니오] = 글만 만들어 미리보기")
        else:
            messagebox.showinfo(
                "오늘 트리거",
                f"오늘 예정: {cat_txt}\n\n"
                "k-culture-now 블로그가 아직 등록되지 않아\n'미리보기'로만 생성합니다.")
        settings = self._collect_settings()

        def job():
            if publish and now_bid:
                pubfn = lambda cfg, log: core.publish_curation(now_bid, cfg, log=log)
                self._trigger_results = evtrig.run_due(settings, publish_fn=pubfn, log=self.log_q.put)
            else:
                self._trigger_results = evtrig.run_due(settings, log=self.log_q.put)

        def done():
            self._show_trigger_results()
            self.refresh_events_list()
        self._start_worker(job, f"오늘 트리거 ({cat_txt})", on_done=done)

    def _show_trigger_results(self):
        res = getattr(self, "_trigger_results", []) or []
        made = [r for r in res if r.get("cfg")]
        if not made:
            messagebox.showinfo("오늘 트리거",
                                "예정 카테고리는 있었지만 발행 후보 이벤트가 없었습니다.\n"
                                "[＋ 직접 추가]나 [📥 수집]으로 이벤트를 채워주세요.")
            return
        blocks = []
        published = []
        for r in made:
            cfg = r["cfg"]
            tag = evdb.CATEGORY_LABEL.get(r["category"], r["category"])
            if r.get("status") == "published":
                published.append(f"{tag}: {r.get('ko_url','')}")
            blocks.append(
                f"<section style='margin-bottom:48px'><p style='color:#ef6c00'>● {tag}</p>"
                f"<h1>🇰🇷 {cfg.get('ko_title','')}</h1>{cfg.get('body_ko','')}"
                f"<h2 style='color:#888'>🇺🇸 {cfg.get('en_title','')}</h2>{cfg.get('body_en','')}</section>")
        html = ("<html><head><meta charset='utf-8'><title>오늘 트리거</title>"
                "<style>body{font-family:'맑은 고딕';max-width:760px;margin:40px auto;"
                "line-height:1.7;padding:0 16px}h1{border-bottom:2px solid #eee}</style></head><body>"
                + "".join(blocks) + "</body></html>")
        out = core.GENERATED_DIR / "trigger_preview.html"
        out.write_text(html, encoding="utf-8")
        webbrowser.open(out.as_uri())
        if published:
            messagebox.showinfo("발행 완료", "발행된 글:\n" + "\n".join(published))
        else:
            self._log("👁 오늘 트리거 — 글 생성·미리보기 완료(발행 안 함)\n")

    def _build_calendar_frame(self, parent):
        nav = tk.Frame(parent)
        nav.pack(fill="x")
        tk.Button(nav, text="◀", command=self.nav_prev, width=3).pack(side="left")
        self.month_lbl = tk.Label(nav, text="", font=("맑은 고딕", 13, "bold"))
        self.month_lbl.pack(side="left", expand=True)
        # 보기 전환(월간/주간/일간)
        self.view_mode_var = tk.StringVar(value="month")
        for label, mode in [("월간", "month"), ("주간", "week"), ("일간", "day")]:
            tk.Radiobutton(nav, text=label, variable=self.view_mode_var, value=mode,
                           indicatoron=False, width=4, font=("맑은 고딕", 9),
                           command=self.set_view_mode).pack(side="left", padx=1)
        tk.Button(nav, text="▶", command=self.nav_next, width=3).pack(side="right")
        tk.Button(nav, text="오늘", command=self.go_today, width=5).pack(side="right", padx=4)

        # 여러 날짜 한번에 삭제(Ctrl+클릭으로 날짜 여러 개 선택)
        msbar = tk.Frame(parent); msbar.pack(fill="x", pady=(2, 0))
        tk.Label(msbar, text="Ctrl+클릭으로 여러 날짜 선택 →", font=("맑은 고딕", 8), fg="#888").pack(side="left")
        self.multisel_lbl = tk.Label(msbar, text="", font=("맑은 고딕", 8, "bold"), fg="#1565c0")
        self.multisel_lbl.pack(side="left", padx=6)
        tk.Button(msbar, text="🗑 선택 날짜 삭제", font=("맑은 고딕", 8, "bold"), fg="#c62828",
                  command=self.delete_multi_selected_days).pack(side="right")
        tk.Button(msbar, text="선택 해제", font=("맑은 고딕", 8),
                  command=self.clear_multi_selected_days).pack(side="right", padx=(0, 4))

        # 요일 헤더(월간에서만 표시)
        head = tk.Frame(parent)
        head.pack(fill="x", pady=(6, 2))
        self._weekhead = head
        for i, w in enumerate(WEEK_HEADERS):
            fg = "#d32f2f" if i == 0 else ("#1565c0" if i == 6 else "#333")
            tk.Label(head, text=w, fg=fg, font=("맑은 고딕", 10, "bold"),
                     width=8).grid(row=0, column=i, sticky="nsew")
            head.columnconfigure(i, weight=1)

        self.grid_frame = tk.Frame(parent)
        self.grid_frame.pack(fill="both", expand=True)

        # 범례 (달력 셀과 같은 팔레트를 써야 함 — 다크/라이트 전환 시 _draw_legend()로 다시 그림)
        self._legend_frame = tk.Frame(parent)
        self._legend_frame.pack(fill="x", pady=(6, 0))
        self._draw_legend()

    def _draw_legend(self):
        legend = getattr(self, "_legend_frame", None)
        if not legend:
            return
        for w in legend.winfo_children():
            w.destroy()
        palette = COLORS_DARK if self.dark_mode else COLORS
        weekly_bg = "#3a3220" if self.dark_mode else "#fff3cd"
        items = [(weekly_bg, "↻ 요일 템플릿 예정"),
                 (palette[core.ST_PENDING], "주제 입력됨"),
                 (palette[core.ST_GENERATED], "글 생성됨"),
                 (palette[core.ST_PUBLISHED], "발행 완료")]
        for bg, label in items:
            tk.Label(legend, text="  ", bg=bg, relief="solid", bd=1).pack(side="left", padx=(8, 3))
            tk.Label(legend, text=label, font=("맑은 고딕", 9)).pack(side="left")

    def _build_side_panel(self, parent):
        # 선택 날짜 편집
        self.sel_box = tk.LabelFrame(parent, text="선택한 날짜", font=("맑은 고딕", 10, "bold"))
        self.sel_box.pack(fill="x", pady=(0, 8))

        self.sel_date_lbl = tk.Label(self.sel_box, text="", font=("맑은 고딕", 12, "bold"))
        self.sel_date_lbl.pack(anchor="w", padx=8, pady=(6, 0))
        self.sel_status_lbl = tk.Label(self.sel_box, text="", font=("맑은 고딕", 9), fg="#555")
        self.sel_status_lbl.pack(anchor="w", padx=8)

        self.origin_lbl = tk.Label(self.sel_box, text="", font=("맑은 고딕", 9), fg="#1565c0",
                                   wraplength=330, justify="left")
        self.origin_lbl.pack(anchor="w", padx=8)

        # 이 날짜의 글들(여러 개 가능 — 시간대가 서로 달라야 함)
        prow = tk.Frame(self.sel_box); prow.pack(fill="x", padx=8, pady=(4, 0))
        tk.Label(prow, text="이 날짜의 글:", font=("맑은 고딕", 9)).pack(side="left")
        self.post_sel_var = tk.StringVar()
        self.post_sel = ttk.Combobox(prow, textvariable=self.post_sel_var, state="readonly",
                                     width=20, font=("맑은 고딕", 9))
        self.post_sel.pack(side="left", fill="x", expand=True, padx=4)
        self.post_sel.bind("<<ComboboxSelected>>", self._on_post_selected)
        tk.Button(prow, text="＋글", width=4, command=self.add_post).pack(side="left")
        tk.Button(prow, text="🗑", width=2, command=self.delete_post).pack(side="left", padx=(2, 0))

        tk.Label(self.sel_box, text="발행 주제:", font=("맑은 고딕", 9)).pack(anchor="w", padx=8, pady=(6, 0))
        self.topic_entry = tk.Entry(self.sel_box, font=("맑은 고딕", 11))
        self.topic_entry.pack(fill="x", padx=8, pady=2)

        tk.Label(self.sel_box, text="참고 사이트 · 작성 방향:", font=("맑은 고딕", 9)).pack(anchor="w", padx=8, pady=(4, 0))
        self.refs_text = tk.Text(self.sel_box, height=3, font=("맑은 고딕", 9), wrap="word")
        self.refs_text.pack(fill="x", padx=8, pady=2)

        trow = tk.Frame(self.sel_box); trow.pack(fill="x", padx=8, pady=2)
        tk.Label(trow, text="이 날 발행시각", anchor="w").pack(side="left")
        self.dtime_var = tk.StringVar()
        tk.Entry(trow, textvariable=self.dtime_var, width=8).pack(side="left", padx=4)
        tk.Label(trow, text="(비우면 기본/요일값)", fg="#888", font=("맑은 고딕", 8)).pack(side="left")

        tk.Label(self.sel_box, text="사진/이미지 폴더:", font=("맑은 고딕", 9)).pack(anchor="w", padx=8, pady=(4, 0))
        prow = tk.Frame(self.sel_box); prow.pack(fill="x", padx=8, pady=2)
        self.photo_dir_var = tk.StringVar()
        tk.Entry(prow, textvariable=self.photo_dir_var, font=("맑은 고딕", 8)).pack(
            side="left", fill="x", expand=True)
        tk.Button(prow, text="폴더 선택", command=self.pick_photo_dir).pack(side="left", padx=(4, 0))
        tk.Button(prow, text="✕", width=2, command=self.clear_photo_dir).pack(side="left", padx=(2, 0))
        self.photo_cnt_lbl = tk.Label(self.sel_box, text="", font=("맑은 고딕", 8), fg="#2e7d32")
        self.photo_cnt_lbl.pack(anchor="w", padx=8)
        tk.Button(self.sel_box, text="📂 폴더 사진 글에 반영 (작성 후에도)",
                  command=self.apply_folder_photos, bg="#5d4037", fg="white").pack(
            fill="x", padx=8, pady=(2, 0))
        tk.Button(self.sel_box, text="📋 발행된 글 목록에서 고르기",
                  command=self.show_published_list, bg="#37474f", fg="white").pack(
            fill="x", padx=8, pady=(2, 0))

        tk.Button(self.sel_box, text="이 날짜 저장", command=self.save_topic).pack(fill="x", padx=8, pady=(2, 6))

        btnrow = tk.Frame(self.sel_box)
        btnrow.pack(fill="x", padx=8, pady=(0, 8))
        self.btn_gen = tk.Button(btnrow, text="✍ 1. 새 글 생성", command=self.run_generate)
        self.btn_gen.pack(side="left", expand=True, fill="x", padx=(0, 2))
        self.btn_pub = tk.Button(btnrow, text="🚀 2. 생성된 글 발행", command=self.run_publish,
                                 bg="#2e7d32", fg="white")
        self.btn_pub.pack(side="left", expand=True, fill="x", padx=2)
        self.btn_prev = tk.Button(btnrow, text="👁 미리보기", command=self.preview)
        self.btn_prev.pack(side="left", expand=True, fill="x", padx=(2, 0))

        self.btn_img = tk.Button(self.sel_box, text="🖼 이미지 찾기 (직접 사진이 없을 때)",
                                 command=self.run_find_images, bg="#00695c", fg="white")
        self.btn_img.pack(fill="x", padx=8, pady=(0, 4))
        self.btn_shot = tk.Button(self.sel_box, text="📸 촬영 목록 만들기 (직접 찍을 사진 안내)",
                                  command=self.run_shot_list, bg="#455a64", fg="white")
        self.btn_shot.pack(fill="x", padx=8, pady=(0, 4))
        self.btn_autoimg = tk.Button(self.sel_box, text="🖼 소주제별 이미지 자동 채우기 (전체+소주제 6장)",
                                     command=self.run_autofill_images, bg="#00838f", fg="white")
        self.btn_autoimg.pack(fill="x", padx=8, pady=(0, 6))

        self.url_lbl = tk.Label(self.sel_box, text="", font=("맑은 고딕", 8), fg="#1565c0",
                                wraplength=330, justify="left", cursor="hand2")
        self.url_lbl.pack(anchor="w", padx=8, pady=(0, 6))
        self.url_lbl.bind("<Button-1>", self._open_urls)

    def _build_settings_bar(self, parent):
        """공용 설정 바 — 헤더의 [⚙️ 설정] 버튼으로 열고 닫음(기본 숨김)."""
        s = self.data["settings"]
        box = tk.LabelFrame(parent, text="설정", font=("맑은 고딕", 10, "bold"))
        box.pack(fill="x", padx=10, pady=(0, 4))
        self.settings_box = box

        col1 = tk.Frame(box); col1.pack(side="left", fill="x", expand=True, padx=8, pady=6)
        r = tk.Frame(col1); r.pack(fill="x", pady=2)
        tk.Label(r, text="매일 기본 발행시각", width=15, anchor="w").pack(side="left")
        self.time_var = tk.StringVar(value=s["publish_time"])
        tk.Entry(r, textvariable=self.time_var, width=8).pack(side="left")
        tk.Label(r, text="(요일별로 정하면 그 값 우선)", fg="#888",
                 font=("맑은 고딕", 8)).pack(side="left", padx=4)

        r = tk.Frame(col1); r.pack(fill="x", pady=2)
        self.auto_var = tk.BooleanVar(value=s["auto_publish"])
        tk.Checkbutton(r, text="프로그램이 켜져 있으면 그 시각에 자동 발행",
                       variable=self.auto_var).pack(side="left")

        r = tk.Frame(col1); r.pack(fill="x", pady=2)
        tk.Label(r, text="글 생성 엔진", width=15, anchor="w").pack(side="left")
        self.llm_var = tk.StringVar(value=s["llm"])
        tk.Radiobutton(r, text="로컬 gemma4(무료)", variable=self.llm_var,
                       value="gemma4").pack(side="left")
        tk.Radiobutton(r, text="Claude(API키)", variable=self.llm_var,
                       value="claude").pack(side="left")

        r = tk.Frame(col1); r.pack(fill="x", pady=2)
        tk.Label(r, text="소주제 개수", width=15, anchor="w").pack(side="left")
        self.sections_var = tk.StringVar(value=str(s.get("sections", 5)))
        tk.Spinbox(r, from_=3, to=10, width=4, textvariable=self.sections_var).pack(side="left")
        tk.Label(r, text="개로 나눠 깊이 있게 작성 (많을수록 글이 길고 느림)",
                 fg="#888", font=("맑은 고딕", 8)).pack(side="left", padx=4)

        col2 = tk.Frame(box); col2.pack(side="left", fill="x", expand=True, padx=8, pady=6)
        r = tk.Frame(col2); r.pack(fill="x", pady=2)
        tk.Label(r, text="gemma 모델", width=10, anchor="w").pack(side="left")
        self.omodel_var = tk.StringVar(value=s["ollama_model"])
        tk.Button(r, text="확인/받기", command=self.check_and_pull_model,
                  font=("맑은 고딕", 8)).pack(side="right", padx=(4, 0))
        tk.Entry(r, textvariable=self.omodel_var).pack(side="left", fill="x", expand=True)
        r = tk.Frame(col2); r.pack(fill="x", pady=2)
        tk.Label(r, text="Claude 모델", width=10, anchor="w").pack(side="left")
        self.cmodel_var = tk.StringVar(value=s["claude_model"])
        tk.Entry(r, textvariable=self.cmodel_var).pack(side="left", fill="x", expand=True)
        r = tk.Frame(col2); r.pack(fill="x", pady=2)
        tk.Label(r, text="Claude 키", width=10, anchor="w").pack(side="left")
        self.ckey_var = tk.StringVar(value=s["claude_api_key"])
        tk.Entry(r, textvariable=self.ckey_var, show="•").pack(side="left", fill="x", expand=True)
        r = tk.Frame(col2); r.pack(fill="x", pady=2)
        tk.Label(r, text="공연API 키", width=10, anchor="w").pack(side="left")
        self.culturekey_var = tk.StringVar(value=s.get("culture_api_key", ""))
        tk.Entry(r, textvariable=self.culturekey_var, show="•").pack(side="left", fill="x", expand=True)
        r = tk.Frame(col2); r.pack(fill="x", pady=2)
        tk.Label(r, text="네이버 ID", width=10, anchor="w").pack(side="left")
        self.naverid_var = tk.StringVar(value=s.get("naver_client_id", ""))
        tk.Entry(r, textvariable=self.naverid_var).pack(side="left", fill="x", expand=True)
        r = tk.Frame(col2); r.pack(fill="x", pady=2)
        tk.Label(r, text="네이버 Secret", width=10, anchor="w").pack(side="left")
        self.naversecret_var = tk.StringVar(value=s.get("naver_client_secret", ""))
        tk.Entry(r, textvariable=self.naversecret_var, show="•").pack(side="left", fill="x", expand=True)
        r = tk.Frame(col2); r.pack(fill="x", pady=2)
        tk.Label(r, text="내 사진 ©", width=10, anchor="w").pack(side="left")
        self.photocredit_var = tk.StringVar(value=s.get("photo_credit", ""))
        tk.Entry(r, textvariable=self.photocredit_var).pack(side="left", fill="x", expand=True)
        r = tk.Frame(col2); r.pack(fill="x", pady=2)
        tk.Label(r, text="TourAPI", width=10, anchor="w").pack(side="left")
        self.tourkey_var = tk.StringVar(value=s.get("tourapi_key", ""))
        tk.Entry(r, textvariable=self.tourkey_var, show="•").pack(side="left", fill="x", expand=True)
        r = tk.Frame(col2); r.pack(fill="x", pady=2)
        tk.Label(r, text="공유마당", width=10, anchor="w").pack(side="left")
        self.gongukey_var = tk.StringVar(value=s.get("gongu_key", ""))
        tk.Entry(r, textvariable=self.gongukey_var, show="•").pack(side="left", fill="x", expand=True)
        r = tk.Frame(col2); r.pack(fill="x", pady=2)
        tk.Label(r, text="Pexels", width=10, anchor="w").pack(side="left")
        self.pexkey_var = tk.StringVar(value=s.get("pexels_key", ""))
        tk.Entry(r, textvariable=self.pexkey_var, show="•").pack(side="left", fill="x", expand=True)
        r = tk.Frame(col2); r.pack(fill="x", pady=2)
        tk.Label(r, text="Pixabay", width=10, anchor="w").pack(side="left")
        self.pixkey_var = tk.StringVar(value=s.get("pixabay_key", ""))
        tk.Entry(r, textvariable=self.pixkey_var, show="•").pack(side="left", fill="x", expand=True)
        r = tk.Frame(col2); r.pack(fill="x", pady=2)
        tk.Label(r, text="Unsplash", width=10, anchor="w").pack(side="left")
        self.unsplashkey_var = tk.StringVar(value=s.get("unsplash_key", ""))
        tk.Entry(r, textvariable=self.unsplashkey_var, show="•").pack(side="left", fill="x", expand=True)
        r = tk.Frame(col2); r.pack(fill="x", pady=2)
        tk.Label(r, text="위키미디어", width=10, anchor="w").pack(side="left")
        self.wikitoken_var = tk.StringVar(value=s.get("wikimedia_token", ""))
        tk.Entry(r, textvariable=self.wikitoken_var, show="•").pack(side="left", fill="x", expand=True)
        # 저자(E-E-A-T) — 이름은 JSON-LD author 와 본문 '글쓴이'에 함께 쓰임
        r = tk.Frame(col2); r.pack(fill="x", pady=2)
        tk.Label(r, text="글쓴이", width=10, anchor="w").pack(side="left")
        self.authorname_var = tk.StringVar(value=s.get("author_name", ""))
        tk.Entry(r, textvariable=self.authorname_var).pack(side="left", fill="x", expand=True)
        r = tk.Frame(col2); r.pack(fill="x", pady=2)
        tk.Label(r, text="저자소개(한)", width=10, anchor="w").pack(side="left")
        self.authorbioko_var = tk.StringVar(value=s.get("author_bio_ko", ""))
        tk.Entry(r, textvariable=self.authorbioko_var, font=("맑은 고딕", 8)).pack(
            side="left", fill="x", expand=True)
        r = tk.Frame(col2); r.pack(fill="x", pady=2)
        tk.Label(r, text="저자소개(영)", width=10, anchor="w").pack(side="left")
        self.authorbioen_var = tk.StringVar(value=s.get("author_bio_en", ""))
        tk.Entry(r, textvariable=self.authorbioen_var, font=("맑은 고딕", 8)).pack(
            side="left", fill="x", expand=True)
        r = tk.Frame(col2); r.pack(fill="x", pady=2)
        self.seoschema_var = tk.BooleanVar(value=s.get("seo_schema", True))
        tk.Checkbutton(r, text="구조화데이터(JSON-LD) 넣기",
                       variable=self.seoschema_var).pack(side="left")
        r = tk.Frame(col2); r.pack(fill="x", pady=2)
        tk.Label(r, text="문의 이메일", width=10, anchor="w").pack(side="left")
        self.contactemail_var = tk.StringVar(value=s.get("contact_email", ""))
        tk.Entry(r, textvariable=self.contactemail_var).pack(side="left", fill="x", expand=True)
        r = tk.Frame(col2); r.pack(fill="x", pady=2)
        tk.Label(r, text="ComfyUI 경로", width=10, anchor="w").pack(side="left")
        self.comfypath_var = tk.StringVar(value=s.get("comfy_path", ""))
        tk.Entry(r, textvariable=self.comfypath_var, font=("맑은 고딕", 8)).pack(
            side="left", fill="x", expand=True)
        tk.Button(r, text="🛑 종료", command=self.stop_comfy,
                  font=("맑은 고딕", 8)).pack(side="left", padx=(4, 0))

        # 시드 키워드 — 시리즈 기획/키워드 조사가 참고하는 목록. 무료 키워드 확장으로 채워 넣기.
        r = tk.Frame(col2); r.pack(fill="x", pady=2)
        tk.Label(r, text="시드 키워드", width=10, anchor="w").pack(side="left", anchor="n")
        kwbox = tk.Frame(r); kwbox.pack(side="left", fill="x", expand=True)
        self.seedkw_list = tk.Listbox(kwbox, height=4, font=("맑은 고딕", 8),
                                      selectmode="extended")
        self.seedkw_list.pack(side="left", fill="x", expand=True)
        for kw in s.get("seed_keywords", []):
            self.seedkw_list.insert("end", kw)
        kwbtns = tk.Frame(r); kwbtns.pack(side="left", padx=(4, 0))
        tk.Button(kwbtns, text="🔍 키워드 확장", command=self.open_keyword_research,
                  font=("맑은 고딕", 8)).pack(fill="x")
        tk.Button(kwbtns, text="🗑 선택 삭제", command=self._remove_seed_keywords,
                  font=("맑은 고딕", 8)).pack(fill="x", pady=(2, 0))

        col3 = tk.Frame(box); col3.pack(side="right", padx=8, pady=6)
        tk.Button(col3, text="설정 저장", command=self.save_settings, width=12).pack(pady=2)
        tk.Button(col3, text="사용법 보기", command=self.show_help, width=12).pack(pady=2)
        tk.Button(col3, text="📄 필수 페이지 만들기", command=self.run_static_pages,
                  width=14, font=("맑은 고딕", 9)).pack(pady=2)

    def _set_md_label(self, lbl, path):
        lbl.config(text=("📄 " + Path(path).name) if path else "없음",
                   fg=("#1565c0" if path else "#aaa"))

    def _pick_weekly_md(self, md_var, md_lbl):
        f = filedialog.askopenfilename(
            title="참고 .md 파일 선택",
            filetypes=[("Markdown/텍스트", "*.md *.markdown *.txt"), ("모든 파일", "*.*")])
        if f:
            md_var.set(f)
            self._set_md_label(md_lbl, f)

    def _clear_weekly_md(self, md_var, md_lbl):
        md_var.set("")
        self._set_md_label(md_lbl, "")

    def save_weekly(self):
        """하위 호환용 — 현재 선택된 요일을 저장(주간 탭은 이제 요일별 패널 편집)."""
        self.save_weekday()

    # ── 시리즈 기획 ──────────────────────────────────────────────────────────
    def _active_weekly_topics(self):
        out = []
        for wd in range(7):
            w = core.get_weekly(self.data, wd)
            if w["enabled"] and w["topic"].strip():
                out.append(f"{core.WEEKDAY_KO[wd]}요일: {w['topic'].strip()}")
        return out

    # ── 시리즈 카테고리(블로그별, 요일 주제 연동) ────────────────────────────
    def _refresh_categories(self, select_first=False):
        """카테고리 콤보박스를 이 블로그의 목록(요일 주제 + 추가분)으로 갱신."""
        cats = core.blog_categories(self.data)
        combo = getattr(self, "series_category_combo", None)
        if combo is not None:
            combo["values"] = cats
        cur = self.series_category_var.get().strip()
        if select_first or (cur not in cats):
            self.series_category_var.set(cats[0] if cats else "")

    def add_series_category(self):
        """입력창에 적은 주제를 이 블로그의 카테고리로 추가(영구 저장)."""
        name = self.series_category_var.get().strip()
        if not name:
            messagebox.showinfo("카테고리 추가",
                                "추가할 카테고리(주제)를 입력한 뒤 [+ 추가]를 누르세요.")
            return
        if core.add_category(self.data, name):
            core.save_schedule(self.data)
            self._refresh_categories()
            self.series_category_var.set(name)
            self._log(f"🏷️ 시리즈 카테고리 추가: {name}\n")
        else:
            messagebox.showinfo("카테고리 추가",
                                f"'{name}'은(는) 이미 카테고리 목록에 있습니다.\n"
                                "(요일별 발행 주제는 자동으로 포함됩니다.)")

    def apply_karts_now(self):
        """활성 블로그에 k-arts-now(시의성 큐레이션) 전략 프리셋을 적용."""
        if self.busy:
            messagebox.showinfo("진행 중", "작업이 끝난 뒤 적용하세요.")
            return
        if not messagebox.askyesno(
                "k-arts-now 전략 적용",
                f"현재 블로그 '{self._blog_name(self.active_blog)}'를 'k-arts-now'(시의성 큐레이션) "
                "전략으로 설정합니다.\n\n"
                "• 정체성(색깔), 6개 시기별 카테고리(Monthly Preview~Festival Watch),\n"
                "  요일 일정(월·수·금 06시), 시드 키워드를 채웁니다.\n"
                "• 기존 정체성·카테고리·해당 요일(월·수·금) 설정을 덮어씁니다.\n\n계속할까요?"):
            return
        core.apply_karts_now_preset(self.data)
        core.save_schedule(self.data)
        self._reload_for_active()
        self._log("📋 k-arts-now 전략 적용 완료 — 정체성·6카테고리·요일일정·시드 설정됨\n")
        messagebox.showinfo(
            "적용 완료",
            "k-arts-now 전략을 적용했습니다.\n"
            "[📅 요일별 발행]과 [🎬 시리즈 기획]에서 확인하세요.\n\n"
            "※ This Week·Weekend Picks 같은 시의성 글의 '실제 이벤트 내용'은\n"
            "  다음 단계(이벤트 DB)에서 자동 채워집니다.")

    # ── 관심 키워드 조사 → 선택 ──────────────────────────────────────────────
    def run_research_keywords(self):
        category = self.series_category_var.get()
        settings = self._collect_settings()
        self._researched = None

        def job():
            self._researched = core.research_keywords(category, settings,
                                                      log=self.log_q.put, progress=self._progress_cb)

        def done():
            if self._researched:
                self._show_keyword_chooser(category, self._researched)
        self._start_worker(job, f"키워드 조사 ({category})", on_done=done)

    def _show_keyword_chooser(self, category, kws):
        win = tk.Toplevel(self.root)
        win.title(f"{category} — 관심 키워드 (1~{len(kws)}위)")
        win.transient(self.root)
        win.bind("<Map>", lambda e, w=win: self._theme_walk(w, self._theme, self.dark_mode))
        win.geometry("660x500")
        tk.Label(win, text=f"[{category}] 관심도 높은 순입니다. 시리즈 중심으로 쓸 키워드를 고르세요:",
                 font=("맑은 고딕", 11, "bold")).pack(anchor="w", padx=14, pady=(14, 6))

        canvas = tk.Canvas(win, highlightthickness=0)
        sb = ttk.Scrollbar(win, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True, padx=(14, 0), pady=4)
        sb.pack(side="right", fill="y")

        var = tk.StringVar(value=(kws[0].get("keyword", "") if kws else ""))
        for i, k in enumerate(kws, 1):
            kw = k.get("keyword", "")
            en = k.get("en", "")
            note = k.get("note", "")
            label = f"{i}. {kw}"
            if en:
                label += f"  ({en})"
            if note:
                label += f"\n      → {note}"
            tk.Radiobutton(inner, text=label, variable=var, value=kw,
                           justify="left", anchor="w", font=("맑은 고딕", 10),
                           wraplength=560).pack(fill="x", anchor="w", pady=2)

        def choose():
            sel = var.get().strip()
            if sel:
                self.series_theme_var.set(sel)
                self._log(f"🔑 중심 테마 선택: {sel}\n")
            win.destroy()

        def save_to_pool():
            settings = self._collect_settings()

            def job():
                kwpool.score_researched_keywords(kws, settings, source=f"category:{category}",
                                                 log=self.log_q.put)

            def done():
                s = kwpool.pool_summary()
                messagebox.showinfo("키워드 풀 저장",
                                     f"이 조사 결과를 풀에 저장했습니다.\n"
                                     f"전체 {s['total']}개 · 대기 {s['pending']}개")
            self._start_worker(job, "키워드 풀에 저장(자동완성 신호 확인 중)", on_done=done)

        btnrow = tk.Frame(win); btnrow.pack(pady=10)
        tk.Button(btnrow, text="이 키워드로 중심 테마 설정", command=choose,
                  bg="#2e7d32", fg="white", font=("맑은 고딕", 11, "bold")).pack(side="left", padx=4)
        tk.Button(btnrow, text="📥 전체 결과 풀에 저장(나중에 쓰기)", command=save_to_pool,
                  bg="#6a1b9a", fg="white").pack(side="left", padx=4)

    def open_keyword_pool(self):
        """키워드 풀(대기열) 보기·수집·선택 — [🔎 관심 키워드 조사]로 모은 키워드를
        점수순으로 쌓아 두고, 시리즈 기획할 때 상위 점수부터 골라 쓰는 화면."""
        win = tk.Toplevel(self.root)
        win.title(f"🏆 키워드 풀 — {self._blog_name(self.active_blog)}")
        win.transient(self.root); win.geometry("720x560")
        win.bind("<Map>", lambda e, w=win: self._theme_walk(w, self._theme, self.dark_mode))

        tk.Label(win, text="LLM 관심도 순위 + 구글 자동완성 신호로 점수를 매긴 키워드 대기열입니다. "
                            "한 번 쓴 키워드는 자동으로 '사용됨'으로 표시되어 중복되지 않습니다.",
                 font=("맑은 고딕", 9), fg="#555", wraplength=680, justify="left").pack(
            anchor="w", padx=12, pady=(12, 6))

        summary_lbl = tk.Label(win, text="", font=("맑은 고딕", 9, "bold"), fg="#333")
        summary_lbl.pack(anchor="w", padx=12)

        collect_bar = tk.Frame(win); collect_bar.pack(fill="x", padx=12, pady=(8, 4))
        tk.Label(collect_bar, text="새로 수집할 카테고리:").pack(side="left")
        cat_var = tk.StringVar(value=self.series_category_var.get())
        ttk.Combobox(collect_bar, textvariable=cat_var, width=20,
                     values=core.blog_categories(self.data)).pack(side="left", padx=(4, 8))

        cols = ("keyword", "en", "score", "status", "source", "collected_at")
        tv = ttk.Treeview(win, columns=cols, show="headings", height=18, selectmode="extended")
        heads = {"keyword": "키워드", "en": "영어", "score": "점수", "status": "상태",
                  "source": "출처", "collected_at": "수집일"}
        widths = {"keyword": 150, "en": 150, "score": 55, "status": 65, "source": 130,
                   "collected_at": 85}
        for c in cols:
            tv.heading(c, text=heads[c])
            tv.column(c, width=widths[c], anchor="w")
        tv.pack(fill="both", expand=True, padx=12, pady=(4, 4))
        sb = ttk.Scrollbar(win, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set); sb.pack(side="right", fill="y")

        def refresh():
            for i in tv.get_children():
                tv.delete(i)
            pool = sorted(kwpool.load_pool(), key=lambda p: p.get("score", 0), reverse=True)
            for p in pool:
                tv.insert("", "end", iid=p["keyword"],
                          values=(p["keyword"], p.get("en", ""), p.get("score", 0),
                                  p.get("status", ""), p.get("source", ""),
                                  p.get("collected_at", "")))
            s = kwpool.pool_summary()
            summary_lbl.config(
                text=f"전체 {s['total']}개 · 대기 {s['pending']}개 · 사용됨 {s['used']}개 · "
                     f"거절 {s['rejected']}개")

        def do_collect():
            category = cat_var.get().strip()
            if not category:
                messagebox.showwarning("카테고리 없음", "수집할 카테고리를 입력하거나 고르세요.")
                return
            settings = self._collect_settings()

            def job():
                kwpool.collect_for_category(category, settings, log=self.log_q.put)

            def done():
                refresh()
            self._start_worker(job, f"키워드 풀 수집 ({category})", on_done=done)

        tk.Button(collect_bar, text="🔎 새로 수집", command=do_collect,
                  bg="#0277bd", fg="white").pack(side="left")
        tk.Button(collect_bar, text="🔄 새로고침", command=refresh).pack(side="left", padx=(6, 0))

        def choose_selected():
            sel = tv.selection()
            if not sel:
                messagebox.showinfo("선택", "테마로 쓸 키워드를 하나 선택하세요.")
                return
            kw = sel[0]
            self.series_theme_var.set(kw)
            kwpool.set_status([kw], "used")
            self._log(f"🔑 키워드 풀에서 중심 테마 선택: {kw}\n")
            refresh()
            win.destroy()

        def reject_selected():
            sel = list(tv.selection())
            if not sel:
                return
            kwpool.set_status(sel, "rejected")
            refresh()

        def restore_selected():
            sel = list(tv.selection())
            if not sel:
                return
            kwpool.set_status(sel, "pending")
            refresh()

        btns = tk.Frame(win); btns.pack(fill="x", padx=12, pady=(4, 12))
        tk.Button(btns, text="✅ 선택 키워드로 중심 테마 설정", command=choose_selected,
                  bg="#2e7d32", fg="white", font=("맑은 고딕", 10, "bold")).pack(side="left")
        tk.Button(btns, text="🗑 거절 처리", command=reject_selected).pack(side="left", padx=(6, 0))
        tk.Button(btns, text="↩ 대기로 되돌리기", command=restore_selected).pack(side="left", padx=(6, 0))
        tk.Button(btns, text="닫기", command=win.destroy).pack(side="right")

        refresh()

    def run_plan_series(self):
        try:
            count = max(3, int(self.series_count_var.get()))
        except ValueError:
            count = 5
        theme = self.series_theme_var.get().strip()
        request = self.series_request_text.get("1.0", "end").strip()
        settings = self._collect_settings()   # blog_identity 포함 저장
        anchor = self._active_weekly_topics()
        self._planned = None
        self._plan_series_stop = False
        self.btn_apply.config(state="disabled")
        if request:
            self._log(f"📝 요청사항으로 기획합니다: {request[:60]}\n")
        elif anchor:
            self._log("🧭 주간 요일별 주제와 연관지어 기획합니다: " + "; ".join(anchor) + "\n")
        if self.series_photo_dir_var.get().strip():
            self._log(f"📷 사진 폴더 반영: {self.series_photo_dir_var.get().strip()}\n")

        def job():
            self._planned = core.plan_series(
                theme, count, settings, log=self.log_q.put, progress=self._progress_cb,
                anchor_topics=anchor, request=request,
                stop_check=lambda: getattr(self, "_plan_series_stop", False),
                photo_dir=self.series_photo_dir_var.get().strip() or None)

        def done():
            if self._planned:
                self._show_plan_preview(self._planned)
        self._start_worker(job, f"시리즈 기획 ({count}편)", on_done=done)

    def stop_plan_series(self):
        """진행 중인 시리즈 기획을 중단(편수 등을 잘못 입력했을 때). 지금 진행 중인
        LLM 응답 1회는 끝까지 기다리고, 그다음 재시도부터 멈춘다."""
        self._plan_series_stop = True
        self._log("■ 시리즈 기획 중단을 요청했습니다 — 진행 중인 응답이 오면 곧 멈춥니다.\n")

    def _show_plan_preview(self, plan):
        self.last_plan = plan
        posts = plan.get("posts") or []
        lines = []
        lines.append(f"📚 시리즈: {plan.get('series_title_ko','')}")
        if plan.get("series_title_en"):
            lines.append(f"    ({plan['series_title_en']})")
        if plan.get("theme"):
            lines.append(f"🎯 테마: {plan['theme']}")
        if plan.get("keywords"):
            lines.append(f"🔑 키워드: {', '.join(plan['keywords'])}")
        lines.append("")
        for i, p in enumerate(posts, 1):
            lines.append(f"[{i}편] {p.get('title_ko','')}")
            if p.get("title_en"):
                lines.append(f"       EN: {p['title_en']}")
            if p.get("keyword"):
                lines.append(f"       키워드: {p['keyword']}")
            if p.get("hook"):
                lines.append(f"       후킹: {p['hook']}")
            if p.get("summary"):
                lines.append(f"       내용: {p['summary']}")
            lines.append("")
        text = "\n".join(lines)
        self.series_preview.config(state="normal")
        self.series_preview.delete("1.0", "end")
        self.series_preview.insert("1.0", text)
        self.series_preview.config(state="disabled")
        self.btn_apply.config(state="normal")
        self._log(f"🎬 시리즈 기획 완료 — {len(posts)}편. 확인 후 [② 캘린더에 넣기]를 누르세요.\n")

    def apply_planned_series(self):
        if not self.last_plan:
            messagebox.showwarning("기획 없음", "먼저 [① 시리즈 기획하기]로 시리즈를 만드세요.")
            return
        start = self.series_start_var.get().strip()
        try:
            start_d = datetime.strptime(start, "%Y-%m-%d").date()
        except ValueError:
            messagebox.showwarning("날짜 형식", "시작 날짜를 YYYY-MM-DD 형식으로 입력하세요.")
            return
        try:
            interval = max(1, int(self.series_interval_var.get()))
        except ValueError:
            interval = 1
        n = len(self.last_plan.get("posts") or [])
        msg = f"{start}부터 {interval}일 간격으로 {n}편을 캘린더에 배정할까요?"
        if start_d < date.today():
            msg += ("\n\n⚠️ 시작일이 과거입니다 — 자동 발행이 켜져 있으면 배정되는 즉시 "
                    "그 날짜부터 생성·발행이 시작될 수 있습니다.")
        if not messagebox.askyesno("캘린더에 넣기", msg):
            return
        sid, assigned, skipped = core.apply_series_to_calendar(
            self.data, self.last_plan, start, interval, log=self._log,
            photo_dir=self.series_photo_dir_var.get().strip() or None)
        self.refresh_calendar()
        if not assigned:
            messagebox.showwarning(
                "배정된 편 없음",
                f"모든 날짜({n}개)가 이미 발행된 글과 겹쳐서 하나도 배정하지 못했습니다.\n"
                "시작 날짜를 다른 기간으로 바꿔서 다시 시도해보세요.")
            return
        self.load_day(assigned[0])
        skip_msg = f" (이미 발행된 {len(skipped)}편은 건너뜀)" if skipped else ""
        self._log(f"✅ 시리즈를 캘린더에 배정했습니다: {assigned[0]} ~ {assigned[-1]} "
                  f"({len(assigned)}편){skip_msg}\n")
        info = (f"{len(assigned)}편을 캘린더에 넣었습니다.\n"
               f"{assigned[0]} ~ {assigned[-1]}\n")
        if skipped:
            info += f"\n⏭ 이미 발행된 날짜 {len(skipped)}개는 건너뛰었습니다(덮어쓰지 않음).\n"
        info += "\n[🗓 날짜별 발행] 탭에서 확인하고, 생성/발행하거나 자동 발행에 맡기세요."
        messagebox.showinfo("배정 완료", info)

    # ── 캘린더 (월간/주간/일간) ───────────────────────────────────────────────
    def set_view_mode(self):
        self.view_mode = self.view_mode_var.get()
        if self.view_mode == "month":
            self._weekhead.pack(fill="x", pady=(6, 2), before=self.grid_frame)
        else:
            self._weekhead.pack_forget()
        self.refresh_calendar()

    def nav_prev(self):
        if self.view_mode == "month":
            self.prev_month()
        else:
            self._shift_days(-7 if self.view_mode == "week" else -1)

    def nav_next(self):
        if self.view_mode == "month":
            self.next_month()
        else:
            self._shift_days(7 if self.view_mode == "week" else 1)

    def _shift_days(self, n):
        d = datetime.strptime(self.selected_date, "%Y-%m-%d").date() + timedelta(days=n)
        self.load_day(d.isoformat())

    def _day_posts_display(self, ds):
        """그 날짜에 표시할 글 목록: 개별 글(시간순) + (없으면) 주간 템플릿 1건."""
        out = []
        for k in core.day_keys(self.data, ds):
            e = self.data["entries"].get(k) or {}
            if not e.get("topic"):
                continue
            out.append({"key": k, "time": e.get("time", "") or "--:--",
                        "topic": e.get("topic", ""), "status": e.get("status"), "origin": "date"})
        if not out:
            w = core.weekly_for_date(self.data, ds)
            if w:
                out.append({"key": ds, "time": w.get("time", "") or "--:--",
                            "topic": w.get("topic", ""), "status": core.ST_PENDING, "origin": "weekly"})
        out.sort(key=lambda r: r["time"])
        return out

    def refresh_calendar(self):
        for w in self.grid_frame.winfo_children():
            w.destroy()
        self.day_buttons.clear()
        if self.view_mode == "week":
            self._render_week()
        elif self.view_mode == "day":
            self._render_day()
        else:
            self._render_month()
        # 새로 만든 캘린더 위젯에도 현재 테마 적용
        if getattr(self, "_theme", None):
            self._theme_walk(self.grid_frame, self._theme, self.dark_mode)

    def _render_week(self):
        base = datetime.strptime(self.selected_date, "%Y-%m-%d").date()
        sun = base - timedelta(days=(base.weekday() + 1) % 7)
        self.month_lbl.config(text=f"주간: {sun.month}/{sun.day} ~ {(sun + timedelta(days=6)).month}/{(sun + timedelta(days=6)).day}")
        today_str = date.today().isoformat()
        for i in range(7):
            d = sun + timedelta(days=i); ds = d.isoformat()
            fg = "#d32f2f" if d.weekday() == 6 else ("#1565c0" if d.weekday() == 5 else "#222")
            row = tk.Frame(self.grid_frame, relief="solid", bd=1,
                           bg=("#fff8e1" if ds == today_str else "#ffffff"))
            row.pack(fill="x", padx=2, pady=1)
            hdr = tk.Label(row, text=f"{d.month}/{d.day}\n({core.WEEKDAY_KO[d.weekday()]})",
                           width=7, fg=fg, font=("맑은 고딕", 10, "bold"),
                           bg=row["bg"]); hdr.pack(side="left", padx=2)
            body = tk.Frame(row, bg=row["bg"]); body.pack(side="left", fill="x", expand=True)
            for p in self._day_posts_display(ds):
                bg = "#fff3cd" if p["origin"] == "weekly" else COLORS.get(p["status"], "#eee")
                txt = f"{p['time']}  {p['topic'][:28]}" + ("  ↻" if p["origin"] == "weekly" else "")
                tk.Button(body, text=txt, bg=bg, anchor="w", relief="flat",
                          font=("맑은 고딕", 9), command=lambda k=p["key"]: self.load_day(k)
                          ).pack(fill="x", padx=2, pady=1)
            tk.Button(body, text="＋ 글 추가", fg="#1565c0", relief="flat", bg=row["bg"],
                      font=("맑은 고딕", 8), command=lambda dd=ds: self._add_post_on(dd)
                      ).pack(anchor="w", padx=2)

    def _render_day(self):
        ds = self.selected_date
        d = datetime.strptime(ds, "%Y-%m-%d").date()
        self.month_lbl.config(text=f"일간: {ds} ({core.WEEKDAY_KO[d.weekday()]})")
        posts = self._day_posts_display(ds)
        if not posts:
            tk.Label(self.grid_frame, text="이 날짜에 글이 없습니다.\n아래 [＋ 이 날짜에 글 추가]로 시작하세요.",
                     fg="#888", font=("맑은 고딕", 11), justify="left").pack(anchor="w", padx=6, pady=20)
        for p in posts:
            bg = "#fff3cd" if p["origin"] == "weekly" else COLORS.get(p["status"], "#eee")
            card = tk.Frame(self.grid_frame, bg=bg, relief="solid", bd=1)
            card.pack(fill="x", padx=4, pady=3)
            tk.Label(card, text=p["time"], bg=bg, font=("맑은 고딕", 12, "bold"),
                     width=8).pack(side="left", padx=6, pady=10)
            tk.Label(card, text=p["topic"][:46] + ("  ↻템플릿" if p["origin"] == "weekly" else ""),
                     bg=bg, anchor="w", font=("맑은 고딕", 11)).pack(side="left", fill="x", expand=True)
            tk.Label(card, text=STATUS_KO.get(p["status"], ""), bg=bg, fg="#555",
                     font=("맑은 고딕", 9)).pack(side="left", padx=6)
            tk.Button(card, text="열기", command=lambda k=p["key"]: self.load_day(k)).pack(side="right", padx=6)
        tk.Button(self.grid_frame, text="＋ 이 날짜에 글 추가", bg="#1565c0", fg="white",
                  font=("맑은 고딕", 10, "bold"),
                  command=lambda: self._add_post_on(ds)).pack(anchor="w", padx=4, pady=10)

    def _add_post_on(self, ds):
        self.selected_date = ds
        self.add_post()

    def _render_month(self):
        self.month_lbl.config(text=f"{self.view_year}년 {self.view_month}월")

        cal = calendar.Calendar(firstweekday=6)  # 일요일 시작
        weeks = cal.monthdayscalendar(self.view_year, self.view_month)
        today_str = date.today().isoformat()

        for r, week in enumerate(weeks):
            self.grid_frame.rowconfigure(r, weight=1)
            for c, day in enumerate(week):
                self.grid_frame.columnconfigure(c, weight=1)
                if day == 0:
                    tk.Frame(self.grid_frame).grid(row=r, column=c, sticky="nsew")
                    continue
                ds = f"{self.view_year:04d}-{self.view_month:02d}-{day:02d}"
                p = core.planned(self.data, ds)
                status, origin = p["status"], p["origin"]
                palette = COLORS_DARK if self.dark_mode else COLORS
                weekly_bg = "#3a3220" if self.dark_mode else "#fff3cd"
                if status == core.ST_PENDING and origin == "weekly":
                    bg = weekly_bg
                elif status:
                    bg = palette.get(status, palette["none"])
                else:
                    bg = palette["none"]
                prefix = "↻ " if (origin == "weekly" and status == core.ST_PENDING) else ""
                topic = p["topic"][:14]
                entry = self.data["entries"].get(ds) or {}
                ser = entry.get("series") or {}
                badge = f"📚{ser['index']}/{ser['total']} " if ser else ""
                # 이 날짜의 개별 글 수(여러 개면 배지로 표시)
                nposts = len([k for k in core.day_keys(self.data, ds)
                              if (self.data["entries"].get(k) or {}).get("topic")])
                multi = f"📝{nposts} " if nposts > 1 else ""

                if self.dark_mode:
                    fg = "#ef5350" if c == 0 else ("#4499ff" if c == 6 else "#eeeeee")
                else:
                    fg = "#d32f2f" if c == 0 else ("#1565c0" if c == 6 else "#222")
                txt = f"{day}"
                if topic:
                    txt += f"\n{multi}{badge}{prefix}{topic}"
                    if nposts > 1:
                        txt += f" 외 {nposts - 1}"
                bd_color = "#2a2a2e" if self.dark_mode else "#dddddd"
                b = tk.Button(
                    self.grid_frame, text=txt, bg=bg, fg=fg,
                    font=("맑은 고딕", 9), anchor="n", justify="left",
                    relief="flat", bd=0, wraplength=110,
                    highlightthickness=1, highlightbackground=bd_color,
                    activebackground=bg, activeforeground=fg)
                b.bind("<Button-1>", lambda e, d=ds: self.load_day(d))
                b.bind("<Control-Button-1>", lambda e, d=ds: self._toggle_multi_selected_day(d))
                if ds == today_str:
                    b.config(highlightthickness=3, highlightbackground="#ff5722",
                             highlightcolor="#ff5722")
                if ds == getattr(self, "selected_date", self.selected):
                    b.config(relief="sunken", bd=3)
                if ds in self.multi_selected_dates:
                    b.config(highlightthickness=4, highlightbackground="#2962ff",
                             highlightcolor="#2962ff")
                b.grid(row=r, column=c, sticky="nsew", padx=1, pady=1)
                self.day_buttons[ds] = b

    def prev_month(self):
        self.view_month -= 1
        if self.view_month < 1:
            self.view_month = 12; self.view_year -= 1
        self.refresh_calendar()

    def next_month(self):
        self.view_month += 1
        if self.view_month > 12:
            self.view_month = 1; self.view_year += 1
        self.refresh_calendar()

    def go_today(self):
        t = date.today()
        self.view_year, self.view_month = t.year, t.month
        self.refresh_calendar()
        self.load_day(t.isoformat())

    # ── 날짜 선택/편집 ────────────────────────────────────────────────────────
    def load_day(self, date_str):
        """날짜 선택 — 그 날짜의 글 목록을 채우고 활성 글을 편집기에 로드."""
        self.selected_date = core.post_date(date_str)
        y, m, _ = map(int, self.selected_date.split("-"))
        if (y, m) != (self.view_year, self.view_month):
            self.view_year, self.view_month = y, m
        keys = core.day_keys(self.data, self.selected_date) or [self.selected_date]
        if date_str in keys:
            self.selected = date_str
        elif self.selected not in keys:
            self.selected = keys[0]
        self._populate_post_selector(keys)
        self.refresh_calendar()
        self.load_post(self.selected)

    def _populate_post_selector(self, keys):
        labels = []
        for k in keys:
            e = self.data["entries"].get(k) or {}
            t = (e.get("time", "") or "--:--")
            tp = (e.get("topic", "") or core.planned(self.data, k)["topic"] or "(주제 없음)")[:16]
            labels.append(f"{t} · {tp}")
        self._post_keys = keys
        self.post_sel["values"] = labels
        if self.selected in keys:
            self.post_sel.current(keys.index(self.selected))
        elif labels:
            self.post_sel.current(0)

    def _on_post_selected(self, _evt=None):
        i = self.post_sel.current()
        if 0 <= i < len(getattr(self, "_post_keys", [])):
            self.load_post(self._post_keys[i])

    def add_post(self):
        """이 날짜에 새 글 항목 추가(시간대는 서로 달라야 함)."""
        dd = getattr(self, "selected_date", None) or core.post_date(self.selected)
        key = core.new_post_key(self.data, dd)
        used = core.day_times(self.data, dd)
        default = next((f"{h:02d}:00" for h in (9, 12, 15, 18, 21, 7, 20)
                        if f"{h:02d}:00" not in used), "09:00")
        core.set_topic(self.data, key, "", "", default)   # 빈 글 자리(시간 지정으로 유지)
        core.save_schedule(self.data)
        self.selected = key
        self.load_day(key)
        self._log(f"➕ {dd} 에 새 글 추가({default}) — 주제와 시각을 입력하고 [이 날짜 저장]\n")

    def _toggle_multi_selected_day(self, date_str):
        if date_str in self.multi_selected_dates:
            self.multi_selected_dates.discard(date_str)
        else:
            self.multi_selected_dates.add(date_str)
        n = len(self.multi_selected_dates)
        self.multisel_lbl.config(text=f"{n}개 선택됨" if n else "")
        self.refresh_calendar()

    def clear_multi_selected_days(self):
        self.multi_selected_dates.clear()
        self.multisel_lbl.config(text="")
        self.refresh_calendar()

    def delete_multi_selected_days(self):
        """캘린더에서 Ctrl+클릭으로 선택한 여러 날짜의 글을 한번에 삭제(생성 캐시 포함).
        선택 범위에 발행된 글이 섞여 있으면 블로그도 지울지 물어본다."""
        dates = sorted(self.multi_selected_dates)
        if not dates:
            messagebox.showinfo("삭제", "Ctrl+클릭으로 지울 날짜를 하나 이상 선택하세요.")
            return
        keys = []
        for ds in dates:
            keys.extend(k for k in core.day_keys(self.data, ds)
                       if (self.data["entries"].get(k) or {}).get("topic"))
        if not keys:
            messagebox.showinfo("삭제", "선택한 날짜에 지울 글이 없습니다.")
            self.clear_multi_selected_days()
            return
        published_keys = [k for k in keys
                          if (self.data["entries"].get(k) or {}).get("status") == core.ST_PUBLISHED]

        if published_keys:
            ans = messagebox.askyesnocancel(
                "선택 날짜 삭제",
                f"선택한 {len(dates)}개 날짜, 총 {len(keys)}편(발행됨 {len(published_keys)}편 포함)을 "
                "삭제합니다.\n\n"
                "[예]      → 블로그 + 스케줄 모두 삭제\n"
                "[아니오] → 스케줄(캘린더)만 삭제 (블로그 글은 유지)\n"
                "[취소]   → 아무것도 하지 않음")
            if ans is None:
                return
            delete_from_blog = ans
        else:
            if not messagebox.askyesno(
                    "선택 날짜 삭제",
                    f"선택한 {len(dates)}개 날짜, 총 {len(keys)}편을 스케줄에서 삭제할까요?\n"
                    "(생성된 초안 캐시도 함께 삭제되어, 다음엔 새로 생성됩니다)"):
                return
            delete_from_blog = False

        if delete_from_blog:
            self._set_buttons(False)
            entries_to_del = {k: (self.data["entries"].get(k) or {}) for k in published_keys}

            def job():
                for key, e in entries_to_del.items():
                    self._log(f"🗑 블로그 삭제 중: {key}\n")
                    try:
                        res = core.delete_blog_posts(e, log=self._log)
                        failed = [l for l, s in res.items() if s == "fail"]
                        if failed:
                            self._log(f"   ⚠️ {key} 일부 삭제 실패: {failed}\n")
                    except Exception as ex:
                        self._log(f"   ❌ {key} 오류: {ex}\n")
                self.root.after(0, _finish)

            def _finish():
                for key in keys:
                    self.data["entries"].pop(key, None)
                    core.delete_generated(core.post_date(key))
                core.save_schedule(self.data)
                self.multi_selected_dates.clear()
                self.multisel_lbl.config(text="")
                self.refresh_calendar()
                self.load_day(self.selected_date)
                self._set_buttons(True)
                self._log(f"✅ {len(dates)}개 날짜({len(keys)}편) 삭제 완료\n")

            threading.Thread(target=job, daemon=True).start()
        else:
            for key in keys:
                self.data["entries"].pop(key, None)
                core.delete_generated(core.post_date(key))
            core.save_schedule(self.data)
            self.multi_selected_dates.clear()
            self.multisel_lbl.config(text="")
            self.refresh_calendar()
            self.load_day(self.selected_date)
            self._log(f"🗑 {len(dates)}개 날짜({len(keys)}편)를 스케줄에서 삭제(생성 캐시 포함)\n")

    def delete_post(self):
        key = self.selected
        e = self.data["entries"].get(key) or {}
        published = e.get("status") == core.ST_PUBLISHED
        has_url = bool(e.get("en_url") or e.get("ko_url"))

        if published and has_url:
            # [예] = 블로그+스케줄 모두 삭제  [아니오] = 스케줄만  [취소] = 중단
            ans = messagebox.askyesnocancel(
                "글 삭제",
                f"발행된 글입니다: {key}\n\n"
                "[예]      → 블로그 + 스케줄 모두 삭제\n"
                "[아니오] → 스케줄만 삭제 (블로그 글 유지)\n"
                "[취소]   → 아무것도 하지 않음")
            if ans is None:      # 취소
                return
            delete_from_blog = (ans is True)
        else:
            if not messagebox.askyesno("글 삭제", f"이 글 항목을 스케줄에서 삭제할까요?\n{key}"):
                return
            delete_from_blog = False

        if delete_from_blog:
            self._log(f"🗑 블로그 글 삭제 중: {key}\n")
            self._set_buttons(False)

            def job():
                try:
                    res = core.delete_blog_posts(e, log=self._log)
                    failed = [l for l, s in res.items() if s == "fail"]
                    if failed:
                        self._log(f"   ⚠️ 일부 삭제 실패: {failed} — 블로그에서 직접 확인하세요.\n")
                except Exception as ex:
                    self._log(f"   ❌ 블로그 삭제 오류: {ex}\n")
                finally:
                    self.root.after(0, _finish)

            def _finish():
                self.data["entries"].pop(key, None)
                core.delete_generated(core.post_date(key))   # 생성 캐시도 삭제 → 다음엔 새로 생성
                core.save_schedule(self.data)
                self.refresh_calendar()
                self.load_day(self.selected_date)
                self._set_buttons(True)
                self._log(f"✅ 삭제 완료(생성 캐시 포함): {key}\n")

            threading.Thread(target=job, daemon=True).start()
        else:
            self.data["entries"].pop(key, None)
            core.delete_generated(core.post_date(key))   # 생성 캐시도 삭제 → 다음엔 새로 생성
            core.save_schedule(self.data)
            self.refresh_calendar()
            self.load_day(self.selected_date)
            self._log(f"🗑 스케줄에서 글 삭제(생성 캐시 포함): {key}\n")

    def load_post(self, key):
        """특정 글(키)을 편집기에 로드."""
        self.selected = key
        ds = core.post_date(key)
        d = datetime.strptime(ds, "%Y-%m-%d")
        wd_ko = core.WEEKDAY_KO[d.weekday()]
        p = core.planned(self.data, key)
        e = self.data["entries"].get(key) or {}
        seq = f"  ·  {key.split('#')[1]}번째 글" if "#" in key else ""
        self.sel_date_lbl.config(text=f"{ds} ({wd_ko}){seq}")
        self.topic_entry.delete(0, "end")
        self.topic_entry.insert(0, p["topic"])
        self.refs_text.delete("1.0", "end")
        self.refs_text.insert("1.0", p["refs"])
        self.dtime_var.set(e.get("time", ""))
        self.photo_dir_var.set(e.get("photo_dir", ""))
        self._refresh_photo_count()

        if p["origin"] == "weekly":
            self.origin_lbl.config(
                text=f"↻ {wd_ko}요일 주간 템플릿 적용 중 — 고치면 이 날짜만 따로 저장됩니다",
                fg="#ef6c00")
        elif p["origin"] == "date":
            self.origin_lbl.config(text="● 이 날짜에 개별 지정됨",
                                   fg=getattr(self, "_theme", {}).get("accent", "#1565c0"))
        else:
            self.origin_lbl.config(text="주제 없음 — 입력 후 [이 날짜 저장]", fg="#888")

        status = p["status"]
        self.sel_status_lbl.config(
            text="상태: " + (STATUS_KO.get(status, "주제 없음") if status else "주제 없음"))
        urls = []
        if e.get("ko_url"): urls.append("🇰🇷 " + e["ko_url"])
        if e.get("en_url"): urls.append("🇺🇸 " + e["en_url"])
        self.url_lbl.config(text="\n".join(urls))

    def save_topic(self):
        key = self.selected
        dd = core.post_date(key)
        topic = self.topic_entry.get().strip()
        refs = self.refs_text.get("1.0", "end").strip()
        t = self.dtime_var.get().strip()
        if t and not re.match(r"^([01]?\d|2[0-3]):[0-5]\d$", t):
            messagebox.showwarning("시각 형식", "발행 시각을 HH:MM 형식(예: 07:00, 14:30)으로 입력하세요.")
            return
        multi = len(core.day_keys(self.data, dd)) > 1 or "#" in key
        if topic and multi and not t:
            messagebox.showwarning("발행 시각 필요",
                                   "이 날짜에 글이 여러 개라 발행 시각을 꼭 정해야 합니다(서로 다른 시각).")
            return
        if t and t in core.day_times(self.data, dd, exclude_key=key):
            messagebox.showwarning("시각 중복",
                                   f"{t} 에 이미 다른 글이 있습니다.\n다른 시각으로 정하세요(하루 여러 글은 시간대가 달라야 합니다).")
            return
        core.set_topic(self.data, key, topic, refs, t)
        core.set_photo_dir(self.data, key, self.photo_dir_var.get().strip())
        core.save_schedule(self.data)
        self.refresh_calendar()
        self.load_day(key)
        self._log(f"📌 {key} 저장: {topic or '(삭제)'}\n")

    def pick_photo_dir(self):
        d = filedialog.askdirectory(title="사진/이미지 폴더 선택")
        if d:
            self.photo_dir_var.set(d)
            self._refresh_photo_count()

    def pick_series_photo_dir(self):
        d = filedialog.askdirectory(title="시리즈에 쓸 사진 폴더 선택")
        if d:
            self.series_photo_dir_var.set(d)

    def clear_series_photo_dir(self):
        self.series_photo_dir_var.set("")

    def pick_series_photo_from_library(self):
        """'내 사진'에 이미 등록(스캔)된 사진들 중에서 검색어로 찾거나, 등록된 폴더 트리를
        훑어서 시리즈 사진 폴더로 지정. 검색은 태그·장소·폴더명 등으로 매칭된 사진을 폴더별로
        묶어 보여줘, 직접 경로를 몰라도 어느 폴더를 쓸지 바로 고를 수 있게 한다."""
        win = tk.Toplevel(self.root)
        win.title("📚 내 사진에서 시리즈 폴더 찾기")
        win.transient(self.root); win.geometry("640x540")
        win.bind("<Map>", lambda e, w=win: self._theme_walk(w, self._theme, self.dark_mode))

        tk.Label(win, text="검색어(장소·태그 등)로 등록된 사진을 찾거나, 아래 '폴더 트리' "
                            "탭에서 직접 골라 선택하세요. 하위 폴더가 여러 개인 상위 폴더를 "
                            "골라도 됩니다(편마다 하위 폴더가 자동으로 하나씩 나뉘어 배정됨).",
                 font=("맑은 고딕", 9), fg="#555", wraplength=600, justify="left").pack(
            anchor="w", padx=12, pady=(12, 6))

        sbar = tk.Frame(win); sbar.pack(fill="x", padx=12)
        tk.Label(sbar, text="검색:").pack(side="left")
        qvar = tk.StringVar()
        ent = tk.Entry(sbar, textvariable=qvar)
        ent.pack(side="left", fill="x", expand=True, padx=(4, 4))
        tk.Button(sbar, text="🔍 검색", command=lambda: do_search()).pack(side="left")
        ent.bind("<Return>", lambda e: do_search())

        nb = ttk.Notebook(win); nb.pack(fill="both", expand=True, padx=12, pady=(8, 4))

        res_tab = tk.Frame(nb); nb.add(res_tab, text="검색 결과")
        res_tv = ttk.Treeview(res_tab, columns=("count",), show="tree headings", height=16)
        res_tv.heading("#0", text="폴더")
        res_tv.heading("count", text="매칭 사진 수")
        res_tv.column("count", width=100, anchor="center")
        res_tv.pack(fill="both", expand=True, side="left")
        res_sb = ttk.Scrollbar(res_tab, orient="vertical", command=res_tv.yview)
        res_tv.configure(yscrollcommand=res_sb.set); res_sb.pack(side="left", fill="y")

        def do_search():
            for i in res_tv.get_children():
                res_tv.delete(i)
            q = qvar.get().strip()
            if not q:
                return
            try:
                rows = photolib.search(q, n=300)
            except Exception as e:
                self._log(f"⚠️ 사진 검색 실패: {e}\n")
                return
            groups = {}
            for r in rows:
                folder = str(Path(r["path"]).parent)
                groups[folder] = groups.get(folder, 0) + 1
            if not groups:
                res_tv.insert("", "end", iid="__none__", text="(매칭 결과 없음)", values=("",))
                return
            for folder, n in sorted(groups.items(), key=lambda kv: -kv[1]):
                res_tv.insert("", "end", iid=folder, text=folder, values=(n,))

        tree_tab = tk.Frame(nb); nb.add(tree_tab, text="폴더 트리")
        tree_tv = ttk.Treeview(tree_tab, show="tree", height=16)
        tree_tv.pack(fill="both", expand=True, side="left")
        tree_sb = ttk.Scrollbar(tree_tab, orient="vertical", command=tree_tv.yview)
        tree_tv.configure(yscrollcommand=tree_sb.set); tree_sb.pack(side="left", fill="y")

        def build_tree():
            for i in tree_tv.get_children():
                tree_tv.delete(i)
            try:
                nodes = photolib.folder_tree()
            except Exception as e:
                self._log(f"⚠️ 폴더 트리 조회 실패: {e}\n")
                nodes = []

            def add(parent_iid, node):
                iid = node["path"]
                if tree_tv.exists(iid):
                    return
                tree_tv.insert(parent_iid, "end", iid=iid,
                                text=f"{node['name']}  ({node['count']}장)", open=False)
                for c in node["children"]:
                    add(iid, c)
            for n in nodes:
                add("", n)
            if not nodes:
                tree_tv.insert("", "end", iid="__none__", text="(등록된 사진 폴더가 없습니다 — "
                                "'내 사진' 탭에서 먼저 폴더를 추가·스캔하세요)")

        build_tree()

        picked = {"path": None}

        def use_selected():
            tabs = nb.tabs()
            cur_tv = res_tv if nb.select() == tabs[0] else tree_tv
            sel = cur_tv.selection()
            if not sel or sel[0] == "__none__":
                messagebox.showinfo("선택", "폴더를 하나 선택하세요.")
                return
            picked["path"] = sel[0]
            win.destroy()

        res_tv.bind("<Double-Button-1>", lambda e: use_selected())
        tree_tv.bind("<Double-Button-1>", lambda e: use_selected())

        btns = tk.Frame(win); btns.pack(fill="x", padx=12, pady=(0, 12))
        tk.Button(btns, text="✅ 이 폴더 사용", command=use_selected,
                  bg="#2e7d32", fg="white", font=("맑은 고딕", 10, "bold")).pack(side="left")
        tk.Button(btns, text="취소", command=win.destroy).pack(side="right")

        win.wait_window()
        if picked["path"]:
            self.series_photo_dir_var.set(picked["path"])
            self._log(f"📷 내 사진에서 폴더 선택: {picked['path']}\n")

    def clear_photo_dir(self):
        self.photo_dir_var.set("")
        self._refresh_photo_count()

    def _refresh_photo_count(self):
        pd = self.photo_dir_var.get().strip() or None
        auto = self.data["settings"].get("auto_date_photos", False)
        try:
            n = len(core.resolve_photos(self.selected, pd, auto))
        except Exception:
            n = 0
        if pd:
            self.photo_cnt_lbl.config(text=f"📷 이 폴더에서 사진 {n}장 발견")
        elif n:
            self.photo_cnt_lbl.config(text=f"📷 날짜 폴더에서 사진 {n}장 자동 발견")
        else:
            self.photo_cnt_lbl.config(text="📷 사진 없음 — 폴더를 직접 지정하세요(자동 사진 없음)")

    def stop_current_job(self):
        """지금 진행 중인 작업(수동 생성/발행, 자동발행, 묶음발행, 시리즈 기획 등)을 중단.
        generate_post/publish_date/plan_series가 안전한 지점마다 확인해서 멈춘다 —
        이미 시작된 LLM 응답 1회나 '기존 글 삭제→재발행' 같은 원자적 구간은 끝까지 진행된
        뒤 멈추므로, 버튼을 눌러도 몇 초~수십 초 정도는 걸릴 수 있다."""
        if not self.busy:
            messagebox.showinfo("중단", "지금 진행 중인 작업이 없습니다.")
            return
        self.stop_requested = True
        self._batch_stop = True          # 묶음 생성·발행 큐도 같이 중단
        self._plan_series_stop = True    # 시리즈 기획 재시도도 같이 중단
        self._log("🛑 중단을 요청했습니다 — 안전한 지점에서 곧 멈춥니다(사진 업로드·재발행 "
                  "중이면 그 단계는 끝까지 진행 후 멈춤).\n")

    # ── 작업 실행 (스레드) ────────────────────────────────────────────────────
    def _start_worker(self, fn, label, on_done=None):
        if not self.busy_lock.acquire(blocking=False):
            messagebox.showinfo("진행 중", "다른 작업이 진행 중입니다. 잠시만 기다려 주세요.")
            return False
        self.busy = True
        self.stop_requested = False   # 새 작업 시작 — 이전 중단 요청이 새 작업에 안 걸리게 리셋
        self._on_done = on_done
        self._set_buttons(False)
        self._set_progress(0, f"{label} — 시작")
        self._log(f"\n{'='*50}\n▶ {label}\n{'='*50}\n")

        def runner():
            old_stdout = sys.stdout
            sys.stdout = QueueWriter(self.log_q)
            try:
                fn()
            except Exception as e:
                self.log_q.put(f"\n❌ 오류: {e}\n")
                # 오류 상태 기록
                entry = self.data["entries"].get(self.selected)
                if entry:
                    entry["status"] = core.ST_ERROR
                    core.save_schedule(self.data)
            finally:
                sys.stdout = old_stdout
                self.busy = False
                self.busy_lock.release()
                self.root.after(0, self._after_worker)

        threading.Thread(target=runner, daemon=True).start()
        return True

    def _after_worker(self):
        self._set_buttons(True)
        self.data = core.load_schedule()
        self.refresh_calendar()
        self.load_day(self.selected)
        # 진행률은 마지막 단계(100% 또는 오류)를 잠깐 보여준 뒤 대기로 전환
        self.root.after(4000, self._set_idle)
        cb = getattr(self, "_on_done", None)
        self._on_done = None
        if cb:
            try:
                cb()
            except Exception as e:
                self._log(f"후처리 오류: {e}\n")

    def _set_buttons(self, enabled):
        st = "normal" if enabled else "disabled"
        btns = [self.btn_gen, self.btn_pub]
        for name in ("btn_plan", "btn_research"):
            b = getattr(self, name, None)
            if b:
                btns.append(b)
        for b in btns:
            b.config(state=st)

    def run_generate(self):
        ds = self.selected
        topic = self.topic_entry.get().strip()
        refs = self.refs_text.get("1.0", "end").strip()
        if not topic:
            messagebox.showwarning("주제 없음", "먼저 발행 주제를 입력하세요.")
            return
        core.set_topic(self.data, ds, topic, refs, self.dtime_var.get().strip())
        core.set_photo_dir(self.data, ds, self.photo_dir_var.get().strip())
        core.save_schedule(self.data)
        settings = self._collect_settings()

        entry0 = self.data["entries"].get(ds) or {}
        series_ctx = entry0.get("series") or None
        photo_dir = entry0.get("photo_dir") or None
        # 작성 방향 + 첨부 참고문서(.md, 주간 템플릿) 내용을 합쳐 전달
        refs = core.combine_refs(refs, core.planned(self.data, ds).get("md_file"))

        def job():
            core.generate_post(ds, topic, settings, log=self.log_q.put, refs=refs,
                               progress=self._progress_cb, series_ctx=series_ctx,
                               photo_dir=photo_dir, data=self.data,
                               stop_check=lambda: self.stop_requested)
            entry = self.data["entries"].setdefault(ds, {"topic": topic})
            entry["status"] = core.ST_GENERATED
            core.save_schedule(self.data)
        self._start_worker(job, f"{ds} 글 생성")

    def run_publish(self):
        ds = self.selected
        topic = self.topic_entry.get().strip()
        if not topic:
            messagebox.showwarning("주제 없음", "먼저 발행 주제를 입력하고 저장하세요.")
            return
        if not messagebox.askyesno("발행 확인",
                                   f"{ds} 글을 지금 블로그에 발행할까요?\n주제: {topic}"):
            return
        refs = self.refs_text.get("1.0", "end").strip()
        core.set_topic(self.data, ds, topic, refs, self.dtime_var.get().strip())
        core.set_photo_dir(self.data, ds, self.photo_dir_var.get().strip())
        core.save_schedule(self.data)
        settings = self._collect_settings()

        def job():
            try:
                core.publish_date(ds, settings, self.data, log=self.log_q.put,
                                  progress=self._progress_cb,
                                  stop_check=lambda: self.stop_requested)
            except Exception as ex:
                if self._handle_quota_error(ex, f"{ds} 발행 중"):
                    return                 # 할당량 소진 — 즉시 중단(재시도 안 함)
                raise
        self._start_worker(job, f"{ds} 발행")

    def preview(self):
        ds = self.selected
        cfg = core.load_generated(ds)
        if not cfg:
            messagebox.showinfo("미리보기 없음",
                                "아직 생성된 글이 없습니다. [지금 생성]을 먼저 누르세요.")
            return
        loc = cfg.get("location", "")
        ko_lbl = ", ".join(cfg.get("ko_labels", []))
        en_lbl = ", ".join(cfg.get("en_labels", []))
        meta = (f"<p style='color:#888'>미리보기 — {ds} · {cfg.get('topic','')}"
                + (f" · 📍 {loc}" if loc else "") + "</p>"
                + f"<p style='color:#0277bd;font-size:13px'>🏷 라벨(KO): {ko_lbl}<br>🏷 Labels(EN): {en_lbl}</p>")
        html = (f"<html><head><meta charset='utf-8'><title>{cfg.get('ko_title','')}</title>"
                f"<style>body{{font-family:'맑은 고딕';max-width:760px;margin:40px auto;"
                f"line-height:1.7;padding:0 16px}}h1{{border-bottom:2px solid #eee}}</style></head><body>"
                f"{meta}"
                f"<h1>🇰🇷 {cfg.get('ko_title','')}</h1>{cfg.get('body_ko','')}"
                f"<hr style='margin:40px 0'>"
                f"<h1>🇺🇸 {cfg.get('en_title','')}</h1>{cfg.get('body_en','')}</body></html>")
        out = core.GENERATED_DIR / ds / "preview.html"
        out.write_text(html, encoding="utf-8")
        webbrowser.open(out.as_uri())
        self._log(f"👁 미리보기 열기: {out}\n")

    # ── 이미지 찾기 (무료·저작권 안전) ───────────────────────────────────────
    def run_find_images(self):
        cfg = core.load_generated(self.selected)
        if not cfg:
            messagebox.showinfo(
                "이미지 찾기",
                "먼저 [✍ 지금 생성]으로 글을 만든 뒤,\n이미지를 찾아 넣을 수 있습니다.")
            return
        self._open_image_dialog(cfg)

    def _open_image_dialog(self, cfg):
        win = tk.Toplevel(self.root)
        win.title("이미지 찾기 — 무료·저작권 안전 (Openverse·Wikimedia·LOC·미술관 등)")
        win.geometry("780x680"); win.transient(self.root)
        win.bind("<Map>", lambda e, w=win: self._theme_walk(w, self._theme, self.dark_mode))

        top = tk.Frame(win); top.pack(fill="x", padx=12, pady=(12, 4))
        tk.Label(top, text="검색어(영어 권장):", font=("맑은 고딕", 10)).pack(side="left")
        qvar = tk.StringVar(value=(cfg.get("en_title") or cfg.get("topic") or "").strip())
        ent = tk.Entry(top, textvariable=qvar, font=("맑은 고딕", 10))
        ent.pack(side="left", fill="x", expand=True, padx=6)
        tk.Button(top, text="🔎 검색", command=lambda: self._do_img_search(qvar),
                  bg="#0277bd", fg="white").pack(side="left")
        tk.Label(win, text="※ 한국 관련성 자동 보강·필터(외국 그림·일본/중국 작품 제외).  "
                           "썸네일을 클릭하면 큰 미리보기가 팝업 안에서 열립니다.",
                 fg="#888", font=("맑은 고딕", 8)).pack(anchor="w", padx=12)

        mid = tk.Frame(win); mid.pack(fill="both", expand=True, padx=(12, 0), pady=6)
        canvas = tk.Canvas(mid, highlightthickness=0)
        sb = ttk.Scrollbar(mid, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        bottom = tk.Frame(win); bottom.pack(fill="x", padx=12, pady=(0, 10))
        tk.Button(bottom, text="✅ 선택한 이미지 글에 넣기", command=self._insert_selected_images,
                  bg="#2e7d32", fg="white", font=("맑은 고딕", 10, "bold")).pack(side="left")
        tk.Button(bottom, text="닫기", command=win.destroy).pack(side="right")

        self._img_dialog = {"win": win, "inner": inner, "photos": [],
                            "vars": [], "items": []}
        self._do_img_search(qvar)   # 처음 열 때 자동 검색

    def _do_img_search(self, qvar):
        d = self._img_dialog
        inner = d["inner"]
        for w in inner.winfo_children():
            w.destroy()
        d["photos"].clear(); d["vars"].clear(); d["items"].clear()
        tk.Label(inner, text="검색 중...", fg="#666", font=("맑은 고딕", 10)).pack(anchor="w", pady=8)
        q = qvar.get().strip()
        settings = self.data["settings"]

        def fetch_thumb(it):
            turl = it.get("thumb") or it.get("url")
            try:
                req = urllib.request.Request(turl, headers={"User-Agent": imgf.UA})
                with urllib.request.urlopen(req, timeout=15) as r:
                    it["_thumb_bytes"] = r.read()
            except Exception:
                it["_thumb_bytes"] = None
            return it

        def job():
            try:
                items = imgf.find_images(q, n=16, settings=settings)
                # 썸네일은 이미지마다 별도 요청이라 병렬로 받아야 16장도 빠르게 뜸
                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
                    items = list(ex.map(fetch_thumb, items))
            except Exception:
                items = []
            self.root.after(0, lambda: self._populate_img_results(items))

        threading.Thread(target=job, daemon=True).start()

    def _thumb_photo(self, data, maxw=170):
        im = Image.open(io.BytesIO(data)); im.thumbnail((maxw, maxw))
        return ImageTk.PhotoImage(im)

    def _show_large_preview(self, item):
        """이미지 후보의 큰 미리보기 — 브라우저 안 띄우고 팝업 안에서 표시."""
        if not HAVE_PIL:
            webbrowser.open(item.get("url", "")); return
        pv = tk.Toplevel(self.root); pv.title(item.get("title") or "이미지 미리보기")
        pv.bind("<Map>", lambda e, w=pv: self._theme_walk(w, self._theme, self.dark_mode))
        pv.transient(self.root); pv.geometry("780x720")
        head = tk.Frame(pv); head.pack(fill="x", padx=12, pady=(10, 4))
        tk.Label(head, text=(item.get("title") or "(제목 없음)")[:80],
                 font=("맑은 고딕", 11, "bold"), anchor="w").pack(anchor="w")
        tk.Label(head, text=f"{item.get('source','')} · {item.get('license','')}",
                 fg="#0277bd", font=("맑은 고딕", 9)).pack(anchor="w")
        body = tk.Label(pv, text="이미지 로드 중...", fg="#888", font=("맑은 고딕", 10))
        body.pack(fill="both", expand=True, padx=12, pady=8)
        pv._photo_ref = None       # GC 방지

        def load():
            try:
                req = urllib.request.Request(item.get("url"), headers={"User-Agent": imgf.UA})
                with urllib.request.urlopen(req, timeout=20) as r:
                    data = r.read()
                im = Image.open(io.BytesIO(data)); im.thumbnail((720, 600))
                ph = ImageTk.PhotoImage(im)
                self.root.after(0, lambda: (body.config(text="", image=ph),
                                            setattr(pv, "_photo_ref", ph)))
            except Exception as e:
                self.root.after(0, lambda: body.config(text=f"불러오기 실패: {e}"))
        threading.Thread(target=load, daemon=True).start()

        bar = tk.Frame(pv); bar.pack(fill="x", padx=12, pady=(0, 12))
        tk.Button(bar, text="🌐 원본 페이지", command=lambda: webbrowser.open(
            item.get("source_page") or item.get("url", ""))).pack(side="left")
        tk.Button(bar, text="닫기", command=pv.destroy).pack(side="right")

    def _populate_img_results(self, items):
        d = self._img_dialog
        if not d["win"].winfo_exists():
            return
        inner = d["inner"]
        for w in inner.winfo_children():
            w.destroy()
        if not items:
            tk.Label(inner, text=(
                "결과가 없습니다 — 추상적인 주제어는 매칭이 잘 안 됩니다.\n\n"
                "다음처럼 검색해보세요:\n"
                "· 한글 정확한 지명·문화유산명 1개만 (예: '경복궁', '전주', '통영')\n"
                "· 구체적인 영어 키워드 (예: 'Korean mountains landscape', "
                "'Korean coastline nature')\n\n"
                "'한국의 지형/기후/역사' 같은 개관형 주제는 한 번에 검색하지 말고,\n"
                "글에 실제로 들어갈 구체적인 대상(예: 설악산·한라산·동해·DMZ)으로\n"
                "나눠서 검색하면 훨씬 잘 나옵니다."),
                fg="#888", font=("맑은 고딕", 10), wraplength=640, justify="left").pack(anchor="w", pady=8)
            return
        for it in items:
            row = tk.Frame(inner, bd=1, relief="solid")
            row.pack(fill="x", padx=4, pady=4)
            var = tk.BooleanVar(value=False)
            tk.Checkbutton(row, variable=var).pack(side="left", padx=4)
            if HAVE_PIL and it.get("_thumb_bytes"):
                try:
                    ph = self._thumb_photo(it["_thumb_bytes"])
                    d["photos"].append(ph)   # 참조 유지(GC 방지)
                    # 클릭 시 큰 미리보기(브라우저 안 띄움)
                    lbl = tk.Label(row, image=ph, cursor="hand2")
                    lbl.pack(side="left", padx=4, pady=4)
                    lbl.bind("<Button-1>", lambda e, item=it: self._show_large_preview(item))
                except Exception:
                    tk.Button(row, text="🔍 미리보기", width=10,
                              command=lambda item=it: self._show_large_preview(item)).pack(side="left", padx=4)
            else:
                tk.Button(row, text="🔍 미리보기", width=10,
                          command=lambda item=it: self._show_large_preview(item)).pack(side="left", padx=4)
            meta = tk.Frame(row); meta.pack(side="left", fill="x", expand=True, padx=6)
            tk.Label(meta, text=(it.get("title") or "(제목 없음)")[:60],
                     font=("맑은 고딕", 10, "bold"), anchor="w",
                     wraplength=420, justify="left").pack(anchor="w")
            tk.Label(meta, text=f"{it.get('source','')} · {it.get('license','')}",
                     fg="#0277bd", font=("맑은 고딕", 8), anchor="w").pack(anchor="w")
            tk.Label(meta, text=imgf.attribution_caption(it, "ko"), fg="#888",
                     font=("맑은 고딕", 8), anchor="w", wraplength=420, justify="left").pack(anchor="w")
            d["vars"].append(var); d["items"].append(it)

    def _insert_selected_images(self):
        d = getattr(self, "_img_dialog", None)
        if not d:
            return
        chosen = [d["items"][i] for i, v in enumerate(d["vars"]) if v.get()]
        if not chosen:
            messagebox.showinfo("이미지 넣기", "넣을 이미지를 한 개 이상 선택하세요(체크박스).")
            return
        settings = self._collect_settings()
        n = core.insert_images_into_generated(self.selected, chosen, settings=settings,
                                               log=self.log_q.put)
        if n:
            messagebox.showinfo("이미지 넣기",
                                f"{n}장을 글 맨 위에 출처와 함께 넣었습니다.\n"
                                "[👁 미리보기]로 확인하세요.")
            d["win"].destroy()

    def _open_urls(self, _evt):
        entry = self.data["entries"].get(self.selected)
        if entry and entry.get("ko_url"):
            webbrowser.open(entry["ko_url"])

    # ── 설정 ──────────────────────────────────────────────────────────────────
    def _collect_settings(self):
        s = self.data["settings"]
        s["publish_time"] = self.time_var.get().strip() or "09:00"
        s["auto_publish"] = bool(self.auto_var.get())
        s["llm"] = self.llm_var.get()
        s["ollama_model"] = self.omodel_var.get().strip() or "gemma4:26b"
        s["claude_model"] = self.cmodel_var.get().strip() or "claude-opus-4-8"
        s["claude_api_key"] = self.ckey_var.get().strip()
        s["culture_api_key"] = self.culturekey_var.get().strip()
        s["naver_client_id"] = self.naverid_var.get().strip()
        s["naver_client_secret"] = self.naversecret_var.get().strip()
        s["comfy_path"] = self.comfypath_var.get().strip()
        s["tourapi_key"] = self.tourkey_var.get().strip()
        s["gongu_key"] = self.gongukey_var.get().strip()
        s["pexels_key"] = self.pexkey_var.get().strip()
        s["pixabay_key"] = self.pixkey_var.get().strip()
        s["unsplash_key"] = self.unsplashkey_var.get().strip()
        s["wikimedia_token"] = self.wikitoken_var.get().strip()
        s["photo_credit"] = self.photocredit_var.get().strip()
        s["author_name"] = self.authorname_var.get().strip()
        s["author_bio_ko"] = self.authorbioko_var.get().strip()
        s["author_bio_en"] = self.authorbioen_var.get().strip()
        s["seo_schema"] = bool(self.seoschema_var.get())
        s["contact_email"] = self.contactemail_var.get().strip()
        if getattr(self, "seedkw_list", None):
            s["seed_keywords"] = list(self.seedkw_list.get(0, "end"))
        try:
            s["sections"] = max(3, min(10, int(self.sections_var.get())))
        except (ValueError, AttributeError):
            s["sections"] = 6
        if getattr(self, "identity_text", None):
            ident = self.identity_text.get("1.0", "end").strip()
            if ident:
                s["blog_identity"] = ident
        return s

    def save_settings(self):
        self._collect_settings()
        core.save_schedule(self.data)
        self._log("⚙️ 설정 저장 완료\n")
        self._tick_header()

    def _remove_seed_keywords(self):
        sel = list(self.seedkw_list.curselection())
        for i in reversed(sel):
            self.seedkw_list.delete(i)

    def open_keyword_research(self):
        """무료 해외 키워드 확장(구글 자동완성 + 의도 필터 조합) 다이얼로그.
        로그인·API키 불필요 — 결과에서 고른 것만 시드 키워드 목록에 추가."""
        win = tk.Toplevel(self.root)
        win.title("🔍 키워드 확장 (구글 자동완성 · 의도 필터)")
        win.geometry("640x520")

        bar = tk.Frame(win); bar.pack(fill="x", padx=10, pady=8)
        tk.Label(bar, text="시드 키워드:").pack(side="left")
        seed_var = tk.StringVar()
        entry = tk.Entry(bar, textvariable=seed_var)
        entry.pack(side="left", fill="x", expand=True, padx=6)
        entry.focus_set()

        result_list = tk.Listbox(win, selectmode="extended", font=("맑은 고딕", 9))
        result_list.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        status_lbl = tk.Label(win, text="시드 키워드를 입력하고 [검색]을 눌러주세요.",
                              fg="#666", font=("맑은 고딕", 9))
        status_lbl.pack(anchor="w", padx=10)

        def do_search():
            seed = seed_var.get().strip()
            if not seed:
                return
            result_list.delete(0, "end")
            status_lbl.config(text="검색 중...")
            search_btn.config(state="disabled")

            def job():
                try:
                    r = core.expand_keywords(seed, log=lambda *a: None)
                except Exception as e:
                    self.root.after(0, lambda: status_lbl.config(text=f"⚠️ 검색 실패: {e}"))
                    self.root.after(0, lambda: search_btn.config(state="normal"))
                    return

                def show():
                    result_list.insert("end", "── 구글 자동완성 ──")
                    for kw in r["autocomplete"]:
                        result_list.insert("end", kw)
                    for cat, kws in r["intent"].items():
                        result_list.insert("end", f"── 의도 조합: {cat} ──")
                        for kw in kws:
                            result_list.insert("end", kw)
                    status_lbl.config(
                        text=f"자동완성 {len(r['autocomplete'])}개 + 의도 조합 "
                             f"{sum(len(v) for v in r['intent'].values())}개. "
                             "여러 개 선택(Ctrl/Shift) 후 [선택 추가]를 누르세요.")
                    search_btn.config(state="normal")
                self.root.after(0, show)

            threading.Thread(target=job, daemon=True).start()

        def add_selected():
            existing = set(self.seedkw_list.get(0, "end"))
            added = 0
            for i in result_list.curselection():
                kw = result_list.get(i)
                if kw.startswith("── ") or kw in existing:
                    continue
                self.seedkw_list.insert("end", kw)
                existing.add(kw)
                added += 1
            status_lbl.config(text=f"✅ {added}개를 시드 키워드에 추가했습니다.")

        search_btn = tk.Button(bar, text="검색", command=do_search)
        search_btn.pack(side="left")
        entry.bind("<Return>", lambda e: do_search())

        btnrow = tk.Frame(win); btnrow.pack(fill="x", padx=10, pady=(0, 10))
        tk.Button(btnrow, text="➕ 선택 추가", command=add_selected,
                  bg="#1565c0", fg="white").pack(side="left")
        tk.Button(btnrow, text="닫기", command=win.destroy).pack(side="right")

    def toggle_settings(self):
        """설정 바 보이기/숨기기(헤더 [⚙️ 설정] 버튼)."""
        box = getattr(self, "settings_box", None)
        if not box:
            return
        if box.winfo_ismapped():
            box.pack_forget()
        else:
            box.pack(fill="x", padx=10, pady=(0, 4), before=self._progf)

    # ── 다크/라이트 테마 ──────────────────────────────────────────────────────
    def toggle_dark(self):
        self.apply_theme(not self.dark_mode)
        self.data["settings"]["dark_mode"] = self.dark_mode
        core.save_schedule(self.data)

    def apply_theme(self, dark: bool):
        self.dark_mode = dark
        t = THEMES["dark" if dark else "light"]
        self._theme = t
        if getattr(self, "dark_btn", None):
            self.dark_btn.config(text=("☀ 라이트" if dark else "🌙 다크"))
        self.root.configure(bg=t["bg"])
        # ttk 위젯 — 모던 카드 톤 + 강조색
        style = ttk.Style()
        try: style.theme_use("clam")
        except Exception: pass
        # 표(Treeview) — 카드 톤
        style.configure("Treeview", background=t["panel2"], foreground=t["text"],
                        fieldbackground=t["panel2"], borderwidth=0, rowheight=24)
        style.map("Treeview", background=[("selected", t["sel"])],
                  foreground=[("selected", t["text"])])
        style.configure("Treeview.Heading", background=t["panel"], foreground=t["sub"],
                        borderwidth=0, relief="flat")
        style.map("Treeview.Heading", background=[("active", t["panel"])])
        # 탭(Notebook)
        style.configure("TNotebook", background=t["bg"], borderwidth=0)
        style.configure("TNotebook.Tab", background=t["bg"], foreground=t["sub"],
                        padding=(14, 6), borderwidth=0)
        style.map("TNotebook.Tab",
                  background=[("selected", t["panel"])],
                  foreground=[("selected", t["text"])])
        # 콤보·스피너·스크롤바·진행률
        style.configure("TCombobox", fieldbackground=t["field"], background=t["panel"],
                        foreground=t["text"], bordercolor=t["border"], arrowcolor=t["sub"])
        style.map("TCombobox", fieldbackground=[("readonly", t["field"])],
                  foreground=[("readonly", t["text"])])
        style.configure("TProgressbar", background=t["accent"], troughcolor=t["panel"],
                        borderwidth=0)
        style.configure("Vertical.TScrollbar", background=t["panel"],
                        troughcolor=t["bg"], borderwidth=0, arrowcolor=t["sub"])
        style.configure("Horizontal.TScrollbar", background=t["panel"],
                        troughcolor=t["bg"], borderwidth=0, arrowcolor=t["sub"])
        style.configure("TSeparator", background=t["border"])
        self._theme_walk(self.root, t, dark)
        self._draw_legend()
        if getattr(self, "grid_frame", None):
            self.refresh_calendar()

    def _theme_walk(self, w, t, dark):
        for c in w.winfo_children():
            self._theme_one(c, t, dark)
            self._theme_walk(c, t, dark)

    def _theme_one(self, w, t, dark):
        """위젯 1개에 테마 적용. 카드 계층: bg < panel < panel2(LabelFrame 안 Frame)."""
        try:
            cls = w.winfo_class()
            # 부모가 LabelFrame이면 panel2(더 깊은 톤)로 계층 표현
            parent_cls = w.master.winfo_class() if w.master else ""
            inside_card = parent_cls == "Labelframe"

            def neutral(key="bg"):
                try: return str(w.cget(key)).lower() in NEUTRAL_BG
                except Exception: return False

            if cls == "Labelframe":
                if neutral("bg"):
                    w.configure(bg=t["panel"], highlightthickness=0)
                try:
                    w.configure(fg=t["text"], bd=0, relief="flat")
                except Exception: pass
            elif cls == "Frame":
                if neutral("bg"):
                    w.configure(bg=(t["panel2"] if inside_card else t["panel"]),
                                highlightthickness=0)
            elif cls in ("Label", "Radiobutton", "Checkbutton"):
                if neutral("bg"):
                    w.configure(bg=(t["panel2"] if inside_card else t["panel"]))
                    try:
                        fg = str(w.cget("fg")).lower()
                        if fg in _DEFAULT_FG:
                            w.configure(fg=t["text"])
                        elif fg in _SUB_FG:
                            w.configure(fg=t["sub"])
                        elif fg in _INFO_FG:
                            w.configure(fg=t["accent"])
                    except Exception: pass
                if cls in ("Radiobutton", "Checkbutton"):
                    try: w.configure(selectcolor=t["field"],
                                     activebackground=t["panel"],
                                     activeforeground=t["text"])
                    except Exception: pass
            elif cls == "Button":
                # 플랫 모던 스타일 — 테두리·하이라이트 제거
                try: w.configure(relief="flat", bd=0, highlightthickness=0,
                                 padx=12, pady=4)
                except Exception: pass
                if neutral("bg"):
                    w.configure(bg=t["btn"], fg=t["btn_text"],
                                activebackground=t["panel2"], activeforeground=t["text"])
                else:
                    try:
                        w.configure(fg=("#ffffff" if _is_dark_color(str(w.cget("bg"))) else "#1a1a1a"),
                                    activeforeground="#ffffff")
                    except Exception: pass
                try:
                    w.configure(disabledforeground=_disabled_fg_for(str(w.cget("bg"))))
                except Exception: pass
            elif cls in ("Text", "Entry"):
                try:
                    w.configure(bg=t["field"], fg=t["text"], insertbackground=t["text"],
                                relief="flat", bd=0, highlightthickness=1,
                                highlightbackground=t["border"], highlightcolor=t["accent"])
                except Exception:
                    w.configure(bg=t["field"], fg=t["text"], insertbackground=t["text"])
            elif cls == "Canvas":
                if neutral("bg"):
                    w.configure(bg=t["panel"], highlightthickness=0)
            elif cls == "Spinbox":
                try:
                    w.configure(bg=t["field"], fg=t["text"], insertbackground=t["text"],
                               buttonbackground=t["btn"], relief="flat", bd=0,
                               highlightthickness=1, highlightbackground=t["border"],
                               highlightcolor=t["accent"])
                except Exception:
                    w.configure(bg=t["field"], fg=t["text"])
            elif cls == "Scrollbar":     # scrolledtext.ScrolledText 내부의 일반 tk 스크롤바
                try:
                    w.configure(bg=t["panel"], activebackground=t["panel2"],
                               troughcolor=t["bg"], highlightthickness=0,
                               relief="flat", bd=0, elementborderwidth=0)
                except Exception: pass
        except Exception:
            pass

    def toggle_series_info(self):
        """시리즈 탭의 설명·정체성 보이기/숨기기."""
        info = getattr(self, "series_info", None)
        if not info:
            return
        if info.winfo_ismapped():
            info.pack_forget()
        else:
            info.pack(fill="x", padx=14, before=self._series_ctrl)

    def stop_comfy(self):
        """[설정]의 [🛑 종료] — 우리가 띄운 ComfyUI 프로세스 종료(VRAM 회수용)."""
        if not messagebox.askyesno("ComfyUI 종료",
                                   "백그라운드로 실행 중인 ComfyUI를 종료할까요?\n(VRAM이 회수됩니다.)"):
            return
        imgen.stop(log=self.log_q.put)
        messagebox.showinfo("ComfyUI", "종료 요청을 보냈습니다.")

    def check_and_pull_model(self, quiet=False):
        """이 PC에 쓸 모델이 있는지 점검하고, 없으면 추천 모델 다운로드를 제안.
        quiet=True 면 시작 시 자동 점검(이상 없을 땐 팝업 없이 조용히)."""
        settings = self._collect_settings()
        if settings.get("llm") == "claude":
            if not quiet:
                messagebox.showinfo(
                    "모델 확인",
                    "지금은 'Claude'로 생성하도록 설정돼 있어 로컬 모델은 필요 없습니다.")
            return
        if getattr(self, "busy", False):
            if not quiet:
                messagebox.showinfo("진행 중", "다른 작업이 끝난 뒤 다시 시도하세요.")
            return

        def job():
            self._mstatus = core.model_status(settings, log=self.log_q.put)
            st = self._mstatus
            if not st["ollama"]:
                self.log_q.put("   ❌ Ollama 서버를 사용할 수 없습니다. Ollama 설치를 확인하세요.\n")
            elif st["use"]:
                self.log_q.put(
                    f"   ✅ 사용할 모델: {st['use']}  (설치됨 {len(st['installed'])}개)\n")

        def done():
            st = getattr(self, "_mstatus", None) or {}
            need = st.get("need_pull")
            if st.get("ollama") and need:
                if messagebox.askyesno(
                    "모델 다운로드",
                    f"이 PC에 쓸 수 있는 글쓰기 모델이 없습니다.\n\n"
                    f"추천 모델 '{need}'(약 8GB)을 지금 받을까요?\n\n"
                    f"[예] 다운로드 시작 (수십 분 걸릴 수 있음)\n"
                    f"[아니오] 받지 않음 — 설정에서 LLM을 'Claude'로 바꿔 쓸 수도 있습니다."):
                    self._start_worker(
                        lambda: core.pull_model(need, log=self.log_q.put),
                        f"모델 다운로드 ({need})")
            elif st.get("ollama") and st.get("use") and not quiet:
                messagebox.showinfo(
                    "모델 확인",
                    f"이 PC에서 사용할 모델: {st['use']}\n글 생성 준비가 되어 있습니다.")

        self._start_worker(job, "모델 확인", on_done=done)

    def show_help(self):
        help_path = core.SCRIPT_DIR / "사용법.md"
        if help_path.exists():
            webbrowser.open(help_path.as_uri())
        else:
            messagebox.showinfo("사용법", "사용법.md 파일을 찾을 수 없습니다.")

    # ── 발행 블로그 (멀티) ───────────────────────────────────────────────────
    def _blog_name(self, bid):
        b = core.load_registry()["blogs"].get(bid, {})
        return b.get("name") or b.get("url") or bid

    def _update_blog_label(self):
        reg = core.load_registry()
        items = [f"{b['name']}  —  {b['url']}" for b in reg["blogs"].values()]
        self.blog_combo["values"] = items
        self._blog_ids = list(reg["blogs"].keys())
        # 현재 활성 블로그 선택 표시
        if self.active_blog in self._blog_ids:
            idx = self._blog_ids.index(self.active_blog)
            self.blog_combo.current(idx)
        cnt = len(self._blog_ids)
        auto = self.data["settings"].get("auto_publish", True)
        self.blog_lbl.config(
            text=f"등록 {cnt}개 · {'둘 다 자동발행 켜짐' if (cnt > 1 and auto) else ''}")

    def _on_blog_selected(self, _evt=None):
        idx = self.blog_combo.current()
        if idx < 0 or idx >= len(getattr(self, "_blog_ids", [])):
            return
        bid = self._blog_ids[idx]
        if bid != self.active_blog:
            self.switch_blog(bid)

    def switch_blog(self, bid):
        if self.busy:
            messagebox.showinfo("진행 중", "작업이 끝난 뒤 전환하세요.")
            return
        core.set_active_blog(bid)
        self.active_blog = bid
        self.data = core.load_schedule()
        self._reload_for_active()
        self._log(f"\n📍 블로그 전환: {self._blog_name(bid)}\n")

    def _reload_for_active(self):
        """활성 블로그가 바뀌면 모든 위젯을 그 블로그 데이터로 다시 채웁니다."""
        s = self.data["settings"]
        # 설정 바
        self.time_var.set(s.get("publish_time", "09:00"))
        self.auto_var.set(s.get("auto_publish", True))
        self.llm_var.set(s.get("llm", "gemma4"))
        self.omodel_var.set(s.get("ollama_model", "gemma4:26b"))
        self.cmodel_var.set(s.get("claude_model", "claude-opus-4-8"))
        self.ckey_var.set(s.get("claude_api_key", ""))
        self.culturekey_var.set(s.get("culture_api_key", ""))
        self.naverid_var.set(s.get("naver_client_id", ""))
        self.naversecret_var.set(s.get("naver_client_secret", ""))
        self.comfypath_var.set(s.get("comfy_path", ""))
        self.tourkey_var.set(s.get("tourapi_key", ""))
        self.gongukey_var.set(s.get("gongu_key", ""))
        self.pexkey_var.set(s.get("pexels_key", ""))
        self.pixkey_var.set(s.get("pixabay_key", ""))
        self.unsplashkey_var.set(s.get("unsplash_key", ""))
        self.wikitoken_var.set(s.get("wikimedia_token", ""))
        self.authorname_var.set(s.get("author_name", ""))
        self.authorbioko_var.set(s.get("author_bio_ko", ""))
        self.authorbioen_var.set(s.get("author_bio_en", ""))
        self.contactemail_var.set(s.get("contact_email", ""))
        self.seoschema_var.set(s.get("seo_schema", True))
        self.sections_var.set(str(s.get("sections", 5)))
        if getattr(self, "seedkw_list", None):
            self.seedkw_list.delete(0, "end")
            for kw in s.get("seed_keywords", []):
                self.seedkw_list.insert("end", kw)
        # 정체성
        self.identity_text.delete("1.0", "end")
        self.identity_text.insert("1.0", s.get("blog_identity", ""))
        # 주간 템플릿(요일 목록 + 현재 요일 패널 갱신) — 블로그 전환 시 새 데이터로
        if hasattr(self, "wk_list_frame"):
            self._load_weekday_into_panel(self.wk_selected_wd or 0)
        # 발행 계획 흐름도 새 블로그 기준으로
        if hasattr(self, "plan_upcoming"):
            self.refresh_plan_lists()
        # 시리즈 카테고리(이 블로그의 요일 주제 + 추가분)·미리보기 초기화
        self._refresh_categories(select_first=True)
        self.last_plan = None
        self.btn_apply.config(state="disabled")
        # 캘린더/헤더/블로그 표시
        self.refresh_calendar()
        self.load_day(self.selected)
        self._update_blog_label()
        self._tick_header()

    def open_category_cleanup(self):
        """블로그에 카테고리(라벨) 위젯이 있어도 글들이 카테고리별로 안 묶이는 문제 —
        새 글은 generate_post()가 카테고리를 라벨에 항상 넣도록 이미 고쳤지만(2026-07-06),
        이미 발행된 글은 로컬에 라벨을 저장해 두지 않아 소급 반영이 안 된다. 이 도구가
        Blogger에서 현재 라벨을 조회하고, 카테고리가 빠진 글은 AI가 분류를 제안 —
        검토·수정 후 선택한 글에만 라벨을 추가한다(본문·제목은 건드리지 않음)."""
        if self.busy:
            messagebox.showinfo("진행 중", "다른 작업이 끝난 뒤 여세요.")
            return

        win = tk.Toplevel(self.root)
        win.title(f"🏷 카테고리 정리 — {self._blog_name(self.active_blog)}")
        win.transient(self.root); win.geometry("780x580")
        win.bind("<Map>", lambda e, w=win: self._theme_walk(w, self._theme, self.dark_mode))

        tk.Label(win, text="발행된 글들의 현재 카테고리(라벨) 상태를 Blogger에서 조회합니다. "
                            "카테고리가 없는 글은 AI가 이 블로그의 카테고리 중 하나로 분류를 "
                            "제안합니다 — 적용 전에 표에서 확인·수정할 수 있습니다.",
                 font=("맑은 고딕", 9), fg="#555", wraplength=740, justify="left").pack(
            anchor="w", padx=12, pady=(12, 6))

        status_lbl = tk.Label(win, text="분석 준비 중...", font=("맑은 고딕", 9), fg="#888")
        status_lbl.pack(anchor="w", padx=12)

        body = tk.Frame(win); body.pack(fill="both", expand=True, padx=12, pady=(6, 4))
        cols = ("date", "topic", "state", "category")
        tv = ttk.Treeview(body, columns=cols, show="headings", height=18, selectmode="extended")
        heads = {"date": "날짜", "topic": "주제", "state": "현재 상태", "category": "적용할 카테고리"}
        widths = {"date": 90, "topic": 320, "state": 110, "category": 160}
        for c in cols:
            tv.heading(c, text=heads[c])
            tv.column(c, width=widths[c], anchor="w")
        tv.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(body, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set); sb.pack(side="left", fill="y")

        tk.Label(win, text="행을 더블클릭하면 카테고리를 바꿀 수 있습니다. "
                            "적용은 선택(Ctrl/Shift로 다중 선택)된 행에만 이루어집니다 — "
                            "카테고리가 이미 있는 글은 기본적으로 선택돼 있지 않습니다.",
                 fg="#888", font=("맑은 고딕", 8), wraplength=740, justify="left").pack(
            anchor="w", padx=12)

        state = {"candidates": [], "assign": {}, "done": [], "failed": []}
        categories = core.blog_categories(self.data)
        settings = self._collect_settings()

        def populate():
            for i in tv.get_children():
                tv.delete(i)
            for c in state["candidates"]:
                cat = state["assign"].get(c["date"], "")
                stat = "✅ 있음" if c["has_category"] else "⚠ 없음"
                tv.insert("", "end", iid=c["date"],
                          values=(c["date"], (c.get("topic") or "")[:40], stat, cat))
            for c in state["candidates"]:
                if not c["has_category"]:
                    tv.selection_add(c["date"])

        def fetch_job():
            try:
                cands = core.fetch_relabel_candidates(
                    self.data, settings, log=self.log_q.put, progress=self._progress_cb)
                assign = core.suggest_categories(
                    cands, self.data, settings, log=self.log_q.put, progress=self._progress_cb)
                state["candidates"] = cands
                state["assign"] = assign
            except Exception as ex:
                self.log_q.put(f"   ❌ 카테고리 조회·분류 오류: {ex}\n")
            finally:
                self.root.after(0, fetch_done)

        def fetch_done():
            n = len(state["candidates"])
            missing = sum(1 for c in state["candidates"] if not c["has_category"])
            status_lbl.config(
                text=f"발행된 글 {n}개 중 카테고리 없음 {missing}개 — "
                     "AI가 제안한 카테고리를 확인 후 [선택 행 일괄 적용]을 누르세요."
                     if n else "조회된 발행글이 없습니다.")
            populate()
            self._set_buttons(True)

        status_lbl.config(text="Blogger에서 발행글 라벨 조회 중...")
        self._set_buttons(False)
        threading.Thread(target=fetch_job, daemon=True).start()

        def edit_category(event=None):
            sel = tv.selection()
            if not sel:
                return
            iid = sel[0]
            ewin = tk.Toplevel(win)
            ewin.title("카테고리 선택"); ewin.transient(win)
            tk.Label(ewin, text=f"{iid} 의 카테고리:").pack(padx=12, pady=(12, 4))
            cur = tv.set(iid, "category") or (categories[0] if categories else "")
            cvar = tk.StringVar(value=cur)
            combo = ttk.Combobox(ewin, textvariable=cvar, values=categories,
                                  state="readonly", width=28)
            combo.pack(padx=12, pady=4)

            def ok():
                state["assign"][iid] = cvar.get()
                tv.set(iid, "category", cvar.get())
                ewin.destroy()
            tk.Button(ewin, text="확인", command=ok, bg="#2e7d32", fg="white").pack(
                pady=(4, 12))
        tv.bind("<Double-Button-1>", edit_category)

        def do_apply():
            sel = tv.selection()
            if not sel:
                messagebox.showinfo("선택", "적용할 행을 하나 이상 선택하세요.")
                return
            assignments = {iid: tv.set(iid, "category") for iid in sel if tv.set(iid, "category")}
            if not assignments:
                messagebox.showinfo("카테고리 없음",
                                     "선택한 행에 적용할 카테고리가 지정되지 않았습니다.")
                return
            if not messagebox.askyesno(
                    "적용 확인",
                    f"{len(assignments)}개 글에 카테고리 라벨을 적용합니다. 계속할까요?"):
                return

            def apply_job():
                try:
                    done, failed = core.apply_category_labels(
                        state["candidates"], assignments,
                        log=self.log_q.put, progress=self._progress_cb)
                    state["done"], state["failed"] = done, failed
                except Exception as ex:
                    self.log_q.put(f"   ❌ 라벨 적용 오류: {ex}\n")
                    state["done"], state["failed"] = [], list(assignments.keys())
                finally:
                    self.root.after(0, apply_done)

            def apply_done():
                done, failed = state.get("done", []), state.get("failed", [])
                msg = f"{len(done)}개 적용 완료"
                if failed:
                    msg += f", {len(failed)}건 실패: {', '.join(failed[:5])}"
                messagebox.showinfo("적용 완료", msg)
                for iid in done:
                    if tv.exists(iid):
                        tv.set(iid, "state", "✅ 있음")
                        for c in state["candidates"]:
                            if c["date"] == iid:
                                c["has_category"] = True
                self._set_buttons(True)

            self._set_buttons(False)
            threading.Thread(target=apply_job, daemon=True).start()

        btns = tk.Frame(win); btns.pack(fill="x", padx=12, pady=(4, 12))
        tk.Button(btns, text="✅ 선택 행 일괄 적용", command=do_apply,
                  bg="#2e7d32", fg="white", font=("맑은 고딕", 10, "bold")).pack(side="left")
        tk.Button(btns, text="닫기", command=win.destroy).pack(side="right")

    def login_manager_dialog(self):
        """사진 업로드 인증 관리 — 어떤 계정으로 로그인돼 있는지 확인/로그인/공유/초기화."""
        if self.busy:
            messagebox.showinfo("진행 중", "작업이 끝난 뒤 여세요.")
            return
        bid = self.active_blog
        reg = core.load_registry()
        b = reg["blogs"][bid]
        own = core.own_profile_path(bid)
        shared = str(Path(b.get("profile_dir", own))) != str(Path(own))

        win = tk.Toplevel(self.root)
        win.title(f"🔐 로그인 관리 — {b.get('name','')}")
        win.transient(self.root)
        win.bind("<Map>", lambda e, w=win: self._theme_walk(w, self._theme, self.dark_mode))
        win.geometry("620x420")

        tk.Label(win, text=f"블로그: {b.get('name','')}  ({b.get('url','')})",
                 font=("맑은 고딕", 11, "bold")).pack(anchor="w", padx=14, pady=(14, 2))
        status_lines = [
            f"사진 업로드 세션: {'다른 블로그와 공유 중' if shared else '이 블로그 전용'}",
            f"  경로: {b.get('profile_dir','')}",
            f"확인된 로그인 계정: {b.get('verified_email') or '(아직 확인 안 함)'}",
            f"에디터 편집 권한: "
            + ("✅ 확인됨" if b.get("verified_ok") else "❓ 미확인/실패")
            + (f"  ({b.get('verified_at','')})" if b.get("verified_at") else ""),
        ]
        tk.Label(win, text="\n".join(status_lines), font=("맑은 고딕", 9),
                 fg="#444", justify="left").pack(anchor="w", padx=14, pady=(4, 10))

        tk.Label(win, text="① 아래 버튼으로 브라우저를 열어 로그인 상태와 권한을 확인하세요.\n"
                            "   로그인이 안 돼 있으면 그 창에서 '이 블로그를 편집할 수 있는 계정'으로\n"
                            "   천천히 로그인하면 됩니다(자동 새로고침 없음). 결과가 여기 기록됩니다.",
                 font=("맑은 고딕", 9), fg="#1565c0", justify="left").pack(anchor="w", padx=14)

        def run_verify():
            win.destroy()

            def job():
                res = core.verify_blog_browser(bid, log=self.log_q.put)
                self._verify_res = res

            def done():
                res = getattr(self, "_verify_res", None) or {}
                email = res.get("email") or "(감지 실패)"
                if res.get("editor_ok"):
                    messagebox.showinfo("확인 완료",
                                        f"로그인 계정: {email}\n"
                                        f"이 블로그 편집 권한: ✅ 있음\n\n"
                                        f"이제 발행하면 이미지가 정상 업로드됩니다.")
                elif not res.get("logged_in"):
                    messagebox.showwarning("로그인 안 됨",
                                           "로그인이 완료되지 않았습니다.\n"
                                           "다시 열어 로그인을 끝까지 진행해 주세요.")
                else:
                    in_list = res.get("in_blog_list")
                    list_line = ("이 세션의 블로그 목록에 보임: "
                                 + ("✅ 예" if in_list else ("❌ 아니오" if in_list is False else "확인 불가")))
                    messagebox.showwarning(
                        "권한 없음",
                        f"로그인 계정: {email}\n"
                        f"{list_line}\n"
                        f"이 블로그 편집 권한: ❌ 없음 (403)\n\n"
                        + ("→ 권한은 보이는데 에디터만 막힘 — [세션 초기화] 후\n"
                           "   새로 로그인하면 해결될 가능성이 큽니다.\n"
                           if in_list else
                           "→ 이 계정에 아직 권한이 없습니다(초대 미수락/미저장 가능).\n"
                           "   블로그 주인 계정에서 권한을 다시 확인하거나,\n"
                           "   이 창에서 주인 계정으로 로그인하세요.\n"))
            self._start_worker(job, "로그인·권한 확인", on_done=done)

        tk.Button(win, text="① 브라우저 열어 로그인·권한 확인", command=run_verify,
                  bg="#1565c0", fg="white", font=("맑은 고딕", 11, "bold")).pack(fill="x", padx=14, pady=(10, 4))

        def do_revert():
            core.revert_own_profile(bid)
            self._log("🔓 공유 해제 — 이 블로그 전용 세션으로 전환\n")
            win.destroy()
            self.login_manager_dialog()

        def do_reset():
            if shared:
                messagebox.showwarning("공유 중", "공유 세션은 초기화할 수 없습니다.\n먼저 전용 세션으로 전환하세요.")
                return
            if not messagebox.askyesno("세션 초기화", "저장된 로그인(브라우저 세션)을 지우고\n다음에 새로 로그인할까요?"):
                return
            import shutil as _sh
            _sh.rmtree(b.get("profile_dir", own), ignore_errors=True)
            core.revert_own_profile(bid)
            self._log("🧹 브라우저 세션 초기화 완료 — 다음 확인 때 새로 로그인\n")
            win.destroy()

        row = tk.Frame(win); row.pack(fill="x", padx=14, pady=4)
        tk.Button(row, text="다른 블로그 세션 공유...", command=lambda: (win.destroy(), self.share_login_dialog())).pack(side="left")
        if shared:
            tk.Button(row, text="공유 해제(전용 세션으로)", command=do_revert).pack(side="left", padx=6)
        tk.Button(row, text="세션 초기화(로그아웃)", command=do_reset).pack(side="left", padx=6)
        tk.Button(win, text="닫기", command=win.destroy).pack(pady=8)

    def share_login_dialog(self):
        """현재 블로그의 사진 업로드에 '이미 로그인된 다른 블로그'의 브라우저 세션을 공유."""
        reg = core.load_registry()
        others = [(bid, b) for bid, b in reg["blogs"].items() if bid != self.active_blog]
        if not others:
            messagebox.showinfo("로그인 공유", "다른 등록된 블로그가 없습니다.")
            return
        win = tk.Toplevel(self.root)
        win.title("사진 업로드 로그인 공유")
        win.transient(self.root)
        win.bind("<Map>", lambda e, w=win: self._theme_walk(w, self._theme, self.dark_mode))
        win.geometry("560x300")
        tk.Label(win,
                 text=f"현재 블로그 '{self._blog_name(self.active_blog)}'의 사진 업로드에\n"
                      f"사용할 '이미 로그인된' 블로그의 브라우저 세션을 고르세요.\n"
                      f"※ 같은 구글 계정일 때만 사용하세요(예: 한 계정이 두 블로그의 관리자).",
                 font=("맑은 고딕", 10), justify="left").pack(anchor="w", padx=14, pady=(14, 8))
        var = tk.StringVar(value=others[0][0])
        for bid, b in others:
            tk.Radiobutton(win, text=f"{b['name']}\n   {b['url']}", variable=var, value=bid,
                           justify="left", anchor="w").pack(fill="x", padx=16, pady=2)

        def apply():
            src = var.get()
            core.set_shared_browser(self.active_blog, src)
            self._log(f"🔗 사진 업로드 로그인 공유: '{self._blog_name(self.active_blog)}' "
                      f"← '{self._blog_name(src)}' 세션 사용\n")
            messagebox.showinfo("완료",
                                "이제 이 블로그의 사진 업로드에 선택한 블로그의 로그인 세션을 씁니다.\n"
                                "다음 발행부터 재로그인 없이 이미지가 올라갑니다.")
            win.destroy()

        tk.Button(win, text="이 로그인 세션 공유", command=apply,
                  bg="#2e7d32", fg="white", font=("맑은 고딕", 11, "bold")).pack(pady=12)

    def add_blog_dialog(self):
        if self.busy:
            messagebox.showinfo("진행 중", "작업이 끝난 뒤 추가하세요.")
            return
        win = tk.Toplevel(self.root)
        win.title("블로그 추가")
        win.transient(self.root)
        win.bind("<Map>", lambda e, w=win: self._theme_walk(w, self._theme, self.dark_mode))
        win.geometry("520x210")
        tk.Label(win, text="추가할 블로그 주소를 입력하세요 (예: k-culture-dictionary.blogspot.com)",
                 font=("맑은 고딕", 10), justify="left").pack(anchor="w", padx=14, pady=(14, 4))
        url_var = tk.StringVar(value="https://")
        tk.Entry(win, textvariable=url_var, font=("맑은 고딕", 11)).pack(fill="x", padx=14, pady=4)
        tk.Label(win, text="• [추가]를 누르면 구글 로그인 창이 열립니다.\n"
                            "  그 블로그를 소유한 구글 계정으로 로그인하세요(다른 계정도 가능).\n"
                            "• 첫 사진 업로드 때 그 계정으로 한 번 더 로그인할 수 있습니다.",
                 fg="#666", font=("맑은 고딕", 9), justify="left").pack(anchor="w", padx=14, pady=(6, 4))

        def do_add():
            url = url_var.get().strip()
            if not url or "blogspot" not in url and "." not in url:
                messagebox.showwarning("주소 확인", "블로그 주소를 정확히 입력하세요.")
                return
            win.destroy()
            self._researched = None

            def job():
                bid, burl, name = core.add_blog_via_login(url, log=self.log_q.put)
                self._added_blog = bid

            def done():
                bid = getattr(self, "_added_blog", None)
                if bid:
                    self._added_blog = None
                    self._update_blog_label()
                    if messagebox.askyesno("전환", f"'{self._blog_name(bid)}' 블로그로 지금 전환할까요?"):
                        self.switch_blog(bid)
                        if core.is_karts_now(url) and messagebox.askyesno(
                            "k-arts-now 전략",
                            "이 블로그에 k-arts-now(시의성 큐레이션) 전략을 자동 설정할까요?\n\n"
                            "• 정체성(색깔), 6개 시기별 카테고리, 요일 일정(월·수·금 06시),\n"
                            "  시드 키워드를 채웁니다."):
                            core.apply_karts_now_preset(self.data)
                            core.save_schedule(self.data)
                            self._reload_for_active()
                            self._log("📋 k-arts-now 전략 자동 적용 완료\n")
            self._start_worker(job, f"블로그 추가: {url}", on_done=done)

        tk.Button(win, text="추가 (로그인)", command=do_add,
                  bg="#2e7d32", fg="white", font=("맑은 고딕", 11, "bold")).pack(pady=10)

    # ── 진행률 ────────────────────────────────────────────────────────────────
    def _progress_cb(self, pct, msg):
        """작업 스레드에서 호출 — 큐를 통해 메인 스레드로 전달(스레드 안전)."""
        self.prog_q.put((float(pct), str(msg)))

    def _set_progress(self, pct, msg):
        pct = max(0.0, min(100.0, pct))
        self.progress_var.set(pct)
        self.pct_lbl.config(text=f"{int(round(pct))}%")
        self.current_msg = msg
        self.task_start = time.time()
        self.status_lbl.config(text=msg, fg=getattr(self, "_theme", {}).get("accent", "#1565c0"))

    def _set_idle(self):
        self.current_msg = "대기 중"
        self.task_start = None
        self.status_lbl.config(text="대기 중", fg="#888")
        self.progress_var.set(0)
        self.pct_lbl.config(text="0%")

    # ── 주기 루프 ─────────────────────────────────────────────────────────────
    def _pump_log(self):
        try:
            while True:
                s = self.log_q.get_nowait()
                self._log(s)
        except queue.Empty:
            pass
        try:
            while True:
                pct, msg = self.prog_q.get_nowait()
                self._set_progress(pct, msg)
        except queue.Empty:
            pass
        self.root.after(150, self._pump_log)

    def _tick_activity(self):
        """작업이 도는 동안 '경과 초 + 점'을 보여줘 멈춘 게 아님을 표시."""
        if self.busy and self.task_start:
            elapsed = int(time.time() - self.task_start)
            dots = "." * (1 + (elapsed % 3))
            mm, ss = divmod(elapsed, 60)
            et = f"{mm}분 {ss}초" if mm else f"{ss}초"
            self.status_lbl.config(
                text=f"⏳ {self.current_msg}{dots}   (이 단계 {et} 경과)",
                fg="#ef6c00")
        self.root.after(1000, self._tick_activity)

    def _log(self, s):
        self.log.config(state="normal")
        self.log.insert("end", s if s.endswith("\n") or s == "" else s)
        self.log.see("end")
        self.log.config(state="disabled")

    def _tick_header(self):
        nxt = core.next_publish_datetime(self.data)
        auto = self.data["settings"].get("auto_publish", True)
        self.auto_lbl.config(
            text=("🟢 자동 발행 켜짐" if auto else "⚪ 자동 발행 꺼짐"),
            fg=("#80cbc4" if auto else "#b0bec5"))
        if not nxt:
            self.header_lbl.config(text="📅 예정된 발행이 없습니다 — 캘린더에서 날짜에 주제를 입력하세요.")
        else:
            dt, ds = nxt
            dd = core.post_date(ds)
            now = datetime.now()
            if dt <= now:
                self.header_lbl.config(
                    text=f"⏰ 발행 대기 중: {dd} {dt.strftime('%H:%M')} — 지금 발행 시각이 지났습니다"
                         + ("  (자동 발행 대기)" if auto else "  ([지금 발행]을 눌러주세요)"))
            else:
                delta = dt - now
                d = delta.days
                h, rem = divmod(delta.seconds, 3600)
                m, sec = divmod(rem, 60)
                cd = (f"{d}일 " if d else "") + f"{h:02d}:{m:02d}:{sec:02d}"
                topic = self.data["entries"].get(ds, {}).get("topic", "")
                self.header_lbl.config(
                    text=f"📅 다음 발행: {dd} {dt.strftime('%H:%M')}  (D-{cd})   주제: {topic}")
        self.root.after(1000, self._tick_header)

    QUOTA_COOLDOWN_SEC = 900       # 쓰기 할당량 소진 시 자동 발행을 멈춰 둘 시간(15분).
    # 429여도 수정된 코드는 쓰기 1~2회만 시도하고 즉시 멈추므로(비파괴적) 짧게 잡아
    # 버스트 제한이 풀리면 빨리 재개한다. 일일 상한이면 15분마다 조용히 확인만 하고 넘어감.

    def _handle_quota_error(self, ex, where: str = "") -> bool:
        """예외가 Blogger 쓰기 할당량(429)이면 자동 발행을 쿨다운시키고 안내 후 True.
        아니면 False(호출부가 평소대로 처리). 429는 재시도해도 리셋 전까지 계속 실패하므로
        글마다 몇 분씩 헛돌지 않게 '즉시 중단'하는 것이 핵심."""
        try:
            import publish_today as pub
            if not pub.is_quota_error(ex):
                return False
        except Exception:
            return False
        self._quota_block_until = time.time() + self.QUOTA_COOLDOWN_SEC
        mins = self.QUOTA_COOLDOWN_SEC // 60
        self.log_q.put(
            f"\n🛑 Blogger 쓰기 할당량 소진(429){(' — ' + where) if where else ''}\n"
            f"   글 발행·수정·이미지 업로드(임시 초안)가 모두 막힌 상태라 재시도해도 실패합니다.\n"
            f"   → 자동 발행을 {mins}분간 멈춥니다. 이후 자동으로 다시 시도합니다.\n"
            f"   ※ 쓰기 할당량은 보통 태평양 자정(한국시간 오후 4시경)에 리셋됩니다.\n")
        return True

    def _tick_scheduler(self):
        """자동 발행: 등록된 '모든' 블로그를 훑어 시각이 지난 미발행 항목을 발행.
        Blogger 쓰기 할당량(429)이 소진되면 재시도해도 계속 실패하므로, 일정 시간 자동 발행을
        멈췄다가(쿨다운) 자동으로 다시 시도한다(2026-07-14)."""
        try:
            blocked_until = getattr(self, "_quota_block_until", 0)
            if time.time() < blocked_until:
                pass                                  # 할당량 소진 쿨다운 중 — 이번 틱은 건너뜀
            elif self.data["settings"].get("auto_publish", True) and not self.busy:
                due = core.scan_all_due()                 # 활성 블로그가 바뀜
                core.set_active_blog(self.active_blog)    # UI용으로 복구
                if due:
                    bid, ds = due[0]
                    self._log(f"\n⏰ 자동 발행: [{self._blog_name(bid)}] {ds}\n")

                    def job(bid=bid, ds=ds):
                        try:
                            try:
                                core.set_active_blog(bid)
                                data = core.load_schedule()
                                core.publish_date(ds, data["settings"], data,
                                                  log=self.log_q.put, progress=self._progress_cb,
                                                  stop_check=lambda: self.stop_requested)
                            finally:
                                core.set_active_blog(self.active_blog)
                        except Exception as ex:
                            if self._handle_quota_error(ex, f"[자동] {ds}"):
                                return                # 할당량 소진 — 조용히 종료(재시도 안 함)
                            raise
                    self._start_worker(job, f"[자동] {self._blog_name(bid)} · {ds}")
        except Exception as e:
            try:
                core.set_active_blog(self.active_blog)
            except Exception:
                pass
            self._log(f"스케줄러 오류: {e}\n")
        self.root.after(30000, self._tick_scheduler)  # 30초마다 확인


def main():
    selftest = "--selftest" in sys.argv
    root = tk.Tk()
    app = BlogStudio(root)
    if selftest:
        root.after(300, root.destroy)
        root.mainloop()
        print("SELFTEST OK")
        return
    root.mainloop()


if __name__ == "__main__":
    main()
