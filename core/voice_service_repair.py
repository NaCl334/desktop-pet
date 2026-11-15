"""Classify speech failures and start one owned service using fallback ports."""
from dataclasses import dataclass
import json
from pathlib import Path
import socket
import threading

from core.config import ROOT, load_json, save_json


_START_LOCK = threading.RLock()
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


@dataclass(frozen=True)
class VoiceIssue:
    kind: str
    message: str
    action: str
    environment_repair: bool = False


def classify_voice_issue(error, runtime_complete=True, diagnostic=""):
    text = str(error)
    direct = text.lower()
    combined = (text + "\n" + diagnostic).lower()
    if any(word in direct for word in ("参考音频", "reference audio", "ref_audio")):
        return VoiceIssue("reference", "当前角色的参考音频缺失或不符合要求，请检查对应语言的参考音频。", "reference")
    if any(word in direct for word in ("权重文件", "ckpt", ".pth", "sovits_weights", "gpt_weights")):
        return VoiceIssue("weights", "当前角色的语音权重缺失或无法加载，请重新选择模型文件。", "weights")
    if any(word in combined for word in ("out of memory", "显存不足", "内存不足")):
        return VoiceIssue("resources", "语音推理资源不足，请关闭占用较大的程序后重试；文字回复仍可使用。", "retry")
    if not runtime_complete or any(word in combined for word in ("no module named", "modulenotfounderror", "dll load failed", "运行副本尚未准备完整")):
        return VoiceIssue("environment", "语音运行环境缺失或依赖加载失败，可以修复环境后启动。", "repair", True)
    if any(word in combined for word in ("端口", "address already in use", "10048", "其他服务", "另一套服务")):
        return VoiceIssue("port", "语音端口不可用，可以检查并切换备用端口后启动。", "repair")
    if any(word in combined for word in ("超时", "timeout", "timed out", "不可用", "连接中断", "启动失败")):
        return VoiceIssue("connection", "语音服务未能正常响应，可以重新检查并启动；文字回复仍可使用。", "repair")
    return VoiceIssue("unknown", "语音处理未完成，暂时无法确定原因。可再次检查，或复制错误信息。", "retry")


def environment_fingerprint():
    from core.gpt_sovits import ENGINE_DIR
    result = []
    for name in ("runtime/python.exe", "venv/Scripts/python.exe", "api_v2.py", "start_api.py",
                 "GPT_SoVITS", "tts_infer_project.yaml"):
        path = ENGINE_DIR / name
        try:
            stat = path.stat()
            result.append((name, stat.st_size, stat.st_mtime_ns))
        except OSError:
            result.append((name, None, None))
    return tuple(result)


def installation_failure(error, install=True):
    return {"state": "failed", "text": str(error),
            "action": "install" if install else "repair", "environment_repair": not install,
            "action_label": "重新安装语音服务" if install else "修复并启动",
            "installation_failure": True, "environment_fingerprint": environment_fingerprint()}


def local_environment_status(client, failure=None):
    """A skipped installation is different from an installed, broken runtime."""
    from core.gpt_sovits import ENGINE_DIR, runtime_ready
    if (failure and failure.get("installation_failure") and client.host in LOCAL_HOSTS
            and failure.get("environment_fingerprint") == environment_fingerprint()):
        return failure
    if client.host not in LOCAL_HOSTS or runtime_ready():
        return None
    installed = any((ENGINE_DIR / name).exists() for name in ("GPT_SoVITS", "venv", "runtime"))
    if not installed:
        return {"state": "missing", "text": "尚未安装本地语音服务，文字对话仍可使用。",
                "action": "install", "action_label": "安装语音服务"}
    return {"state": "failed", "text": "语音环境不完整，可以修复后启动。",
            "action": "repair", "environment_repair": True, "action_label": "修复并启动"}


def install_environment(progress=None, cancel_event=None):
    from core.installer import install_profile
    return install_profile("full", progress=progress, cancel_event=cancel_event)


def port_available(host, port):
    family = socket.AF_INET6 if host == "::1" else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            probe.bind(("127.0.0.1" if host == "localhost" else host, port))
            return True
        except OSError:
            return False


