"""Launch the official API with the desktop pet's local service identity."""
import ctypes
import os
from pathlib import Path
import runpy
import signal
import sys
import threading
import uuid

ROOT = Path(__file__).resolve().parent
os.chdir(str(ROOT))
sys.path[:0] = [str(ROOT), str(ROOT / "GPT_SoVITS")]
os.environ["PATH"] = os.pathsep.join((str(ROOT / "runtime"), str(ROOT / "ffmpeg"),
                                    str(ROOT), os.environ.get("PATH", "")))
if sys.platform == "win32":
    ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)


def configure_device():
    import torch
    import yaml
    if not torch.cuda.is_available():
        return None
    for index, argument in enumerate(sys.argv[:-1]):
        if argument not in ("-c", "--tts_config"):
            continue
        source = Path(sys.argv[index + 1])
        config = yaml.safe_load(source.read_text(encoding="utf-8"))
        for section in config.values():
            if isinstance(section, dict) and "device" in section:
                section.update(device="cuda:0", is_half=True)
        target = ROOT / "temp" / "tts_infer_runtime.yaml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
        sys.argv[index + 1] = str(target)
        return target
    return None


runtime_config = configure_device()
try:
    api = runpy.run_path(str(ROOT / "api_v2.py"), run_name="desktop_pet_api")
    app = api["APP"]
    identity = {"project_root": str(ROOT.parent.parent.resolve()), "pid": os.getpid(),
                "instance_id": uuid.uuid4().hex}

    @app.get("/desktop_pet/service")
    async def service_identity():
        return identity

    @app.get("/desktop_pet/service/stop")
    async def stop_service(instance_id: str):
        if instance_id != identity["instance_id"]:
            return api["JSONResponse"](status_code=409, content={"message": "service changed"})
        threading.Timer(.1, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
        return {"message": "stopping"}

    host = None if api["host"] == "None" else api["host"]
    api["uvicorn"].run(app=app, host=host, port=api["port"], workers=1)
finally:
    if runtime_config is not None:
        runtime_config.unlink(missing_ok=True)
