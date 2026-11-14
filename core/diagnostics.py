import json
import os
import shutil
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from core.config import ROOT, get_character_dir, load_character_config, load_voice_config
from core.environment import detect_environment
from core.portrait_processing import IMAGE_EXTENSIONS


DIAGNOSTICS_DIR = ROOT / "data" / "diagnostics"
MAX_REPORTS = 8
MAX_REPORT_BYTES = 256 * 1024
GPT_SOVITS_LOG = ROOT / "data" / "logs" / "gpt_sovits.stdout.log"


def _port_status(host="127.0.0.1", port=9880):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.8)
    try:
        return bool(sock.connect_ex((host, port)) == 0)
    except OSError:
        return False
    finally:
        sock.close()


def _process_summary():
    try:
        completed = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="mbcs",
            errors="replace",
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        names = []
        for line in completed.stdout.splitlines():
            name = line.split(",", 1)[0].strip('"').lower()
            if name in {"python.exe", "pythonw.exe", "ffmpeg.exe"}:
                names.append(name)
        return {"available": True, "matching": names, "count": len(names)}
    except (OSError, subprocess.SubprocessError):
        return {"available": False, "matching": [], "count": 0}


def _resource_status(character_id):
    character_dir = get_character_dir(character_id)
    character = load_character_config(character_id)
    voice = load_voice_config(character_id)
    portraits_dir = character_dir / "portraits"
    try:
        has_portraits = any(
            path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
            for path in portraits_dir.rglob("*")
        )
    except OSError:
        has_portraits = False
    model = voice.get("model", {}) if isinstance(voice, dict) else {}
    model_paths = []
    for key in ("ckpt_file", "pth_file"):
        value = str(model.get(key, "")).strip()
        if value:
            model_paths.append(character_dir / "voice" / value)
    references = []
    groups = voice.get("language_references", {}) if isinstance(voice, dict) else {}
    if isinstance(groups, dict):
        for item in groups.values():
            if isinstance(item, dict) and str(item.get("audio", "")).strip():
                references.append(character_dir / "voice" / str(item["audio"]))
    default_audio = str(voice.get("reference", {}).get("default_audio", "")).strip()
    if default_audio:
        references.append(character_dir / "voice" / default_audio)
    return {
        "character": (character_dir / "character.json").is_file(),
        "portraits": has_portraits,
        "models": all(path.is_file() for path in model_paths) if model_paths else False,
        "references": any(path.is_file() for path in references),
        "paths": {
            "character": str(character_dir / "character.json"),
            "portraits": [str(portraits_dir)],
            "models": [str(path) for path in model_paths],
            "references": [str(path) for path in references],
        },
    }


def _gpt_sovits_log_summary():
    if not GPT_SOVITS_LOG.is_file():
        return []
    try:
        text = GPT_SOVITS_LOG.read_text(encoding="utf-8", errors="replace")
    except OSError:
        try:
            text = GPT_SOVITS_LOG.read_text(encoding="mbcs", errors="replace")
        except OSError:
            return []
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    relevant = [
        line for line in lines
        if "POST /tts" in line or "Bad Request" in line or "Traceback" in line
    ]
    return relevant[-12:]


def run_diagnostics(character_id="huadan", recent_errors=None):
    environment = detect_environment()
    components = environment.get("components", {})
    resource = _resource_status(character_id)
    report = {
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "python": str(Path(sys.executable).resolve()),
        "character_id": character_id,
        "environment": {
            key: {
                "available": bool(value.get("available")),
                "summary": str(value.get("summary", ""))[:300],
            }
            for key, value in components.items()
        },
        "gpt_sovits_9880": {
            "available": _port_status(),
            "summary": "9880 端口可连接" if _port_status() else "9880 端口未连接",
        },
        "resources": resource,
        "processes": _process_summary(),
        "recent_errors": [str(item)[:500] for item in (recent_errors or [])[-5:]],
        "gpt_sovits_log": _gpt_sovits_log_summary(),
    }
    return report


def save_diagnostic_report(report):
    DIAGNOSTICS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = DIAGNOSTICS_DIR / "diagnostic_{}.txt".format(stamp)
    lines = [
        "桌宠一键诊断报告",
        "检查时间：{}".format(report.get("checked_at", "")),
        "当前角色：{}".format(report.get("character_id", "")),
        "",
        "【检查结果】",
    ]
    failures = []
    labels = {
        "python": "当前 Python",
        "torch": "PyTorch",
        "compute": "CUDA/CPU",
        "ffmpeg": "FFmpeg",
        "gpt_sovits_runtime": "GPT-SoVITS 运行环境",
        "gpt_sovits_model": "GPT-SoVITS 模型",
    }
    for key, item in report.get("environment", {}).items():
        label = labels.get(key, key)
        passed = bool(item.get("available"))
        summary = str(item.get("summary", "")).strip()
        lines.append("{}：{}{}".format(label, "通过" if passed else "失败", "（{}）".format(summary) if summary else ""))
        if not passed:
            failures.append("{}：{}".format(label, summary or "不可用"))
    port = report.get("gpt_sovits_9880", {})
    port_ok = bool(port.get("available"))
    lines.append("GPT-SoVITS 9880 端口：{}（{}）".format("通过" if port_ok else "失败", port.get("summary", "")))
    if not port_ok:
        failures.append("GPT-SoVITS 9880 端口：{}".format(port.get("summary", "未连接")))
    resources = report.get("resources", {})
    resource_labels = {"character": "角色配置文件", "portraits": "角色立绘", "models": "角色语音模型", "references": "参考音频"}
    for key, label in resource_labels.items():
        passed = bool(resources.get(key))
        lines.append("{}：{}".format(label, "通过" if passed else "失败"))
        if not passed:
            paths = resources.get("paths", {}).get(key, [])
            failures.append("{}：文件或目录不存在；检查路径：{}".format(label, "、".join(paths) if paths else "未配置"))
    processes = report.get("processes", {})
    process_ok = bool(processes.get("available"))
    lines.append("Python/FFmpeg 进程检查：{}（发现 {} 个相关进程）".format("通过" if process_ok else "失败", processes.get("count", 0)))
    if not process_ok:
        failures.append("进程检查：无法读取当前进程列表")
    lines.extend(["", "【最近异常】"])
    recent_errors = report.get("recent_errors", [])
    lines.extend("- {}".format(item) for item in recent_errors) if recent_errors else lines.append("未记录最近异常。")
    lines.extend(["", "【GPT-SoVITS 服务日志摘要】"])
    gpt_log = report.get("gpt_sovits_log", [])
    lines.extend("- {}".format(item) for item in gpt_log) if gpt_log else lines.append("未找到 GPT-SoVITS /tts 服务日志。")
    lines.extend(["", "【最终结论】"])
    if failures:
        lines.append("发现 {} 项问题：".format(len(failures)))
        lines.extend("- {}".format(item) for item in failures)
    else:
        lines.append("诊断完成，没有出现错误。")
    path.write_text("\n".join(lines)[:MAX_REPORT_BYTES], encoding="utf-8")
    reports = sorted(DIAGNOSTICS_DIR.glob("diagnostic_*.txt"), key=lambda item: item.stat().st_mtime)
    for old in reports[:-MAX_REPORTS]:
        try:
            old.unlink()
        except OSError:
            pass
    return path
