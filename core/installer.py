import hashlib
import json
import logging
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
from core.setup_packages import PACKAGES, bundled_setup_file, voice_package
from core.lgpl_ffmpeg import install_lgpl_tools, obtain_lgpl_archive
from core.lgpl_ffmpeg import PACKAGE as FFMPEG_PACKAGE


class SetupError(RuntimeError):
    pass


_INSTALL_LOCK = threading.Lock()
MINIMUM_FREE_BYTES = 32 * 1024 ** 3
GIB = 1024 ** 3
INSTALL_FREE_BYTES = 21 * GIB  # 20 GiB extracted estimate + 1 GiB margin.


def setup_options(root=None):
    """下载缓存由程序内部管理，成功后默认清理，不再读取旧的界面选项。"""
    root = Path(root or ROOT)
    return {"cache_dir": str(root / "temp"), "keep_cache": False}


def _existing_parent(path):
    path = Path(path).expanduser().resolve()
    while not path.exists() and path.parent != path:
        path = path.parent
    return path


def recommended_cache_dir():
    """Offer the roomiest local fixed drive; never create directories here."""
    candidates = [ROOT / "temp"]
    if os.name == "nt":
        import ctypes
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            drive = letter + ":\\"
            if ctypes.windll.kernel32.GetDriveTypeW(drive) == 3:
                candidates.append(Path(drive) / "DesktopPetCache")
    usable = []
    for path in candidates:
        try:
            usable.append((shutil.disk_usage(_existing_parent(path)).free, path))
        except OSError:
            continue
    return max(usable, key=lambda item: item[0])[1] if usable else ROOT / "temp"


def preflight_space(install_dir=None, cache_dir=None, package_size=None):
    """Fast check before hardware detection, confirmation or any download."""
    install = Path(install_dir or ROOT).expanduser().resolve()
    cache = Path(cache_dir or setup_options()["cache_dir"]).expanduser()
    if not cache.is_absolute():
        raise SetupError("缓存位置必须是绝对路径。")
    cache = cache.resolve()
    target_parent, cache_parent = _existing_parent(install), _existing_parent(cache)
    same_volume = target_parent.stat().st_dev == cache_parent.stat().st_dev
    size = package_size if package_size is not None else max(p["size"] for p in PACKAGES.values())
    requirements = [(install, MINIMUM_FREE_BYTES)] if same_volume else [
        (install, INSTALL_FREE_BYTES), (cache, size + GIB)]
    for path, required in requirements:
        free = shutil.disk_usage(_existing_parent(path)).free
        if free < required:
            label = path.anchor.rstrip("\\/") or str(path)
            amount = "{:g}".format(required / GIB) if required % GIB == 0 else "{:.1f}".format(required / GIB)
            raise SetupError("需要 {} GiB，{} 当前可用 {:.1f} GiB，还差 {:.1f} GiB。"
                "请释放磁盘空间，或选择空间充足的安装位置。".format(
                    amount, label, free / GIB, (required - free) / GIB))
    return {"install_dir": install, "cache_dir": cache, "same_volume": same_volume}


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
                      expected_size=None, request_headers=None):
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
    if request_headers:
        headers.update(request_headers)
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


def _voice_archive_names(listing):
    """Read verified 7-Zip technical listings without decoding solid blocks."""
    entries = listing.partition("----------")[2]
    names = []
    for line in entries.splitlines():
        if line.startswith("Path = "):
            name = line[len("Path = "):]
            _archive_parts(name)
            names.append(name.replace("\\", "/"))
        elif line.startswith(("Symbolic Link = ", "Hard Link = ")):
            raise SetupError("语音安装包不能包含链接文件。")
    if not names:
        raise SetupError("安装包中没有可用内容。")
    return names


