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
from urllib.error import URLError
import zipfile

from core.config import ROOT

PACKAGE = {
    "filename": "ffmpeg-N-128889-g88c46fce27-win64-lgpl.zip",
    "url": "https://github.com/FFmpeg-Builds/FFmpeg-Builds/releases/download/autobuild-2026-11-13-12-43/ffmpeg-N-128889-g88c46fce27-win64-lgpl.zip",
    "size": 175562391,
    "sha256": "a0a6da9ceded9f6e3b84eb4216485726582a93abc44b7a0befa9b7909ff1a241",
    "files": {
        "ffmpeg.exe": "ee4f488cd77b41b1e11a54ea0b8b006e71ba829bad77821992abe5d1ac4866bb",
        "ffprobe.exe": "e9f05a217207ea0b9887f1ec026e24001e3ccd222b05951b5f0d79bfecfd1cd8",
    },
    "source": "https://github.com/FFmpeg-Builds/FFmpeg-Builds/tree/autobuild-2026-11-13-12-43",
    "license": "LGPL-3.0-or-later",
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


def install_lgpl_tools(engine, progress=None, cancel_event=None):
    from core.installer import SetupError, _check_cancel, _notify, download_verified, verify_sha256
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

    archive = ROOT / "setup_components" / PACKAGE["filename"]
    if archive.is_file():
        verify_sha256(archive, PACKAGE["sha256"], cancel_event)
        if archive.stat().st_size != PACKAGE["size"]:
            raise SetupError("随包 LGPL 工具的文件大小不正确。")
    else:
        archive = ROOT / "temp/setup_downloads" / PACKAGE["filename"]
        for attempt in range(3):
            _check_cancel(cancel_event)
            _notify(progress, "正在下载并校验 LGPL 语音工具（{}/3）…".format(attempt + 1))
            try:
                download_verified(PACKAGE["url"], archive, PACKAGE["sha256"],
                                  cancel_event=cancel_event, expected_size=PACKAGE["size"])
                break
            except (OSError, SetupError, URLError):
                if attempt == 2:
                    raise

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
