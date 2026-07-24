# -*- coding: utf-8 -*-
"""
photo_xmp.py — 사진 파일에 표준 XMP 메타데이터(키워드·캡션) 저장/읽기.

ExifTool(외부 .exe, 60MB)을 사용해 모든 사진 포맷(JPG/PNG/HEIC/TIFF/RAW)에
표준 메타데이터를 안전하게 씁니다. Lightroom·Adobe Bridge·Windows 탐색기·
구글 포토·Apple Photos 모두가 이 데이터를 읽을 수 있어요.

핵심
  ensure_exiftool(log)              ExifTool 자동 다운로드 + 경로 반환
  write_keywords(path, keywords, caption=None, log)   사진에 XMP 키워드·캡션 저장
  read_keywords(path)               사진에서 XMP 키워드 읽기(스캔 시 복원용)
  sync_library(only_unsynced=True)  DB 사진들 일괄 XMP 쓰기
"""

import os
import re
import sys
import json
import time
import zipfile
import subprocess
import urllib.request
from contextlib import closing
from datetime import datetime
from pathlib import Path

import photo_library as pl

# ExifTool은 버전이 자주 올라가고 그때마다 파일명(버전 번호)이 바뀌어서, 특정 버전을
# 직접 박아두면 머지않아 다운로드 링크가 깨집니다(2026-06-21에 실제로 13.04가 404 남).
# 그래서 exiftool.org 홈페이지에서 "지금" 버전의 윈도우 64비트 zip 파일명을 매번 새로
# 확인해서 받습니다. 혹시 그조차 실패하면 마지막으로 확인된 버전으로 한 번 더 시도.
_FALLBACK_EXIFTOOL_URL = "https://sourceforge.net/projects/exiftool/files/exiftool-13.59_64.zip/download"
TOOLS_DIR = Path(__file__).resolve().parent / "tools"
EXIFTOOL_PATH = TOOLS_DIR / "exiftool.exe"