def _voice_archive_exclusions(names):
    """Exclude training utility weights and bytecode only when source exists."""
    paths = set(names)
    prefix = next((name[:-len("api_v2.py")] for name in names
                   if PurePosixPath(name).name == "api_v2.py" and len(PurePosixPath(name).parts) <= 2), None)
    excluded = []
    for name in names:
        relative = name[len(prefix):] if prefix is not None and name.startswith(prefix) else ""
        if any(relative == folder or relative.startswith(folder + "/") for folder in
               ("tools/asr/models", "tools/uvr5/uvr5_weights")):
            excluded.append(name)
            continue
        path = PurePosixPath(name)
        if path.parent.name == "__pycache__" and path.suffix == ".pyc":
            source = path.parent.parent / (path.name.split(".")[0] + ".py")
            if source.as_posix() in paths:
                excluded.append(name)
    return excluded


def _unpack_voice(archive, destination, progress, cancel_event):
    extractor = bundled_setup_file("7zip/7za.exe")
    if not extractor.is_file():
        raise SetupError("安装程序缺少便携解压组件，请重新获取完整安装包。")
    verify_sha256(extractor, "7ddf0a3bc451e166679296be630d6f0573eb386159c04184761c43020c484291", cancel_event)
    _notify(progress, "正在读取语音安装包目录…", 75, "gpt_sovits_runtime", "读取目录中")
    listing = _run([extractor, "l", "-slt", "-sccUTF-8", "--", archive],
                   ROOT, cancel_event=cancel_event)
    names = _voice_archive_names(listing)
    exclusions = _voice_archive_exclusions(names)
    destination.mkdir(parents=True, exist_ok=True)
    last_update = 0

    def extracted(line):
        nonlocal last_update
        match = re.search(r"(\d{1,3})%", line)
        if not match:
            return
        percent = min(int(match[1]), 100)
        if time.monotonic() - last_update >= .2:
            _notify(progress, "正在解压语音环境：{}%…".format(percent), 75 + .13 * percent,
                    "gpt_sovits_runtime", "解压中 {}%".format(percent))
            last_update = time.monotonic()

    with tempfile.TemporaryDirectory(prefix="voice_exclusions_", dir=destination.parent) as rules:
        exclude_file = Path(rules) / "exclude.txt"
        exclude_file.write_text("\n".join(exclusions), encoding="utf-8")
        _notify(progress, "正在解压语音环境（仅安装推理所需内容）…", 75,
                "gpt_sovits_runtime", "解压中")
        _run([extractor, "x", "-y", "-aoa", "-mmt=on", "-bsp1", "-bso0", "-bse1",
              "-sccUTF-8", "-scsUTF-8", "-x@" + str(exclude_file),
              "-o" + str(destination), "--", archive],
             ROOT, extracted, cancel_event, timeout=3600)
    _notify(progress, "语音文件解压完成，准备检查…", 88,
            "gpt_sovits_runtime", "已解压，待检查")
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
    environment.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8:replace", PYTHONDONTWRITEBYTECODE="1")
    environment["PATH"] = str(ffmpeg.parent) + os.pathsep + str(python.parent) + os.pathsep + environment.get("PATH", "")
    _run([ffmpeg, "-version"], engine, cancel_event=cancel_event, env=environment, timeout=20)
    _notify(progress, "音频处理工具检查通过。", component="ffmpeg", state="已就绪")
    _notify(progress, "正在检查语音计算依赖…", component="torch", state="检查中")
    probe = ("import sys;sys.path[:0]=['.','GPT_SoVITS'];"
             "import torch,torchaudio,fastapi,uvicorn,transformers,pyopenjtalk;"
             "from TTS_infer_pack.TTS import TTS,TTS_Config;"
             "print('DESKTOP_PET_DEPENDENCIES_READY',flush=True);")
    if load_models:
        probe += "model=TTS(TTS_Config('tts_infer_project.yaml'));"
    probe += "print('DESKTOP_PET_SETUP_READY')"
    def checked(line):
        if line == "DESKTOP_PET_DEPENDENCIES_READY":
            _notify(progress, "语音计算依赖检查通过。", component="torch", state="已就绪")
            if load_models:
                _notify(progress, "正在加载基础语音模型…", component="gpt_sovits_model", state="加载中")

    result = _run([python, "-c", probe], engine, progress=checked, cancel_event=cancel_event,
                  env=environment, timeout=600)
    if "DESKTOP_PET_SETUP_READY" not in result.splitlines():
        raise SetupError("语音环境检查没有正常完成。")
    _notify(progress, "语音引擎检查通过。", component="gpt_sovits_runtime", state="已就绪")
    if load_models:
        _notify(progress, "基础语音模型加载成功。", component="gpt_sovits_model", state="已就绪")
    return python


