import os
import sys
from pathlib import Path


_BUNDLE_ROOT = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
_TCL_LIBRARY = _BUNDLE_ROOT / "tcl" / "tcl8.6"
_TK_LIBRARY = _BUNDLE_ROOT / "tk" / "tk8.6"

if _TCL_LIBRARY.is_dir():
    os.environ["TCL_LIBRARY"] = str(_TCL_LIBRARY)
if _TK_LIBRARY.is_dir():
    os.environ["TK_LIBRARY"] = str(_TK_LIBRARY)
if hasattr(os, "add_dll_directory"):
    try:
        os.add_dll_directory(str(_BUNDLE_ROOT))
    except (FileNotFoundError, OSError):
        pass
