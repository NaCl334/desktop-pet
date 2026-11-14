"""Optional local readings for Japanese TTS verification."""
import os
import re
import shutil
import threading
from pathlib import Path

from core.config import ROOT


_LOCK = threading.RLock()
_READER = None
_INITIALIZED = False


def japanese_reading(text):
    """Keep Latin names intact; compare Japanese words by their pronunciation."""
    global _READER, _INITIALIZED
    with _LOCK:
        if not _INITIALIZED:
            _INITIALIZED = True
            try:
                import pyopenjtalk

                dictionary = Path(os.fsdecode(pyopenjtalk.OPEN_JTALK_DICT_DIR))
                if not dictionary.is_dir():
                    return None
                dictionary_path = str(dictionary)
                if os.name == "nt" and not dictionary_path.isascii():
                    cache = ROOT / "temp" / "japanese_reading" / dictionary.name
                    if not (cache / "sys.dic").is_file():
                        shutil.copytree(dictionary, cache, dirs_exist_ok=True)
                    dictionary_path = os.path.relpath(cache)
                    if not dictionary_path.isascii():
                        return None
                _READER = pyopenjtalk.OpenJTalk(
                    dn_mecab=os.fsencode(dictionary_path)
                )
            except (ImportError, OSError, RuntimeError, ValueError, AttributeError, TypeError):
                return None
        if _READER is None:
            return None
        try:
            chunks = re.split(r"([A-Za-z][A-Za-z0-9]*)", str(text or ""))
            readings = []
            for chunk in chunks:
                if not chunk:
                    continue
                if re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", chunk):
                    readings.append(chunk)
                else:
                    for token in _READER.run_frontend(chunk):
                        if token.get("pos") != "記号":
                            readings.append(token.get("pron") or token["string"])
            return "".join(readings)
        except (OSError, RuntimeError, ValueError, KeyError, AttributeError, TypeError):
            return None
