"""Build a fresh-user distribution without copying personal configuration or characters."""
import argparse
import json
from pathlib import Path
import shutil
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def assert_without_live2d(executable):
    from PyInstaller.archive.readers import CArchiveReader
    reader = CArchiveReader(str(executable))
    names = list(reader.toc)
    for name in reader.toc:
        if name.endswith(".pyz"):
            names.extend(reader.open_embedded_archive(name).toc)
    def is_sdk(name):
        normalized = name.replace("\\", "/").lower()
        return (name.split(".")[0] in ("live2d", "glfw", "OpenGL")
                or normalized.startswith(("runtime/live2d/", "live2d/", "opengl/", "glfw/"))
                or "_live2d.pyd" in normalized
                or (normalized.endswith(".dll") and "glfw" in normalized))
    if any(is_sdk(name) for name in names):
        raise ValueError("基础版本体仍内嵌 Live2D/OpenGL 组件，请用 -WithoutLive2D 重新构建 EXE。")


def build_release(destination, project_root=ROOT, archive=False, without_live2d=False, executable=None):
    project_root = Path(project_root).resolve()
    destination = Path(destination).resolve()
    release_root = (project_root / "release").resolve()
    if release_root not in destination.parents:
        raise ValueError("发布输出必须位于项目 release 目录内。")
    if destination.exists():
        raise FileExistsError("发布目录已存在，请选择新的输出目录：" + str(destination))
    executable = Path(executable).resolve() if executable else project_root / "我的桌宠.exe"
    if not executable.is_file():
        raise FileNotFoundError("缺少正式桌宠程序。")
    if without_live2d:
        assert_without_live2d(executable)
    destination.mkdir(parents=True)
    shutil.copy2(executable, destination / "我的桌宠.exe")
    shutil.copytree(project_root / "assets", destination / "assets",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    if (project_root / "licenses").is_dir():
        shutil.copytree(project_root / "licenses", destination / "assets/licenses", dirs_exist_ok=True)
    if (project_root / "THIRD_PARTY_NOTICES.md").is_file():
        shutil.copy2(project_root / "THIRD_PARTY_NOTICES.md", destination / "assets/THIRD_PARTY_NOTICES.md")
    live2d = project_root / "runtime/live2d"
    if not without_live2d and live2d.is_dir():
        shutil.copytree(live2d, destination / "runtime/live2d",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for directory in ("characters", "data", "temp", "models/gpt_sovits", "setup_components"):
        (destination / directory).mkdir(parents=True, exist_ok=True)
    config = json.loads((project_root / "config/app_config.example.json").read_text(encoding="utf-8-sig"))
    for key in ("conversation_model", "vision_model"):
        config[key] = {"provider": "", "base_url": "", "model": "", "api_key": "", "api_key_env": ""}
    config["autostart"] = {"enabled": False}
    config["release_template"] = True
    write_json(destination / "config/app_config.json", config)
    (destination / "发布清单.txt").write_text("我的桌宠安装目录。卸载会删除本目录中的程序、角色及本地数据。\n", encoding="utf-8")
    write_json(destination / "config/gpt_sovits.json", {
        "engine": "local_api_v2", "host": "127.0.0.1", "port": 9880,
        "python": "engines/gpt_sovits/runtime/python.exe",
        "api_script": "engines/gpt_sovits/start_api.py",
        "tts_config": "engines/gpt_sovits/tts_infer_project.yaml",
        "device": "auto", "enabled": False, "characters": {},
    })
    write_json(destination / "data/settings.json", {
        "theme": "blue", "display_mode": "small", "character_id": "",
        "always_on_top": True, "voice_output": False, "voice_volume": 100,
        "setup_wizard_completed": False, "setup_wizard_dismissed": False, "setup_profile": "",
    })
    (destination / "使用说明.txt").write_text(
        "双击“我的桌宠.exe”，按向导完成首次配置。\n"
        "首次启动可在向导中选择安装位置，默认使用当前程序目录。\n"
        "仅桌宠模式无需下载 Python 或本地语音环境。\n"
        "本地语音模式自动下载官方 GPT-SoVITS v2Pro Windows 整合包，约8.2–8.9 GB。\n"
        "需要至少32 GiB可用空间，网络失败可重试、续传。下载后会校验并实际加载基础模型。\n"
        "也可将官方原始7z包放入 setup_components 目录，安装时会校验并直接使用。\n"
        "本发布包不含角色、聊天记录、个人语音或密钥。完成后请添加角色及配置模型接口。\n"
        "角色专属语音模型需另行导入，参考音频可随角色包导入。\n"
        "静态立绘自由添加；Live2D 仅支持官方签名角色包。双击包内启动器可定位本体并切换角色。\n"
        "基础版无 Live2D 组件，可继续使用立绘；Live2D 需使用含相应组件的本体。\n"
        "不会安装显卡驱动或修改系统 Python；没有可用CUDA时使用CPU。\n",
        encoding="utf-8",
    )
    if archive:
        output = Path(str(destination) + ".zip")
        if output.exists():
            raise FileExistsError(str(output))
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=4) as handle:
            for path in destination.rglob("*"):
                handle.write(path, str(path.relative_to(destination.parent)))
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "release/我的桌宠-安装版")
    parser.add_argument("--zip", action="store_true")
    parser.add_argument("--without-live2d", action="store_true")
    parser.add_argument("--executable", type=Path)
    args = parser.parse_args()
    print(build_release(args.output, archive=args.zip, without_live2d=args.without_live2d, executable=args.executable))


if __name__ == "__main__":
    main()
