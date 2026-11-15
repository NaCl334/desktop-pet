"""Install verified upstream LGPL tools into the optional voice environment.

No FFmpeg binary is bundled with the desktop pet. The upstream archive remains
in the setup cache for repair/offline reuse; licenses accompany installed tools.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid
from urllib.error import URLError
import zipfile

from core.config import ROOT

PACKAGE = {
    # Upstream retains the final build of each month for two years. Pin that
    # asset rather than one of the rolling 14 daily builds or mutable latest.
    "filename": "ffmpeg-N-128227-g4811625b5f-win64-lgpl.zip",
    "url": "https://github.com/FFmpeg-Builds/FFmpeg-Builds/releases/download/autobuild-2026-10-31-12-59/ffmpeg-N-128227-g4811625b5f-win64-lgpl.zip",
    "api_url": "https://api.github.com/repos/FFmpeg-Builds/FFmpeg-Builds/releases/assets/695397317",
    "size": 175264885,
    "sha256": "6550f7abc950128fbb0069b4552f2972baccf5fbd819f2fd0abbe4ceb8d48734",
    "files": {
        "ffmpeg.exe": "f661607511f91654f369fd7ae270f95f0382bd4dc722c5f3b502de92f33f633f",
        "ffprobe.exe": "915962ff46aab2cc4aefcb9c9856945ff448c90064252eb9ab17b817c5a7e360",
    },
    "source": "https://github.com/FFmpeg-Builds/FFmpeg-Builds/tree/autobuild-2026-10-31-12-59",
    "license": "LGPL-3.0-or-later",
    "release_date": "2026-10-31",
    "review_before": "2028-09-30",
}


def _digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for data in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(data)
    return h.hexdigest()


def _probe(executable):
    from core.installer import SetupError
    result = subprocess.run(
        [str(executable), "-version"], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=20,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode or "--enable-gpl" in result.stdout or "--enable-nonfree" in result.stdout:
        raise SetupError("语音工具校验失败：必须使用已核对的 LGPL FFmpeg 构建。")
    return result.stdout


def obtain_lgpl_archive(progress=None, cancel_event=None):
    from core.installer import SetupError, _check_cancel, _notify, download_verified, verify_sha256
    _check_cancel(cancel_event)
    archive = ROOT / "setup_components" / PACKAGE["filename"]
    if archive.is_file():
        _notify(progress, "正在校验随包 FFmpeg 音频工具…", 71)
        verify_sha256(archive, PACKAGE["sha256"], cancel_event)
        if archive.stat().st_size != PACKAGE["size"]:
            raise SetupError("随包 LGPL 工具的文件大小不正确。")
        return archive
    archive = ROOT / "temp/setup_downloads" / PACKAGE["filename"]
    if archive.is_file():
        # Reuse the prefetched archive without moving progress backwards after
        # voice extraction. An invalid cache still goes through normal repair.
        _notify(progress, "正在校验已下载的 FFmpeg 音频工具…")
        try:
            verify_sha256(archive, PACKAGE["sha256"], cancel_event)
            if archive.stat().st_size == PACKAGE["size"]:
                return archive
        except SetupError:
            _check_cancel(cancel_event)
    last_error = None
    # The official API redirects to the same immutable release asset without
    # requiring access to github.com. Refresh signed redirects on each retry.
    endpoints = ("api_url", "url", "api_url")
    for attempt, key in enumerate(endpoints, 1):
        _check_cancel(cancel_event)
        url = PACKAGE[key]
        headers = None
        label = "官方直链"
        if key == "api_url":
            url += "?download=1&request={}".format(uuid.uuid4().hex)
            headers = {"Accept": "application/octet-stream"}
            label = "官方 API 通道"
        _notify(progress, "正在准备 FFmpeg 音频工具：{}（{}/3）…".format(label, attempt), 71)

        def downloaded(received, total):
            _notify(progress, "正在下载 FFmpeg 音频工具：{:.1f} / {:.1f} MB".format(
                received / 1e6, total / 1e6), 71 + 3 * received / max(total, 1))

        try:
            return download_verified(url, archive, PACKAGE["sha256"], progress=downloaded,
                cancel_event=cancel_event, expected_size=PACKAGE["size"], request_headers=headers)
        except (OSError, SetupError, URLError) as error:
            _check_cancel(cancel_event)
            last_error = error
    raise SetupError("FFmpeg 音频工具下载失败（官方通道已尝试 3 次）。"
        "已保留 GPT-SoVITS 主包和工具下载进度，无需重新下载主包。"
        "请检查网络后重试；也可将原版 {} 放入安装目录的 setup_components 文件夹。原因：{}".format(
            PACKAGE["filename"], last_error)) from last_error


def install_lgpl_tools(engine, progress=None, cancel_event=None):
    from core.installer import SetupError, _check_cancel
    engine = Path(engine)
    _check_cancel(cancel_event)
    candidates = [engine / name for name in PACKAGE["files"]]
    candidates += [engine / directory / name for directory in ("runtime", "ffmpeg") for name in PACKAGE["files"]]
    active = [p for p in candidates if p.is_file()]
    canonical = engine / "ffmpeg"
    if all((canonical / name).is_file() for name in PACKAGE["files"]) and all(
            _digest(p) == PACKAGE["files"][p.name] for p in active):
        _probe(canonical / "ffmpeg.exe")
        return canonical
    archive = obtain_lgpl_archive(progress, cancel_event)

    # Verify every file and executable before replacing any existing tool.
    with tempfile.TemporaryDirectory(prefix="ffmpeg_lgpl_", dir=archive.parent) as directory:
        stage = Path(directory)
        with zipfile.ZipFile(archive) as handle:
            for name, expected in PACKAGE["files"].items():
                members = [n for n in handle.namelist() if n.endswith("/bin/" + name)]
                if len(members) != 1:
                    raise SetupError("LGPL 工具压缩包结构不正确。")
                with handle.open(members[0]) as source, (stage / name).open("wb") as output:
                    shutil.copyfileobj(source, output)
                if _digest(stage / name) != expected:
                    raise SetupError("LGPL 工具文件校验失败。")
            license_member = [n for n in handle.namelist() if n.endswith("/LICENSE.txt")]
            if len(license_member) != 1:
                raise SetupError("LGPL 工具压缩包缺少许可证。")
            (stage / "LICENSE.txt").write_bytes(handle.read(license_member[0]))
        for name in PACKAGE["files"]:
            _probe(stage / name)
        _check_cancel(cancel_event)
        targets = {p for p in active}
        targets.update(canonical / name for name in PACKAGE["files"])
        for target in targets:
            if target.is_symlink() or any(p.is_symlink() for p in target.parents):
                raise SetupError("拒绝替换符号链接中的语音工具。")
        previous = {}
        try:
            for index, target in enumerate(sorted(targets)):
                target.parent.mkdir(parents=True, exist_ok=True)
                backup = stage / ("previous_" + str(index))
                if target.exists():
                    shutil.copy2(target, backup)
                    previous[target] = backup
                else:
                    previous[target] = None
                temporary = target.with_name(target.name + ".lgpl-new")
                shutil.copy2(stage / target.name, temporary)
                os.replace(temporary, target)
            license_dir = canonical / "licenses"
            license_dir.mkdir(exist_ok=True)
            shutil.copy2(stage / "LICENSE.txt", license_dir / "FFmpeg-LGPL-3.0.txt")
            (license_dir / "SOURCE.json").write_text(json.dumps(PACKAGE, indent=2), encoding="utf-8")
        except BaseException:
            for target, backup in previous.items():
                if backup is None:
                    target.unlink(missing_ok=True)
                else:
                    shutil.copy2(backup, target)
            raise
        finally:
            for target in targets:
                target.with_name(target.name + ".lgpl-new").unlink(missing_ok=True)
    return canonical
