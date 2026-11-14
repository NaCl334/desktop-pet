import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from urllib import error as urllib_error
from urllib import request as urllib_request

from core.config import ROOT
from core.environment import base_models_present, detect_environment, profile_ready
from core.setup_packages import bundled_setup_file, voice_package
from core.lgpl_ffmpeg import install_lgpl_tools


class SetupError(RuntimeError):
    pass


_INSTALL_LOCK = threading.Lock()
MINIMUM_FREE_BYTES = 32 * 1024 ** 3


def _check_cancel(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise SetupError("已取消配置，已下载的数据会保留供下次继续。")


def _notify(progress, text, percent=None, component=None, state=None):
    if progress:
        progress({"text": text, "percent": percent, "component": component, "state": state})


def sha256_file(path, chunk_size=1024 * 1024, cancel_event=None):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            _check_cancel(cancel_event)
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def verify_sha256(path, expected_sha256, cancel_event=None):
    expected = str(expected_sha256 or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise SetupError("安装文件缺少有效的 SHA-256 校验值。")
    if sha256_file(path, cancel_event=cancel_event) != expected:
        raise SetupError("安装文件校验失败，请重新下载。")
    return True


def download_verified(url, destination, expected_sha256, progress=None, cancel_event=None,
                      expected_size=None):
    """Resume only validated byte ranges; never deploy an unverified download."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file():
        try:
            verify_sha256(destination, expected_sha256, cancel_event)
            if expected_size is None or destination.stat().st_size == expected_size:
                return destination
        except SetupError:
            _check_cancel(cancel_event)
        destination.unlink()
    temporary = destination.with_suffix(destination.suffix + ".download")
    if expected_size and temporary.exists() and temporary.stat().st_size >= expected_size:
        if temporary.stat().st_size == expected_size:
            try:
                verify_sha256(temporary, expected_sha256, cancel_event)
                temporary.replace(destination)
                return destination
            except SetupError:
                _check_cancel(cancel_event)
        temporary.unlink()
    offset = temporary.stat().st_size if temporary.exists() else 0
    headers = {"User-Agent": "DesktopPet-Installer/1", "Accept-Encoding": "identity"}
    if offset:
        headers["Range"] = "bytes={}-".format(offset)
    request = urllib_request.Request(url, headers=headers)
    _check_cancel(cancel_event)
    with urllib_request.urlopen(request, timeout=25) as response:
        status = response.getcode()
        if status == 206:
            match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)",
                                 response.headers.get("Content-Range", ""))
            if not match or int(match[1]) != offset:
                raise SetupError("下载服务器返回了错误的续传范围。")
            total = int(match[3])
            if expected_size is not None and total != expected_size:
                raise SetupError("下载服务器返回的文件大小与安装清单不符。")
        elif status == 200:
            offset = 0
            total = int(response.headers.get("Content-Length") or 0)
            if total and expected_size is not None and total != expected_size:
                raise SetupError("下载服务器返回的文件大小与安装清单不符。")
        else:
            raise SetupError("下载服务器没有提供可用文件。")
        received = offset
        last_update = 0
        with temporary.open("ab" if offset else "wb") as output:
            while True:
                _check_cancel(cancel_event)
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                received += len(chunk)
                if expected_size and received > expected_size:
                    raise SetupError("下载文件超出预期大小。")
                output.write(chunk)
                if progress and time.monotonic() - last_update >= .15:
                    progress(received, expected_size or total)
                    last_update = time.monotonic()
    if (expected_size or total) and received != (expected_size or total):
        raise SetupError("下载尚未完成，重试时会继续下载。")
    try:
        verify_sha256(temporary, expected_sha256, cancel_event)
    except SetupError:
        _check_cancel(cancel_event)
        temporary.unlink(missing_ok=True)
        raise
    temporary.replace(destination)
    if progress:
        progress(received, expected_size or total)
    return destination


def _safe_remove(path):
    target = Path(path).resolve()
    root = ROOT.resolve()
    if target == root or root not in target.parents:
        raise SetupError("拒绝删除项目目录之外或项目根目录。")
    native = str(target)
    if os.name == "nt" and not native.startswith("\\\\?\\"):
        native = "\\\\?\\UNC\\" + native[2:] if native.startswith("\\\\") else "\\\\?\\" + native
    if target.is_dir():
        shutil.rmtree(native)
    elif os.path.exists(native):
        os.unlink(native)


def _run(command, cwd, progress=None, cancel_event=None, env=None, timeout=600):
    """Keep cancellation responsive even when a child emits no output."""
    _check_cancel(cancel_event)
    process = subprocess.Popen(
        [str(item) for item in command], cwd=str(cwd), stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), env=env,
    )
    lines = queue.Queue()

    def read_output():
        try:
            for line in process.stdout:
                lines.put(line.rstrip())
        finally:
            lines.put(None)

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    output = []
    deadline = time.monotonic() + timeout
    try:
        while True:
            _check_cancel(cancel_event)
            if time.monotonic() > deadline:
                raise SetupError("组件检查或解压超时，请重试。")
            try:
                line = lines.get(timeout=.1)
            except queue.Empty:
                continue
            if line is None:
                break
            output.append(line)
            if progress:
                progress(line)
        process.wait(timeout=5)
        if process.returncode:
            raise SetupError("组件操作失败（退出码 {}）：{}".format(
                process.returncode, "\n".join(output[-8:])))
        return "\n".join(output)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        reader.join(timeout=2)
        process.stdout.close()


def _archive_parts(name):
    normalized = name.replace("\\", "/").rstrip("/")
    if (not normalized or normalized.startswith("/") or ":" in normalized
            or any(part == ".." for part in normalized.split("/"))):
        raise SetupError("安装包包含不安全的路径。")
    return PurePosixPath(normalized).parts


def _unpack_voice(archive, destination, progress, cancel_event):
    extractor = bundled_setup_file("7zip/7za.exe")
    if not extractor.is_file():
        raise SetupError("安装程序缺少便携解压组件，请重新获取完整安装包。")
    verify_sha256(extractor, "7ddf0a3bc451e166679296be630d6f0573eb386159c04184761c43020c484291", cancel_event)
    tar = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32" / "tar.exe"
    if not tar.is_file():
        raise SetupError("缺少 Windows 自带的 tar.exe，请使用受支持的 Windows 10/11 系统。")
    listing = _run([tar, "-tf", archive], ROOT, cancel_event=cancel_event)
    names = [name for name in listing.splitlines() if name]
    for name in names:
        _archive_parts(name)
    if not names:
        raise SetupError("安装包中没有可用内容。")
    destination.mkdir(parents=True, exist_ok=True)
    last_update = 0

    def extracted(line):
        nonlocal last_update
        match = re.search(r"(\d{1,3})%", line)
        if not match:
            return
        percent = min(int(match[1]), 100)
        if time.monotonic() - last_update >= .2:
            _notify(progress, "正在解压语音环境：{}%…".format(percent), 75 + .13 * percent)
            last_update = time.monotonic()

    _run([extractor, "x", "-y", "-aoa", "-mmt=on", "-bsp1", "-bso0", "-bse1",
          "-sccUTF-8", "-o" + str(destination), archive],
         ROOT, extracted, cancel_event, timeout=3600)
    candidates = [destination] + [path for path in destination.iterdir() if path.is_dir()]
    engine = next((path for path in candidates if
                   (path / "api_v2.py").is_file() and (path / "GPT_SoVITS").is_dir()), None)
    if engine is None:
        raise SetupError("安装包中没有找到 GPT-SoVITS 推理引擎。")
    return engine


def _prepare_engine(engine):
    wrapper = bundled_setup_file("start_api.py")
    if not wrapper.is_file():
        raise SetupError("安装程序缺少语音服务启动模板。")
    shutil.copyfile(wrapper, engine / "start_api.py")
    config = {"custom": {
        "device": "cpu", "is_half": False, "version": "v2Pro",
        "t2s_weights_path": "GPT_SoVITS/pretrained_models/s1v3.ckpt",
        "vits_weights_path": "GPT_SoVITS/pretrained_models/v2Pro/s2Gv2Pro.pth",
        "bert_base_path": "GPT_SoVITS/pretrained_models/chinese-roberta-wwm-ext-large",
        "cnhuhbert_base_path": "GPT_SoVITS/pretrained_models/chinese-hubert-base",
    }}
    # JSON is valid YAML; no installer-side PyYAML dependency is required.
    (engine / "tts_infer_project.yaml").write_text(json.dumps(config, indent=2), encoding="utf-8")
    (engine / "temp").mkdir(exist_ok=True)


def _validate_engine(engine, progress=None, cancel_event=None, load_models=True):
    python = next((path for path in (engine / "runtime/python.exe",
                   engine / "venv/Scripts/python.exe", engine / "python.exe") if path.is_file()), None)
    if python is None or not base_models_present(engine):
        raise SetupError("语音环境或基础模型文件不完整。")
    ffmpeg = next((path for path in (engine / "ffmpeg.exe", engine / "ffmpeg/ffmpeg.exe",
                  engine / "runtime/ffmpeg.exe") if path.is_file()), None)
    if ffmpeg is None:
        raise SetupError("语音安装包中缺少 FFmpeg。")
    environment = os.environ.copy()
    for key in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "VIRTUAL_ENV_PROMPT"):
        environment.pop(key, None)
    environment.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8:replace")
    environment["PATH"] = str(ffmpeg.parent) + os.pathsep + str(python.parent) + os.pathsep + environment.get("PATH", "")
    _run([ffmpeg, "-version"], engine, cancel_event=cancel_event, env=environment, timeout=20)
    probe = ("import sys;sys.path[:0]=['.','GPT_SoVITS'];"
             "import torch,torchaudio,fastapi,uvicorn,transformers,pyopenjtalk;"
             "from TTS_infer_pack.TTS import TTS,TTS_Config;")
    if load_models:
        probe += "model=TTS(TTS_Config('tts_infer_project.yaml'));"
    probe += "print('DESKTOP_PET_SETUP_READY')"
    result = _run([python, "-c", probe], engine, cancel_event=cancel_event,
                  env=environment, timeout=600)
    if "DESKTOP_PET_SETUP_READY" not in result.splitlines():
        raise SetupError("语音环境检查没有正常完成。")
    return python


def _obtain_package(package, progress, cancel_event):
    local = ROOT / "setup_components" / package["filename"]
    if local.is_file():
        _notify(progress, "正在校验随包语音组件…", 5)
        verify_sha256(local, package["sha256"], cancel_event)
        if local.stat().st_size != package["size"]:
            raise SetupError("随包组件的文件大小不正确。")
        return local
    cache = ROOT / "temp" / "setup_downloads" / package["filename"]
    last_error = None
    for attempt in range(3):
        _check_cancel(cancel_event)
        if attempt:
            _notify(progress, "连接失败，正在重试（{}/3）…".format(attempt + 1))

        def downloaded(received, total):
            text = "正在下载语音环境：{:.2f} / {:.2f} GB".format(received / 1e9, total / 1e9)
            _notify(progress, text, 5 + 65 * received / max(total, 1))

        try:
            return download_verified(package["urls"][attempt % len(package["urls"])],
                                     cache, package["sha256"], downloaded, cancel_event, package["size"])
        except (OSError, SetupError, urllib_error.URLError) as error:
            _check_cancel(cancel_event)
            last_error = error
    raise SetupError("语音组件下载失败，已保留下载进度。请检查网络后点击重试。原因：{}".format(last_error))


def install_profile(profile, progress=None, cancel_event=None, force_repair=False):
    _check_cancel(cancel_event)
    try:
        with tempfile.TemporaryFile(dir=ROOT):
            pass
    except OSError as error:
        raise SetupError("安装程序所在目录无法写入，请将整个程序文件夹移至可写目录后重试：{}".format(ROOT)) from error
    if profile == "desktop":
        _notify(progress, "桌宠运行环境已就绪。", 100, "python", "已就绪")
        return None
    if profile != "full":
        raise SetupError("未知安装模式：{}".format(profile))
    if not _INSTALL_LOCK.acquire(blocking=False):
        raise SetupError("另一个语音环境配置任务正在运行。")
    stage = None
    try:
        _notify(progress, "正在检查已有语音环境…", 2)
        report = detect_environment()
        engine = ROOT / "engines/gpt_sovits"
        if profile_ready(report, "full") and not force_repair:
            _notify(progress, "正在验证已有语音引擎与基础模型…", 90)
            install_lgpl_tools(engine, progress, cancel_event)
            _validate_engine(engine, progress, cancel_event)
            _notify(progress, "现有语音环境验证通过。", 100)
            return report
        if os.name != "nt":
            raise SetupError("自动配置语音环境目前支持 Windows 10/11 x64。")
        if shutil.disk_usage(ROOT).free < MINIMUM_FREE_BYTES:
            raise SetupError("配置本地语音需要至少 32 GiB 可用空间（含下载缓存和解压临时文件）。")
        package = voice_package()
        archive = _obtain_package(package, progress, cancel_event)
        _notify(progress, "下载与校验完成，正在准备解压…", 75)
        stage_parent = ROOT / "temp" / "setup_staging"
        stage_parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix="voice_", dir=stage_parent))
        extracted = _unpack_voice(archive, stage, progress, cancel_event)
        _prepare_engine(extracted)
        install_lgpl_tools(extracted, progress, cancel_event)
        _notify(progress, "正在检查推理依赖并加载基础语音模型，首次检查可能需要几分钟…", 90)
        _validate_engine(extracted, progress, cancel_event)
        _check_cancel(cancel_event)
        engine.parent.mkdir(parents=True, exist_ok=True)
        backup = None
        if engine.exists():
            backup = stage_parent / ("previous_" + stage.name)
            engine.rename(backup)
        try:
            extracted.rename(engine)
            (ROOT / "models/gpt_sovits").mkdir(parents=True, exist_ok=True)
            _notify(progress, "正在确认安装后的运行路径…", 97)
            _validate_engine(engine, progress, cancel_event, load_models=False)
            report = detect_environment()
            if not profile_ready(report, "full"):
                raise SetupError("安装后环境检查未通过，已还原原有语音目录。")
        except BaseException:
            if engine.exists():
                _safe_remove(engine)
            if backup is not None and backup.exists():
                backup.rename(engine)
            raise
        _notify(progress, "本地语音环境配置完成；角色音色在添加角色时导入。", 100)
        return report
    finally:
        try:
            if stage is not None and stage.exists():
                _safe_remove(stage)
        finally:
            _INSTALL_LOCK.release()
