import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from core.config import ROOT


ENGINES_DIR = ROOT / "engines"
MODELS_DIR = ROOT / "models"
GPT_SOVITS_ENGINE_DIR = ENGINES_DIR / "gpt_sovits"
GPT_SOVITS_MODEL_DIR = MODELS_DIR / "gpt_sovits"
RUNTIME_DIR = ROOT / "runtime"


INSTALL_PROFILES = {
    "desktop": {
        "label": "仅桌宠模式",
        "download": "0 MB",
        "disk": "约 50 MB（不含现有项目）",
        "admin": "否",
        "network": "否",
        "description": "只运行现有桌宠功能，不安装本地模型。",
    },
    "full": {
        "label": "桌宠 + GPT-SoVITS",
        "download": "已有完整环境时 0 MB；缺失时约 8.2–8.9 GB",
        "disk": "至少 32 GiB 可用空间（含下载缓存和解压临时文件）",
        "admin": "否（程序目录需要可写）",
        "network": "组件缺失时需要",
        "description": "复用已有完整环境；缺失时下载并配置官方 Windows 语音组件。",
    },
}


def _python_path(directory):
    if os.name == "nt":
        return Path(directory) / "Scripts" / "python.exe"
    return Path(directory) / "bin" / "python"


def portable_python_path():
    return RUNTIME_DIR / ("python.exe" if os.name == "nt" else "python")


def _refresh_portable_venv(venv_dir):
    """让发布包内复制的 Windows venv 指向解压后的实际运行时目录。"""
    runtime_python = portable_python_path()
    config_path = Path(venv_dir) / "pyvenv.cfg"
    if not runtime_python.is_file() or not config_path.is_file():
        return
    runtime = str(RUNTIME_DIR.resolve())
    content = "\n".join(
        (
            "home = {}".format(runtime),
            "include-system-site-packages = false",
            "version = 3.11",
            "executable = {}".format(runtime_python.resolve()),
            "command = portable-runtime",
            "",
        )
    )
    try:
        if config_path.read_text(encoding="utf-8") != content:
            config_path.write_text(content, encoding="utf-8")
    except OSError:
        # 环境检测会继续探测，无法写入时由后续探测结果给出明确状态。
        pass




def gpt_python_path():
    venv_dir = GPT_SOVITS_ENGINE_DIR / "venv"
    _refresh_portable_venv(venv_dir)
    candidate = _python_path(venv_dir)
    if candidate.is_file():
        return candidate
    for candidate in (GPT_SOVITS_ENGINE_DIR / "runtime" / "python.exe",
                      GPT_SOVITS_ENGINE_DIR / "python.exe"):
        if candidate.is_file():
            return candidate
    return Path(sys.executable) if not getattr(sys, "frozen", False) else candidate


