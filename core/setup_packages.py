"""Pinned Windows packs linked by the upstream 20250606v2pro release."""
from copy import deepcopy
from pathlib import Path
import subprocess


RELEASE_URL = "https://github.com/RVC-Boss/GPT-SoVITS/releases/tag/20250606v2pro"
VOICE_ENGINE_LABEL = "GPT-SoVITS v2Pro"
PACKAGES = {
    "standard": {
        "filename": "GPT-SoVITS-v2pro-20250604.7z",
        "size": 8185086602,
        "sha256": "bd60d0796553ff05d8568136e199c13e0dc22ebe2ed24273134e34ed6f215cd6",
    },
    "nvidia50": {
        "filename": "GPT-SoVITS-v2pro-20250604-nvidia50.7z",
        "size": 8835144925,
        "sha256": "97b4edcd451c42357db7e26e6c1c877ca5d85144fe97beaff6d7005d35bee008",
    },
}


def voice_package():
    variant = "standard"
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode == 0 and "RTX 50" in result.stdout:
            variant = "nvidia50"
    except (OSError, subprocess.TimeoutExpired):
        pass
    package = deepcopy(PACKAGES[variant])
    filename = package["filename"]
    package["urls"] = [
        "https://www.modelscope.cn/models/FlowerCry/gpt-sovits-7z-pacakges/resolve/master/" + filename,
        "https://hf-mirror.com/lj1995/GPT-SoVITS-windows-package/resolve/main/" + filename,
        "https://huggingface.co/lj1995/GPT-SoVITS-windows-package/resolve/main/" + filename,
    ]
    return package


def bundled_setup_file(name):
    import sys
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    return root / "assets" / "setup" / name
