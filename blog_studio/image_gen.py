# -*- coding: utf-8 -*-
"""
image_gen.py — 로컬 ComfyUI로 촬영목록(샷 리스트) 기반 이미지 생성.

ComfyUI HTTP API(기본 http://127.0.0.1:8188)에 표준 txt2img 워크플로를 큐잉하고,
완료되면 결과 이미지를 받아 폴더에 저장합니다. 그 폴더를 [📂 폴더 사진 글에 반영]으로 넣으면
전체(히어로)+소주제별로 배치됩니다.

⚠️ AI 생성 이미지는 '실제 특정 장소·문화재·작품·인물'을 정확히 못 그립니다(분위기·개념·일러스트용).

핵심
  is_available(settings)                  ComfyUI 실행 여부
  list_checkpoints(settings)              사용 가능한 체크포인트 목록
  generate(prompt, settings, ...)         이미지 1장 생성 → bytes
  generate_shot_images(shots, settings, out_dir)  샷 리스트로 여러 장 생성 → 파일 경로 목록
"""

import sys
import json
import os
import time
import uuid
import subprocess
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:8188"
# ComfyUI 자동 탐지 — 흔한 설치 위치(포터블·SM·사용자 폴더 등). 드라이브 문자 경로는
# 윈도우 전용이라 그쪽 OS에서만 후보에 넣고, 사용자 폴더 기준 경로는 맥에서도 유효
# (맥은 보통 소스에서 직접 빌드해 Applications/ComfyUI 등에 두는 경우가 많음 — 2026-07-24).
_COMMON_PATHS = []
if sys.platform == "win32":
    _COMMON_PATHS += [
        r"C:\ComfyUI_portable\ComfyUI_portable_01",
        r"C:\ComfyUI",
        r"C:\ComfyUI_windows_portable",
        r"C:\StabilityMatrix\Data\Packages\ComfyUI",
    ]
else:
    _COMMON_PATHS += [
        str(Path("/Applications/ComfyUI")),
        str(Path.home() / "StabilityMatrix" / "Data" / "Packages" / "ComfyUI"),
    ]
_COMMON_PATHS += [
    str(Path.home() / "ComfyUI"),
    str(Path.home() / "Documents" / "ComfyUI"),
]
# 우리가 직접 실행한 ComfyUI 프로세스의 PID 캐시(중복 실행 방지)
_PID_FILE = Path(__file__).resolve().parent / ".comfy_pid"
NEG_DEFAULT = ("lowres, low quality, blurry, jpeg artifacts, watermark, text, signature, "
               "deformed, extra limbs, bad anatomy, cropped, ugly")


def comfy_url(settings: dict) -> str:
    return (settings.get("comfy_url") or DEFAULT_URL).rstrip("/")


def _get(url: str, timeout: int = 8) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def is_available(settings: dict) -> bool:
    try:
        _get(comfy_url(settings) + "/system_stats", timeout=4)
        return True
    except Exception:
        return False


# 윈도우 포터블 배포판은 run_*.bat 런처를 제공하지만, 맥은 보통 소스에서 직접
# `python main.py`로 실행해 표준 런처 스크립트가 없다. 그래도 사용자가 직접 만들어
# 둔 셸 스크립트가 있으면 그걸 쓸 수 있게 후보에 넣는다(2026-07-24 맥 이식 대응).
_LAUNCHER_NAMES = (["run_nvidia_gpu.bat", "run_cpu.bat"] if sys.platform == "win32"
                   else ["run_gpu.sh", "run_cpu.sh", "run.sh", "main.py"])


def find_launcher(settings: dict = None) -> str:
    """ComfyUI 실행용 런처(윈도우 .bat / 맥·리눅스 .sh 또는 main.py) 경로를 찾아 반환.
    없으면 ''."""
    settings = settings or {}
    explicit = (settings.get("comfy_path") or "").strip()
    candidates = ([explicit] if explicit else []) + _COMMON_PATHS
    for base in candidates:
        if not base or not Path(base).exists():
            continue
        # ComfyUI 폴더 또는 그 상위(포터블처럼 런처가 한 단계 위) 모두 검사
        for root in (Path(base), Path(base).parent):
            for name in _LAUNCHER_NAMES:
                cand = root / name
                if cand.exists():
                    return str(cand)
    return ""