def _obtain_package(package, progress, cancel_event, cache_dir=None):
    local = ROOT / "setup_components" / package["filename"]
    if local.is_file():
        _notify(progress, "正在校验随包语音组件…", 5, "gpt_sovits_runtime", "校验中")
        verify_sha256(local, package["sha256"], cancel_event)
        if local.stat().st_size != package["size"]:
            raise SetupError("随包组件的文件大小不正确。")
        return local
    cache = Path(cache_dir or setup_options()["cache_dir"]) / "setup_downloads" / package["filename"]
    last_error = None
    for attempt in range(3):
        _check_cancel(cancel_event)
        if attempt:
            _notify(progress, "连接失败，正在重试（{}/3）…".format(attempt + 1))

        def downloaded(received, total):
            text = "正在下载语音环境：{:.2f} / {:.2f} GB".format(received / 1e9, total / 1e9)
            _notify(progress, text, 5 + 65 * received / max(total, 1), "gpt_sovits_runtime", "下载中")

        try:
            return download_verified(package["urls"][attempt % len(package["urls"])],
                                     cache, package["sha256"], downloaded, cancel_event, package["size"])
        except (OSError, SetupError, urllib_error.URLError) as error:
            _check_cancel(cancel_event)
            last_error = error
    raise SetupError("语音组件下载失败，已保留下载进度。请检查网络后点击重试。原因：{}".format(last_error))


def install_profile(profile, progress=None, cancel_event=None, force_repair=False,
                    cache_dir=None, keep_cache=None):
    _check_cancel(cancel_event)
    options = setup_options()
    cache_dir = Path(cache_dir or options["cache_dir"])
    keep_cache = options["keep_cache"] if keep_cache is None else keep_cache
    if profile == "full":
        preflight_space(cache_dir=cache_dir)
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
        _notify(progress, "正在检查已有语音环境…", 2, "gpt_sovits_runtime", "检查中")
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
        package = voice_package()
        preflight_space(cache_dir=cache_dir, package_size=package.get("size"))
        archive = _obtain_package(package, progress, cancel_event, cache_dir=cache_dir)
        # Resolve the small extra dependency before spending minutes extracting
        # the voice pack. A network failure keeps the verified voice archive.
        _notify(progress, "正在准备音频处理工具…", component="ffmpeg", state="下载及校验中")
        obtain_lgpl_archive(progress, cancel_event)
        _notify(progress, "音频处理工具下载完成。", component="ffmpeg", state="已下载，待安装")
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
        cached_archive = (cache_dir / "setup_downloads" / package.get("filename", "")).resolve()
        if not keep_cache and Path(archive).resolve() == cached_archive:
            try:
                cached_archive.unlink(missing_ok=True)
            except OSError:
                logging.getLogger(__name__).warning("安装成功，但下载缓存清理失败：%s", cached_archive, exc_info=True)
        if not keep_cache:
            tool_cache = ROOT / "temp/setup_downloads" / FFMPEG_PACKAGE["filename"]
            try:
                tool_cache.unlink(missing_ok=True)
            except OSError:
                logging.getLogger(__name__).warning("安装成功，但工具缓存清理失败：%s", tool_cache, exc_info=True)
        return report
    finally:
        try:
            if stage is not None and stage.exists():
                _safe_remove(stage)
        finally:
            _INSTALL_LOCK.release()
