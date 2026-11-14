"""Build once, copy into a prepared payload, then sign the complete payload."""
import json
from pathlib import Path
import subprocess
import sys

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.body_location import locate_body, remember_body


def launch(package, choose_body):
    package = Path(package).resolve()
    character = json.loads((package / "character.json").read_text(encoding="utf-8-sig"))
    character_id = character.get("id", "")
    if (not isinstance(character_id, str) or not 0 < len(character_id) <= 120
            or not all(c.isalnum() or c in "_-" for c in character_id)):
        raise ValueError("角色 ID 无效。")
    if not (package / "signature.ed25519").is_file():
        raise ValueError("缺少角色包签名，请重新解压完整的官方角色包。")
    executable = locate_body(package)
    if executable is None:
        selected = choose_body()
        if not selected:
            return False
        executable = remember_body(package, selected)
    subprocess.Popen([str(executable), "--character", character_id,
                      "--character-package", str(package)], cwd=executable.parent)
    return True


def main():
    import tkinter as tk
    from tkinter import filedialog, messagebox
    root = tk.Tk()
    root.withdraw()
    package = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(sys.argv[1])
    def choose():
        messagebox.showinfo("定位桌宠本体", "请先安装桌宠本体，再选择安装目录中的“我的桌宠.exe”。下载方式见项目发布说明。", parent=root)
        return filedialog.askopenfilename(title="选择桌宠本体", filetypes=[("桌宠程序", "*.exe")], parent=root)
    try:
        launch(package, choose)
    except Exception as error:
        messagebox.showerror("角色启动失败", str(error), parent=root)
    finally:
        root.destroy()


if __name__ == "__main__":
    main()
