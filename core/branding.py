from pathlib import Path

from core.config import ROOT


APP_NAME = "我的桌宠"
BRANDING_DIR = ROOT / "assets" / "branding"
LOGO_PATH = BRANDING_DIR / "logo.png"
ICON_PATH = BRANDING_DIR / "desktop_pet.ico"
TEXTURE_PATH = BRANDING_DIR / "blue_texture.png"


def existing_asset(path):
    path = Path(path)
    return path if path.is_file() else None