def own_service(client):
    try:
        identity = json.loads(client._request("/desktop_pet/service", timeout=1.5))
        if (isinstance(identity, dict) and identity.get("instance_id") and identity.get("project_root")
                and Path(identity["project_root"]).resolve() == ROOT.resolve()):
            return identity
    except (OSError, RuntimeError, ValueError, TypeError):
        pass
    return None


def candidate_ports(config):
    values = [config.get("last_successful_port"), config.get("port", 9880)]
    fallback = config.get("fallback_ports")
    values += list(fallback if isinstance(fallback, (list, tuple)) else range(9880, 9886))
    result = []
    for value in values:
        try:
            value = int(value)
        except (ValueError, TypeError):
            continue
        if 1 <= value <= 65535 and value not in result:
            result.append(value)
    return result[:8]


def persist_port(client):
    path = ROOT / "config/gpt_sovits.json"
    client.config["last_successful_port"] = client.port
    if path.is_file():
        stored = load_json(path, {})
        if isinstance(stored, dict) and (stored.get("port") != client.port or stored.get("last_successful_port") != client.port):
            stored.update(port=client.port, last_successful_port=client.port)
            temporary = path.with_suffix(".port.tmp")
            try:
                save_json(temporary, stored)
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)


def start_managed_service(client, progress=None, cancelled=lambda: False, persist=persist_port):
    """Never reuse a foreign local /docs endpoint or stop its process."""
    def notify(text):
        if progress:
            progress(text)
    def remember():
        try:
            persist(client)
        except OSError:
            notify("语音服务已连接，但当前目录无法保存成功端口。")
    with _START_LOCK, client._managed_lock:
        if cancelled():
            raise RuntimeError("桌宠正在退出，已取消语音服务启动。")
        if client.host not in LOCAL_HOSTS:
            notify("正在连接已配置的远程语音服务…")
            client.start()
            client.wait_ready()
            return
        process = getattr(client, "process", None)
        if process is not None and process.poll() is None:
            notify("语音服务进程仍在运行，正在确认连接…")
            client.wait_ready()
            identity = own_service(client)
            if identity:
                client._service_identity = identity
                remember()
                return
            raise RuntimeError("语音服务仍在运行但暂未响应身份检查，请稍后重试。")
        original_port = client.config.get("port", 9880)
        try:
            for port in candidate_ports(client.config):
                if cancelled():
                    raise RuntimeError("桌宠正在退出，已取消语音服务启动。")
                client.config["port"] = port
                notify("正在检查语音端口 {}…".format(port))
                if not port_available(client.host, port):
                    identity = own_service(client)
                    if identity:
                        client._service_identity = identity
                        client.wait_ready()
                        remember()
                        return
                    continue
                notify("正在使用端口 {} 加载语音服务…".format(port))
                try:
                    client.start()
                    client.wait_ready()
                except Exception:
                    # A bind race is the only reason to switch after a failed spawn.
                    occupied = not port_available(client.host, port)
                    process = getattr(client, "process", None)
                    if occupied and (process is None or process.poll() is not None) and not own_service(client):
                        client.stop()
                        continue
                    raise
                identity = own_service(client)
                if not identity:
                    # start() may have met a foreign endpoint between probe and bind.
                    client.stop()
                    if not port_available(client.host, port):
                        continue
                    raise RuntimeError("语音服务身份无法确认，未将其标记为就绪。")
                client._service_identity = identity
                remember()
                return
            raise RuntimeError("预设语音端口均不可用，请关闭冲突服务或修改语音端口配置。")
        except BaseException:
            client.config["port"] = original_port
            raise


def repair_environment(progress=None, cancel_event=None):
    from core.installer import install_profile
    return install_profile("full", progress=progress, cancel_event=cancel_event, force_repair=True)


def diagnostic_position():
    path = ROOT / "data/logs/gpt_sovits.stderr.log"
    try:
        return path.stat().st_size
    except OSError:
        return 0


def diagnostic_since(position):
    path = ROOT / "data/logs/gpt_sovits.stderr.log"
    try:
        with path.open("rb") as stream:
            length = stream.seek(0, 2)
            stream.seek(max(min(position, length), length - 8192))
            return stream.read(8192).decode("utf-8", errors="replace")
    except OSError:
        return ""