def _read_pid() -> int:
    try:
        return int(_PID_FILE.read_text().strip())
    except Exception:
        return 0


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                                 capture_output=True, text=True, timeout=4).stdout
            return str(pid) in out
        except Exception:
            return False
    else:                                  # 맥·리눅스: 시그널 0으로 존재 여부만 확인(안 죽임)
        try:
            os.kill(pid, 0)
            return True
        except Exception:
            return False


def launch(settings: dict, log=print) -> bool:
    """ComfyUI를 백그라운드 실행. 이미 응답하면 그대로 사용. 성공 시 True."""
    if is_available(settings):
        return True
    launcher = find_launcher(settings)
    if not launcher:
        log("   ❌ ComfyUI 설치 경로를 찾지 못했습니다. [⚙️ 설정]의 'ComfyUI 경로'에 폴더를 지정하세요.")
        return False
    log(f"   🚀 ComfyUI 자동 실행: {launcher}")
    try:
        # 콘솔 창 안 띄우고 백그라운드로 — ComfyUI는 모델 로딩에 수십 초 걸릴 수 있음
        cwd = str(Path(launcher).parent)
        if os.name == "nt":
            proc = subprocess.Popen(
                [launcher], cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                shell=True, creationflags=subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NEW_PROCESS_GROUP)
        else:
            # 맥·리눅스: .sh는 직접 실행, main.py는 python으로 실행. start_new_session으로
            # 앱 종료 후에도 살아있게(윈도우의 DETACHED_PROCESS에 대응).
            cmd = [sys.executable, launcher] if launcher.endswith(".py") else [launcher]
            proc = subprocess.Popen(
                cmd, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)
        try:
            _PID_FILE.write_text(str(proc.pid))
        except Exception:
            pass
    except Exception as e:
        log(f"   ❌ ComfyUI 실행 실패: {e}")
        return False
    # 부팅 대기(모델 로딩 포함, 최대 120초)
    log("   ⏳ ComfyUI 부팅 대기 중(모델 로딩까지 1~2분)...")
    for i in range(120):
        time.sleep(1)
        if is_available(settings):
            log(f"   ✅ ComfyUI 준비 완료 ({i + 1}초)")
            return True
    log("   ❌ 시간 초과 — ComfyUI 창을 직접 열어 정상 실행되는지 확인하세요.")
    return False


def ensure_running(settings: dict, log=print) -> bool:
    """is_available + launch 의 편의 래퍼."""
    return is_available(settings) or launch(settings, log)


def stop(log=print) -> bool:
    """우리가 띄운 ComfyUI 프로세스를 종료."""
    pid = _read_pid()
    if not _is_pid_alive(pid):
        log("   · 종료할 ComfyUI 프로세스가 없습니다.")
        try: _PID_FILE.unlink()
        except Exception: pass
        return False
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=8)
        else:
            import signal
            os.kill(pid, signal.SIGTERM)
        log(f"   🛑 ComfyUI 종료(PID {pid})")
        try: _PID_FILE.unlink()
        except Exception: pass
        return True
    except Exception as e:
        log(f"   ⚠️ 종료 실패: {e}")
        return False


def list_checkpoints(settings: dict) -> list:
    try:
        info = json.loads(_get(comfy_url(settings) + "/object_info/CheckpointLoaderSimple", timeout=6))
        return list(info["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"][0])
    except Exception:
        return []


def _pick_checkpoint(settings: dict) -> str:
    want = (settings.get("comfy_ckpt") or "").strip()
    cks = list_checkpoints(settings)
    if want and want in cks:
        return want
    return cks[0] if cks else want


def _workflow(positive, negative, ckpt, w, h, steps, seed, cfg, sampler, scheduler) -> dict:
    """표준 txt2img 워크플로(API 포맷)."""
    return {
        "3": {"class_type": "KSampler", "inputs": {
            "seed": seed, "steps": steps, "cfg": cfg, "sampler_name": sampler,
            "scheduler": scheduler, "denoise": 1.0,
            "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["5", 0]}},
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}},
        "5": {"class_type": "EmptyLatentImage", "inputs": {"width": w, "height": h, "batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": positive, "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": ["4", 1]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "blogstudio", "images": ["8", 0]}},
    }