def _run_python_probe(python_path):
    code = (
        "import importlib.util,json,platform,sys;"
        "names=['torch','fastapi','uvicorn'];"
        "result={'python':sys.executable,'version':platform.python_version()};"
        "result.update({name:bool(importlib.util.find_spec(name)) for name in names});"
        "cuda=False;"
        "torch=__import__('torch') if result['torch'] else None;"
        "cuda=bool(torch.cuda.is_available()) if torch else False;"
        "result['cuda']=cuda;"
        "print(json.dumps(result))"
    )
    try:
        completed = subprocess.run(
            [str(python_path), "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if completed.returncode != 0:
        return {}
    try:
        return json.loads(completed.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {}


def _has_payload(directory):
    directory = Path(directory)
    if not directory.is_dir():
        return False
    for path in directory.rglob("*"):
        if path.is_file() and path.name.lower() not in ("readme.md", ".gitkeep"):
            return True
    return False




def _find_ffmpeg():
    candidates = (
        shutil.which("ffmpeg"),
        str(GPT_SOVITS_ENGINE_DIR / "ffmpeg" / "ffmpeg.exe"),
        str(GPT_SOVITS_ENGINE_DIR / "ffmpeg.exe"),
        str(GPT_SOVITS_ENGINE_DIR / "runtime" / "ffmpeg.exe"),
        str(ROOT / "ffmpeg.exe"),
    )
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    return ""


def _gpt_runtime_present():
    gpt_python = gpt_python_path()
    if not GPT_SOVITS_ENGINE_DIR.is_dir() or not gpt_python.is_file():
        return False
    markers = (
        "api.py",
        "api_v2.py",
        "run.bat",
        "webui.py",
        "GPT_SoVITS",
    )
    return (
        any((GPT_SOVITS_ENGINE_DIR / marker).exists() for marker in markers)
        and any((GPT_SOVITS_ENGINE_DIR / layout).is_dir() for layout in
                ("venv/Lib/site-packages", "runtime/Lib/site-packages", "Lib/site-packages"))
    )


def base_models_present(engine_dir=None):
    engine = Path(engine_dir) if engine_dir is not None else GPT_SOVITS_ENGINE_DIR
    pretrained = engine / "GPT_SoVITS" / "pretrained_models"
    required = (
        "chinese-roberta-wwm-ext-large/config.json",
        "chinese-roberta-wwm-ext-large/pytorch_model.bin",
        "chinese-roberta-wwm-ext-large/tokenizer.json",
        "chinese-hubert-base/config.json",
        "chinese-hubert-base/pytorch_model.bin",
        "s1v3.ckpt", "v2Pro/s2Gv2Pro.pth", "sv/pretrained_eres2netv2w24s4ep4.ckpt",
    )
    return all((pretrained / name).is_file() and (pretrained / name).stat().st_size > 0
               for name in required)


def live2d_runtime_roots():
    roots = [ROOT / "runtime" / "live2d"]
    if getattr(sys, "frozen", False):
        roots.append(Path(getattr(sys, "_MEIPASS", ROOT)) / "runtime" / "live2d")
    return roots


def live2d_present():
    return any((root / "live2d" / "_live2d.pyd").is_file() for root in live2d_runtime_roots())


def detect_environment():
    # A frozen GUI executable is not a Python command-line interpreter.
    frozen = bool(getattr(sys, "frozen", False))
    current_probe = {} if frozen else _run_python_probe(Path(sys.executable))
    gpt_path = gpt_python_path()
    gpt_probe = (current_probe if gpt_path == Path(sys.executable) else
                 _run_python_probe(gpt_path)) if not (frozen and gpt_path == Path(sys.executable)) else {}
    ffmpeg_path = _find_ffmpeg()
    gpt_model_present = base_models_present()
    report = {
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "platform": platform.platform(),
        "paths": {
            "python": str(Path(sys.executable).resolve()),
            "gpt_python": str(gpt_path.resolve()),
            "gpt_sovits_engine": str(GPT_SOVITS_ENGINE_DIR.resolve()),
            "gpt_sovits_model": str(GPT_SOVITS_MODEL_DIR.resolve()),
            "ffmpeg": ffmpeg_path,
        },
        "components": {
            "live2d": {
                "available": live2d_present(),
                "summary": "已安装" if live2d_present() else "未安装（可继续使用立绘角色；请使用含 Live2D 组件的本体）",
            },
            "python": {
                "available": frozen or bool(current_probe),
                "summary": current_probe.get("version", sys.version.split()[0]),
            },
            "torch": {
                "available": bool(
                    gpt_probe.get("torch") and gpt_probe.get("fastapi") and gpt_probe.get("uvicorn")
                ),
                "summary": "已安装" if (
                    gpt_probe.get("torch") and gpt_probe.get("fastapi") and gpt_probe.get("uvicorn")
                ) else "未安装",
            },
            "compute": {
                "available": True,
                "summary": "CUDA" if (
                    gpt_probe.get("cuda")
                    or current_probe.get("cuda")
                ) else "CPU",
            },
            "ffmpeg": {
                "available": bool(ffmpeg_path),
                "summary": ffmpeg_path or "未找到",
            },
            "gpt_sovits_runtime": {
                "available": _gpt_runtime_present(),
                "summary": (
                    "运行副本和项目环境就绪"
                    if _gpt_runtime_present()
                    else "未发现运行副本或项目环境"
                ),
            },
            "gpt_sovits_model": {
                "available": gpt_model_present,
                "summary": "基础模型就绪；角色音色另行导入" if gpt_model_present else "基础语音模型不完整",
            },
        },
    }
    report["missing"] = [
        key
        for key, item in report["components"].items()
        if not item["available"] and key not in ("torch", "compute", "ffmpeg", "live2d")
    ]
    return report


def profile_ready(report, profile):
    if profile == "desktop":
        return True
    components = report.get("components", {})
    if profile != "full":
        return False
    return all(
        components.get(key, {}).get("available", False)
        for key in ("torch", "ffmpeg", "gpt_sovits_runtime", "gpt_sovits_model")
    )