def _latest_exiftool_url(log=print) -> str:
    """exiftool.org에서 현재 윈도우 64비트 zip 파일명을 찾아 SourceForge 다운로드
    URL을 구성. 실패하면 마지막으로 확인된 버전 URL로 폴백."""
    try:
        req = urllib.request.Request("https://exiftool.org/",
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            html = r.read().decode("utf-8", errors="replace")
        m = re.search(r"exiftool-[\d.]+_64\.zip", html)
        if m:
            fname = m.group(0)
            return f"https://sourceforge.net/projects/exiftool/files/{fname}/download"
    except Exception as e:
        log(f"   ⚠️ 최신 버전 확인 실패({e}) — 마지막 확인된 버전으로 시도")
    return _FALLBACK_EXIFTOOL_URL


def ensure_exiftool(log=print) -> str:
    """ExifTool이 PATH에 있으면 그걸 쓰고, 없으면(윈도우만) tools/ 폴더에 자동 다운로드.
    설치 경로 반환(빈 문자열이면 실패)."""
    # 1) 이미 받아 둔 것
    if EXIFTOOL_PATH.exists():
        return str(EXIFTOOL_PATH)
    # 2) PATH에 있나 — 맥·리눅스는 보통 'exiftool'(확장자 없음), 이 경로로 brew 설치본도 잡힘
    for name in ("exiftool.exe", "exiftool"):
        try:
            r = subprocess.run([name, "-ver"], capture_output=True, text=True, timeout=4)
            if r.returncode == 0:
                return name
        except Exception:
            pass
    # 2.5) 맥·리눅스는 자동 다운로드가 윈도우 실행파일(zip) 대상이라 의미가 없음 —
    #      Homebrew 설치 안내만 하고 종료(2026-07-24, 맥 이식 대응).
    if sys.platform != "win32":
        log("   ❌ ExifTool을 찾을 수 없습니다. 터미널에서 한 번만 설치해 주세요:")
        log("      brew install exiftool")
        log("      설치 후 [💾 사진에 태그 쓰기]를 다시 누르면 자동으로 인식됩니다.")
        return ""
    # 3) 자동 다운로드 시도(윈도우 전용) — 망·서버 사정에 따라 막힐 수 있어 '되면 좋고' 정도로 취급.
    #    (SourceForge가 자동화된 요청을 막아 HTML을 내려주는 경우가 있어, zip 형식인지
    #    꼭 확인하고, 안 되면 바로 수동 설치 안내로 넘어감 — 우회를 시도하지 않음.)
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    zpath = TOOLS_DIR / "exiftool.zip"
    urls = [_latest_exiftool_url(log)]
    if urls[0] != _FALLBACK_EXIFTOOL_URL:
        urls.append(_FALLBACK_EXIFTOOL_URL)
    downloaded = False
    for i, url in enumerate(urls):
        log(f"   ⬇️ ExifTool 자동 다운로드 시도... {url}")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r, open(zpath, "wb") as f:
                f.write(r.read())
            if zipfile.is_zipfile(zpath):
                downloaded = True
                break
            log("   ⚠️ 받은 파일이 zip이 아님(다운로드 사이트가 자동 요청을 막은 듯)")
        except Exception as e:
            log(f"   ⚠️ 다운로드 실패: {e}")
    if not downloaded:
        zpath.unlink(missing_ok=True)
        log("   ❌ 자동 다운로드 불가 — 한 번만 직접 설치가 필요합니다.")
        log("      1) https://exiftool.org/ 에서 'Windows Executable' 다운로드")
        log("      2) 압축 풀어서 나온 'exiftool(-k).exe' 파일 이름을 'exiftool.exe'로 변경")
        log(f"      3) 이 파일을 다음 폴더로 이동: {TOOLS_DIR}")
        log("      4) 이동 후 [💾 사진에 태그 쓰기]를 다시 누르면 자동으로 인식됩니다.")
        return ""
    log("   📦 압축 해제 중...")
    try:
        with zipfile.ZipFile(zpath) as z:
            # 배포본 안의 파일명: exiftool(-k).exe → exiftool.exe 로 추출
            for n in z.namelist():
                if n.lower().endswith(".exe"):
                    z.extract(n, TOOLS_DIR)
                    src = TOOLS_DIR / n
                    src.rename(EXIFTOOL_PATH)
                if n.lower().startswith("exiftool_files"):
                    z.extract(n, TOOLS_DIR)
        zpath.unlink(missing_ok=True)
    except Exception as e:
        log(f"   ❌ 압축 해제 실패: {e}")
        return ""
    if EXIFTOOL_PATH.exists():
        log(f"   ✅ ExifTool 준비 완료: {EXIFTOOL_PATH}")
        return str(EXIFTOOL_PATH)
    log("   ❌ ExifTool 설치 실패")
    return ""


def _run(tool: str, args: list, log=print, timeout: int = 30) -> tuple:
    try:
        r = subprocess.run([tool] + args, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except Exception as e:
        log(f"      ⚠️ exiftool 호출 실패: {e}")
        return -1, "", str(e)


def write_keywords(path: str, keywords: list, caption: str = "",
                   tool: str = None, log=print) -> bool:
    """사진에 XMP·IPTC 키워드(=태그) + 캡션 저장. -overwrite_original로 백업파일 안 만듦."""
    tool = tool or ensure_exiftool(log)
    if not tool:
        return False
    if not Path(path).exists():
        return False
    args = ["-overwrite_original", "-codedcharacterset=utf8", "-charset", "iptc=utf8",
            "-XMP-dc:Subject=", "-IPTC:Keywords="]   # 기존 키워드 비우고
    for k in keywords:
        k = (k or "").strip()
        if k:
            args += [f"-XMP-dc:Subject+={k}", f"-IPTC:Keywords+={k}"]
    if caption:
        args += [f"-XMP-dc:Description={caption}", f"-IPTC:Caption-Abstract={caption}"]
    args.append(path)
    rc, out, err = _run(tool, args, log)
    if rc != 0:
        log(f"      ⚠️ XMP 쓰기 실패 ({Path(path).name}): {err[:120]}")
        return False
    return True


def read_keywords(path: str, tool: str = None) -> dict:
    """사진에서 XMP 키워드·캡션을 읽음. {keywords:[], caption:''}."""
    tool = tool or ensure_exiftool(log=lambda *a: None)
    if not tool or not Path(path).exists():
        return {"keywords": [], "caption": ""}
    rc, out, err = _run(tool, ["-j", "-XMP-dc:Subject", "-XMP-dc:Description",
                                "-IPTC:Keywords", "-IPTC:Caption-Abstract", path],
                        log=lambda *a: None)
    if rc != 0 or not out.strip():
        return {"keywords": [], "caption": ""}
    try:
        d = json.loads(out)[0]
    except Exception:
        return {"keywords": [], "caption": ""}
    kw = d.get("Subject") or d.get("Keywords") or []
    if isinstance(kw, str):
        kw = [k.strip() for k in kw.split(",") if k.strip()]
    cap = d.get("Description") or d.get("Caption-Abstract") or ""
    return {"keywords": [str(k) for k in kw], "caption": str(cap)}


def sync_library(only_unsynced: bool = True, limit: int = 300,
                 log=print, on_progress=None, db_path=None) -> dict:
    """DB의 user_tags + auto_caption을 사진 파일(XMP)에 일괄 저장.
    only_unsynced=True 면 user_tags가 바뀐 사진(xmp_synced_at 이전 갱신)만."""
    tool = ensure_exiftool(log)
    if not tool:
        raise RuntimeError(
            "ExifTool 자동 설치가 안 됐습니다(다운로드 사이트가 막혀 있는 경우가 있어요).\n\n"
            "한 번만 직접 설치하면 됩니다:\n"
            "  1) https://exiftool.org/ 에서 'Windows Executable' 다운로드\n"
            "  2) 압축 풀어서 나온 'exiftool(-k).exe'를 'exiftool.exe'로 이름 변경\n"
            f"  3) 이 파일을 다음 폴더로 이동: {TOOLS_DIR}\n"
            "  4) 다시 [💾 사진에 태그 쓰기]를 누르면 자동 인식됩니다.")
    pl.init_db(db_path)
    with closing(pl._conn(db_path)) as c:
        if only_unsynced:
            rows = c.execute(
                "SELECT id, path, user_tags, auto_caption, xmp_synced_at "
                "FROM photos "
                "WHERE (user_tags IS NOT NULL AND user_tags!='') "
                "  AND (xmp_synced_at IS NULL OR xmp_synced_at='') "
                "LIMIT ?", (limit,)).fetchall()
        else:
            rows = c.execute(
                "SELECT id, path, user_tags, auto_caption, xmp_synced_at "
                "FROM photos WHERE user_tags IS NOT NULL AND user_tags!='' "
                "LIMIT ?", (limit,)).fetchall()
    log(f"   🏷 XMP 동기화 — 대상 {len(rows)}장")
    ok = fail = 0
    for i, r in enumerate(rows, 1):
        tags = [t.strip() for t in (r["user_tags"] or "").split(",") if t.strip()]
        cap = (r["auto_caption"] or "").strip()
        if write_keywords(r["path"], tags, cap, tool, log):
            with closing(pl._conn(db_path)) as c, c:
                c.execute("UPDATE photos SET xmp_synced_at=? WHERE id=?",
                          (datetime.now().isoformat(), r["id"]))
            ok += 1
        else:
            fail += 1
        if on_progress:
            try: on_progress(i, len(rows))
            except Exception: pass
        if i % 20 == 0:
            log(f"     · {i}/{len(rows)} (ok {ok} / fail {fail})")
    log(f"   ✅ XMP 동기화 완료 — 성공 {ok} / 실패 {fail}")
    return {"ok": ok, "fail": fail, "total": len(rows)}