def generate(prompt: str, settings: dict, negative: str = None, width: int = 768,
             height: int = 512, steps: int = 22, cfg: float = 7.0, seed: int = None,
             sampler: str = "euler", scheduler: str = "normal",
             ckpt: str = None, timeout: int = 180, log=print) -> bytes:
    """ComfyUI로 이미지 1장 생성 → PNG bytes (실패 시 None)."""
    base = comfy_url(settings)
    ckpt = ckpt or _pick_checkpoint(settings)
    if not ckpt:
        raise RuntimeError("ComfyUI에 체크포인트(모델)가 없습니다. ComfyUI models/checkpoints 확인.")
    seed = seed if seed is not None else uuid.uuid4().int % (2 ** 31)
    wf = _workflow(prompt, negative or NEG_DEFAULT, ckpt, width, height, steps, seed, cfg, sampler, scheduler)
    client_id = uuid.uuid4().hex
    body = json.dumps({"prompt": wf, "client_id": client_id}).encode("utf-8")
    req = urllib.request.Request(base + "/prompt", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            pid = json.loads(r.read().decode("utf-8")).get("prompt_id")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"ComfyUI 워크플로 거부({e.code}) — 체크포인트/노드 확인: {e.read()[:160]}")
    if not pid:
        raise RuntimeError("ComfyUI가 prompt_id를 반환하지 않았습니다.")
    # 완료까지 history 폴링
    for _ in range(timeout):
        time.sleep(1)
        try:
            hist = json.loads(_get(base + "/history/" + pid, timeout=8))
        except Exception:
            continue
        if pid in hist:
            outs = hist[pid].get("outputs", {})
            for node in outs.values():
                for im in node.get("images", []):
                    qs = urllib.parse.urlencode({"filename": im["filename"],
                                                 "subfolder": im.get("subfolder", ""),
                                                 "type": im.get("type", "output")})
                    return _get(base + "/view?" + qs, timeout=30)
            return None
    raise RuntimeError("이미지 생성 시간이 초과됐습니다.")


def _shot_to_prompt(shot: dict) -> str:
    """샷 리스트 항목 → SD 프롬프트(영어 설명 + 검색어 + 품질 태그)."""
    desc = (shot.get("description_en") or shot.get("search_en") or shot.get("heading") or "").strip()
    kw = (shot.get("search_en") or "").strip()
    base = desc if desc else kw
    if kw and kw.lower() not in base.lower():
        base = f"{base}, {kw}"
    return f"{base}, high quality photograph, natural lighting, sharp focus, detailed".strip(", ")


def generate_shot_images(shots: list, settings: dict, out_dir, log=print,
                         width: int = 768, height: int = 512) -> list:
    """샷 리스트 전체를 ComfyUI로 생성해 out_dir에 저장. 파일은 01,02… 순(01=히어로)."""
    if not ensure_running(settings, log):
        raise RuntimeError("ComfyUI를 자동 실행하지 못했습니다. [⚙️ 설정]의 'ComfyUI 경로'를 확인하세요.")
    out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    ckpt = _pick_checkpoint(settings)
    log(f"   🎨 ComfyUI 이미지 생성 시작 — 모델: {ckpt or '(자동)'} / {len(shots)}장")
    paths = []
    for i, s in enumerate(shots, 1):
        prompt = _shot_to_prompt(s)
        log(f"   🎨 {i}/{len(shots)} 생성: {s.get('slot', '')} — {prompt[:50]}")
        try:
            img = generate(prompt, settings, width=width, height=height, ckpt=ckpt, log=log)
        except Exception as e:
            log(f"      ⚠️ {i}번 생성 실패: {e}")
            continue
        if img:
            slot = (s.get("slot") or f"shot{i}").replace(" ", "_").replace("/", "_")
            p = out_dir / f"{i:02d}_{slot}.png"
            p.write_bytes(img)
            paths.append(str(p))
    log(f"   ✅ 이미지 {len(paths)}장 생성 완료 → {out_dir}")
    return paths
