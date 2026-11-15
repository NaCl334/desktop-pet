import json
import io
import http.client
import os
import re
import subprocess
import sys
import threading
import time
import unicodedata
import wave
from contextlib import contextmanager
from pathlib import Path
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from core.config import ROOT, load_json

try:
    import msvcrt
except ImportError:
    msvcrt = None


GPT_CONFIG_PATH = ROOT / "config" / "gpt_sovits.json"
ENGINE_DIR = ROOT / "engines" / "gpt_sovits"
MODEL_DIR = ROOT / "models" / "gpt_sovits"
LOG_DIR = ROOT / "data" / "logs"
STARTUP_LOCK_PATH = ROOT / "data" / "gpt_sovits.start.lock"
_STARTUP_THREAD_LOCK = threading.RLock()
SUPPORTED_LANGUAGES = {
    "auto",
    "auto_yue",
    "en",
    "zh",
    "ja",
    "yue",
    "ko",
    "all_zh",
    "all_ja",
    "all_yue",
    "all_ko",
}
SUPPORTED_REFERENCE_SUFFIXES = {".wav", ".mp3", ".flac", ".ogg"}
REFERENCE_MIN_SECONDS = 3.0
REFERENCE_MAX_SECONDS = 10.0


@contextmanager
def _gpt_service_start_lock():
    """在多个桌宠实例同时启动时，只允许一个实例创建 GPT-SoVITS。"""
    with _STARTUP_THREAD_LOCK:
        lock_file = None
        locked = False
        try:
            if msvcrt is not None and os.name == "nt":
                STARTUP_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
                lock_file = STARTUP_LOCK_PATH.open("a+b")
                lock_file.seek(0, os.SEEK_END)
                if lock_file.tell() == 0:
                    lock_file.write(b"0")
                    lock_file.flush()
                lock_file.seek(0)
                # msvcrt 在另一个进程正持有同一锁时有时直接返回
                # ``Resource deadlock avoided``，而不是按预期等待。将这
                # 个可重试的竞争转换为短轮询，避免并行启动把语音服务
                # 误判为启动失败。
                deadline = time.monotonic() + 125
                while True:
                    try:
                        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                        locked = True
                        break
                    except OSError as error:
                        if time.monotonic() >= deadline:
                            raise
                        if getattr(error, "errno", None) not in (36, 13, 33):
                            raise
                        time.sleep(0.25)
            yield
        finally:
            if lock_file is not None:
                if locked:
                    try:
                        lock_file.seek(0)
                        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                    except (OSError, ValueError):
                        pass
                lock_file.close()


def _clean_tts_text(text):
    """移除控制文本和不可朗读控制字符，保留模型可处理的正文。"""
    cleaned = re.sub(
        r"\[(?:emotion|intent):[^\]]+\]",
        "",
        str(text or ""),
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"```.*?```", "", cleaned, flags=re.DOTALL)
    cleaned = re.sub(r"!\[[^\]]*\]\([^)]+\)|\[[^\]]*\]\([^)]+\)", "", cleaned)
    cleaned = re.sub(r"^\s{0,3}#{1,6}\s*", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"[*_`~]", "", cleaned)
    cleaned = "".join(
        char
        for char in cleaned
        if not unicodedata.category(char).startswith(("C", "S"))
    )
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def _has_tts_content(text, language=None):
    cleaned = _clean_tts_text(text)
    language = str(language or "").strip().lower()
    if language in ("zh", "all_zh"):
        return any("\u3400" <= char <= "\u9fff" for char in cleaned)
    if language in ("ja", "all_ja"):
        return any(
            "\u3040" <= char <= "\u30ff" or "\u3400" <= char <= "\u9fff"
            for char in cleaned
        )
    return any(char.isalnum() for char in cleaned)


def load_gpt_config():
    try:
        return json.loads(GPT_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def resolve_project_path(value):
    path = Path(str(value or ""))
    return path if path.is_absolute() else ROOT / path


def character_weights(character_id):
    config = load_gpt_config()
    item = config.get("characters", {}).get(character_id)
    if isinstance(item, dict):
        gpt_path = resolve_project_path(item.get("gpt_weights"))
        sovits_path = resolve_project_path(item.get("sovits_weights"))
    else:
        voice_dir = ROOT / "characters" / character_id / "voice"
        voice_config = load_json(voice_dir / "voice.json", {})
        model = voice_config.get("model", {}) if isinstance(voice_config, dict) else {}
        if not isinstance(model, dict) or not (
            model.get("ckpt_file") or model.get("pth_file")
        ):
            raise RuntimeError(
                "没有找到角色的 GPT-SoVITS 权重配置：{}".format(character_id)
            )

        def resolve_voice_path(value):
            path = Path(str(value or ""))
            return path if path.is_absolute() else voice_dir / path

        item = {
            "gpt_weights": model.get("ckpt_file"),
            "sovits_weights": model.get("pth_file"),
            "version": voice_config.get("version", ""),
        }
        gpt_path = resolve_voice_path(item["gpt_weights"])
        sovits_path = resolve_voice_path(item["sovits_weights"])
    if not gpt_path.is_file() or not sovits_path.is_file():
        raise RuntimeError("角色 GPT-SoVITS 权重文件不完整：{}".format(character_id))
    return item, gpt_path, sovits_path


def runtime_ready():
    config = load_gpt_config()
    script = resolve_project_path(config.get("api_script"))
    tts_config = resolve_project_path(config.get("tts_config"))
    return (
        script.is_file()
        and tts_config.is_file()
        and (ENGINE_DIR / "GPT_SoVITS").is_dir()
        and (ENGINE_DIR / "tools").is_dir()
        and MODEL_DIR.is_dir()
    )


def _http_error_detail(error):
    """提取 HTTP 错误正文，便于定位 GPT-SoVITS 的 400 拒绝原因。"""
    raw = b""
    try:
        raw = error.read()
    except OSError:
        pass
    detail = ""
    if raw:
        text = raw.decode("utf-8", errors="replace").strip()
        try:
            response = json.loads(text)
        except ValueError:
            response = None
        if isinstance(response, dict):
            detail = str(
                response.get("Exception")
                or response.get("message")
                or response.get("detail")
                or ""
            ).strip()
        if not detail:
            detail = text
    detail = " ".join(detail.split())
    if len(detail) > 600:
        detail = detail[:600] + "……"
    status = getattr(error, "code", "未知状态")
    reason = str(getattr(error, "reason", "") or "").strip()
    return "HTTP {}：{}".format(status, detail or reason or "服务端拒绝请求")


class GPTSoVITSClient(object):
    def __init__(self, config=None):
        self.config = config or load_gpt_config()
        self.process = None
        self._process_lock = threading.RLock()
        self._managed_lock = threading.RLock()
        self._closed = threading.Event()
        self._service_identity = None
        self.current_character_id = ""

    @property
    def host(self):
        return str(self.config.get("host", "127.0.0.1"))

    @property
    def port(self):
        return int(self.config.get("port", 9880))

    @property
    def base_url(self):
        return "http://{}:{}".format(self.host, self.port)

    def _python_path(self):
        candidates = [
            resolve_project_path(self.config.get("python")),
            ENGINE_DIR / "runtime" / "python.exe",
            ENGINE_DIR / "venv" / "Scripts" / "python.exe",
            ROOT / "runtime" / ("python.exe" if os.name == "nt" else "python"),
            Path.home() / ".conda" / "envs" / "gpt_sovits" / (
                "python.exe" if os.name == "nt" else "python"
            ),
            Path(sys.executable),
        ]
        for candidate in candidates:
            if not candidate.is_file():
                continue
            if getattr(sys, "frozen", False) and candidate.resolve() == Path(sys.executable).resolve():
                continue
            try:
                probe = subprocess.run(
                    [str(candidate), "-c", "import sys; print(sys.executable)"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=8,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            if probe.returncode == 0:
                return candidate
        raise RuntimeError(
            "GPT-SoVITS Python 运行环境不可用；已跳过失效的旧解释器路径，"
            "请检查 engines/gpt_sovits/venv 或安装向导。"
        )

    def start(self):
        with self._managed_lock:
            return self._start()

    def _start(self):
        if self._closed.is_set():
            raise RuntimeError("桌宠正在退出，不能再启动语音服务。")
        with _gpt_service_start_lock():
            with self._process_lock:
                if self._closed.is_set():
                    raise RuntimeError("桌宠正在退出，不能再启动语音服务。")
                if self.process is not None and self.process.poll() is None:
                    return
                if self.process is not None:
                    self.process = None

                # 另一个桌宠实例可能已经把服务启动到“正在加载模型”的阶段。
                # 进程级锁覆盖“探测到创建”的整个窗口，避免两个实例同时绑定 9880。
                for _ in range(12):
                    try:
                        self._request("/docs", timeout=1.5)
                        self._remember_service_identity()
                        return
                    except RuntimeError:
                        if self._closed.wait(0.5):
                            raise RuntimeError("桌宠正在退出，已取消语音服务启动。")

                if not runtime_ready():
                    raise RuntimeError("GPT-SoVITS 项目内运行副本尚未准备完整。")
                LOG_DIR.mkdir(parents=True, exist_ok=True)
                script = resolve_project_path(self.config.get("api_script"))
                tts_config = resolve_project_path(self.config.get("tts_config"))
                stdout = (LOG_DIR / "gpt_sovits.stdout.log").open("a", encoding="utf-8")
                stderr = (LOG_DIR / "gpt_sovits.stderr.log").open("a", encoding="utf-8")
                environment = os.environ.copy()
                for key in ("PYTHONHOME", "VIRTUAL_ENV", "VIRTUAL_ENV_PROMPT"):
                    environment.pop(key, None)
                environment["PYTHONUTF8"] = "1"
                environment["PYTHONIOENCODING"] = "utf-8:replace"
                environment["PYTHONDONTWRITEBYTECODE"] = "1"
                environment["PATH"] = os.pathsep.join((str(ENGINE_DIR / "ffmpeg"),
                    str(ENGINE_DIR / "runtime"), str(ENGINE_DIR), environment.get("PATH", "")))
                try:
                    process = subprocess.Popen(
                        [
                            str(self._python_path()),
                            str(script),
                            "-a",
                            self.host,
                            "-p",
                            str(self.port),
                            "-c",
                            str(tts_config),
                        ],
                        cwd=str(ENGINE_DIR),
                        stdout=stdout,
                        stderr=stderr,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        env=environment,
                    )
                finally:
                    stdout.close()
                    stderr.close()
                self.process = process
                try:
                    # 首次加载 GPT-SoVITS 模型在本机实测约 66 秒；
                    # 60 秒会把“服务仍在正常加载”误判为失败。给冷启动
                    # 留出 120 秒，但仍由桌宠退出事件及时取消等待。
                    self._wait_for_service(timeout=120)
                    self._remember_service_identity()
                except Exception:
                    self.stop()
                    raise

    def _remember_service_identity(self):
        if self.host not in ("127.0.0.1", "localhost", "::1"):
            return
        try:
            identity = json.loads(self._request("/desktop_pet/service", timeout=2))
            if (
                isinstance(identity, dict)
                and Path(identity.get("project_root", "")).resolve() == ROOT.resolve()
                and identity.get("instance_id")
            ):
                self._service_identity = identity
        except (RuntimeError, OSError, ValueError, TypeError):
            pass

    def shutdown(self):
        """退出后禁止后台任务重新启动服务，并关闭本项目复用的服务。"""
        self._closed.set()
        self.stop()

    def stop(self):
        with self._process_lock:
            process = self.process
            self.process = None
            identity = self._service_identity
            self._service_identity = None
            self.current_character_id = ""
        if process is None or process.poll() is not None:
            if identity is not None:
                self._stop_reused_service(identity)
            return
        if os.name == "nt":
            try:
                result = subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if result.returncode == 0:
                    process.wait(timeout=3)
                    return
            except (OSError, subprocess.TimeoutExpired):
                pass
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)

    def _stop_reused_service(self, identity):
        try:
            current = json.loads(self._request("/desktop_pet/service", timeout=2))
        except RuntimeError:
            return
        if current != identity:
            return
        try:
            self._request(
                "/desktop_pet/service/stop",
                {"instance_id": identity["instance_id"]},
                timeout=2,
            )
        except RuntimeError:
            # 服务退出可能先关闭连接，继续检查原实例是否已离线。
            pass
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                current = json.loads(self._request("/desktop_pet/service", timeout=0.5))
            except RuntimeError:
                return
            if current != identity:
                return
            time.sleep(0.1)
        raise RuntimeError("语音服务未能在退出时限内关闭。")

    def _request(self, path, params=None, timeout=30):
        query = urllib_parse.urlencode(params or {})
        url = self.base_url + path + (("?" + query) if query else "")
        try:
            with urllib_request.urlopen(url, timeout=timeout) as response:
                return response.read()
        except urllib_error.HTTPError as error:
            raise RuntimeError(_http_error_detail(error))
        except urllib_error.URLError as error:
            raise RuntimeError("GPT-SoVITS 本地服务不可用：{}".format(error.reason))
        except (TimeoutError, ConnectionError, http.client.HTTPException) as error:
            raise RuntimeError("GPT-SoVITS 本地服务连接中断：{}".format(error)) from error

    def wait_ready(self, timeout=60):
        self._wait_for_service(timeout=timeout)

    def _wait_for_service(self, timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._closed.is_set():
                raise RuntimeError("桌宠正在退出，已取消等待语音服务。")
            process = self.process
            if process is not None and process.poll() is not None:
                # 端口冲突时，另一个实例可能已经成功接管服务。
                try:
                    self._request("/docs", timeout=1.5)
                    self.process = None
                    return True
                except RuntimeError:
                    raise RuntimeError(
                        "GPT-SoVITS 本地服务启动失败，请查看 data/logs。"
                    )
            try:
                self._request("/docs", timeout=2)
            except RuntimeError:
                time.sleep(0.5)
                continue
            return True
        raise RuntimeError("等待 GPT-SoVITS 本地服务超时。")

    def select_character(self, character_id, weights_override=None):
        if weights_override is None and character_id == self.current_character_id:
            return
        _item, gpt_path, sovits_path = weights_override or character_weights(character_id)
        self.current_character_id = None
        self._request("/set_gpt_weights", {"weights_path": str(gpt_path)})
        self._request("/set_sovits_weights", {"weights_path": str(sovits_path)})
        self.current_character_id = character_id if weights_override is None else None

    @staticmethod
    def _generation_payload(generation_params):
        if not isinstance(generation_params, dict):
            return {}
        payload = {}
        float_limits = {
            "temperature": (0.01, 2.0, "温度"),
            "top_p": (0.01, 1.0, "Top-P"),
            "speed_factor": (0.1, 3.0, "语速倍率"),
            "repetition_penalty": (0.1, 3.0, "重复惩罚"),
        }
        int_limits = {
            "top_k": (1, 100, "Top-K"),
            "sample_steps": (1, 200, "采样步数"),
            "min_chunk_length": (1, 1000, "流式最小片段长度"),
        }

        for key, (minimum, maximum, label) in float_limits.items():
            value = generation_params.get(key)
            if value is None or value == "":
                continue
            try:
                number = float(value)
            except (TypeError, ValueError):
                raise ValueError("{}必须是数字。".format(label))
            if not (number == number and abs(number) != float("inf")):
                raise ValueError("{}不能是 NaN 或无穷大。".format(label))
            if not minimum <= number <= maximum:
                raise ValueError(
                    "{}应在 {}～{} 之间，当前为 {}。".format(
                        label,
                        minimum,
                        maximum,
                        value,
                    )
                )
            payload[key] = number

        for key, (minimum, maximum, label) in int_limits.items():
            value = generation_params.get(key)
            if value is None or value == "":
                continue
            if isinstance(value, bool):
                raise ValueError("{}必须是整数。".format(label))
            try:
                numeric_value = float(value)
                number = int(numeric_value)
            except (TypeError, ValueError, OverflowError):
                raise ValueError("{}必须是整数。".format(label))
            if numeric_value != number:
                raise ValueError("{}必须是整数。".format(label))
            if not minimum <= number <= maximum:
                raise ValueError(
                    "{}应在 {}～{} 之间，当前为 {}。".format(
                        label,
                        minimum,
                        maximum,
                        value,
                    )
                )
            payload[key] = number

        value = generation_params.get("seed")
        if value not in (None, ""):
            if isinstance(value, bool):
                raise ValueError("随机种子必须是整数。")
            try:
                numeric_seed = float(value)
                seed = int(numeric_seed)
            except (TypeError, ValueError, OverflowError):
                raise ValueError("随机种子必须是整数。")
            if numeric_seed != seed:
                raise ValueError("随机种子必须是整数。")
            payload["seed"] = seed

        value = generation_params.get("parallel_infer")
        if value not in (None, ""):
            if isinstance(value, str):
                normalized = value.strip().lower()
                if normalized in ("true", "1", "yes", "on"):
                    payload["parallel_infer"] = True
                elif normalized in ("false", "0", "no", "off"):
                    payload["parallel_infer"] = False
                else:
                    raise ValueError("并行推理必须是 true 或 false。")
            elif isinstance(value, bool):
                payload["parallel_infer"] = value
            else:
                raise ValueError("并行推理必须是 true 或 false。")

        value = generation_params.get("streaming_mode")
        if value not in (None, ""):
            if isinstance(value, bool):
                value = int(value)
            try:
                numeric_mode = float(value)
                streaming_mode = int(numeric_mode)
            except (TypeError, ValueError, OverflowError):
                raise ValueError("流式模式必须是 0～3 的整数。")
            if numeric_mode != streaming_mode:
                raise ValueError("流式模式必须是 0～3 的整数。")
            if streaming_mode not in (0, 1, 2, 3):
                raise ValueError("流式模式必须是 0～3 的整数。")
            payload["streaming_mode"] = streaming_mode

        if "speed_factor" not in payload and generation_params.get("speed") not in (None, ""):
            speed = generation_params["speed"]
            try:
                speed = float(speed)
            except (TypeError, ValueError):
                raise ValueError("语速必须是数字。")
            if not (0.1 <= speed <= 3.0):
                raise ValueError("语速应在 0.1～3.0 之间，当前为 {}。".format(generation_params["speed"]))
            payload["speed_factor"] = speed
        return payload

    @staticmethod
    def _validate_reference_audio(reference_audio):
        if reference_audio in (None, ""):
            raise FileNotFoundError("没有配置参考音频。")
        path = Path(str(reference_audio))
        if not path.is_file():
            raise FileNotFoundError("参考音频不存在：{}".format(path))
        if path.suffix.lower() not in SUPPORTED_REFERENCE_SUFFIXES:
            raise ValueError(
                "参考音频格式不受支持：{}。支持 wav、mp3、flac、ogg。".format(path.suffix or "无扩展名")
            )
        try:
            with path.open("rb") as audio_file:
                if not audio_file.read(1):
                    raise ValueError("参考音频文件为空：{}".format(path))
        except OSError as error:
            raise OSError("参考音频无法读取：{}（{}）".format(path, error))

        # 当前项目使用 WAV 参考音频；对 WAV 做完整检查，提前复现服务端的 3～10 秒约束。
        if path.suffix.lower() != ".wav":
            return path.resolve()
        try:
            with wave.open(str(path), "rb") as reader:
                channels = reader.getnchannels()
                sample_width = reader.getsampwidth()
                sample_rate = reader.getframerate()
                frames = reader.getnframes()
                duration = frames / float(sample_rate) if sample_rate else 0.0
        except (EOFError, OSError, wave.Error) as error:
            raise ValueError("参考音频不是有效 WAV 文件：{}（{}）".format(path, error))
        if channels not in (1, 2):
            raise ValueError("参考音频声道数为 {}，GPT-SoVITS 只支持单声道或双声道。".format(channels))
        if sample_width not in (1, 2, 3, 4) or sample_rate <= 0 or frames <= 0:
            raise ValueError("参考音频的采样率、位深或帧数无效：{}。".format(path))
        if duration < REFERENCE_MIN_SECONDS or duration > REFERENCE_MAX_SECONDS:
            raise ValueError(
                "参考音频时长为 {:.2f} 秒，需要在 3～10 秒之间：{}。".format(
                    duration,
                    path,
                )
            )
        return path.resolve()

    @staticmethod
    def _validate_language(language, field_name):
        value = str(language or "").strip().lower()
        if not value:
            raise ValueError("{}不能为空。".format(field_name))
        if value not in SUPPORTED_LANGUAGES:
            supported = "、".join(sorted(SUPPORTED_LANGUAGES))
            raise ValueError(
                "{}“{}”不受当前 GPT-SoVITS 支持；可用值：{}。".format(
                    field_name,
                    language,
                    supported,
                )
            )
        return value

    @classmethod
    def _validate_tts_request(
        cls,
        text,
        reference_audio,
        text_lang,
        prompt_lang,
        generation_params=None,
    ):
        cleaned_text = _clean_tts_text(text)
        reference_path = cls._validate_reference_audio(reference_audio)
        normalized_text_lang = cls._validate_language(text_lang, "目标文本语言")
        normalized_prompt_lang = cls._validate_language(prompt_lang, "参考文本语言")
        if not _has_tts_content(cleaned_text, normalized_text_lang):
            raise ValueError("TTS 文本没有当前语言可朗读内容。")
        payload = cls._generation_payload(generation_params)
        return (
            cleaned_text,
            reference_path,
            normalized_text_lang,
            normalized_prompt_lang,
            payload,
        )

    def warmup(
        self,
        character_id,
        reference_audio,
        prompt_text="",
        text_lang="all_zh",
        prompt_lang="all_zh",
        generation_params=None,
    ):
        """真实合成一个短句，建立参考音频与文本缓存。"""
        warmup_text = {
            "all_zh": "你好。",
            "zh": "你好。",
            "ja": "こんにちは。",
            "all_ja": "こんにちは。",
            "en": "Hello.",
        }.get(str(text_lang).lower(), "你好。")
        output_path = (
            ROOT
            / "temp"
            / "tts"
            / "tts_warmup_{}.wav".format(int(time.time() * 1000))
        )
        try:
            self.synthesize(
                warmup_text,
                character_id,
                reference_audio,
                prompt_text=prompt_text,
                text_lang=text_lang,
                prompt_lang=prompt_lang,
                output_path=output_path,
                generation_params=generation_params,
            )
        finally:
            output_path.unlink(missing_ok=True)

    def synthesize(
        self,
        text,
        character_id,
        reference_audio,
        prompt_text="",
        text_lang="zh",
        prompt_lang="zh",
        output_path=None,
        generation_params=None,
        weights_override=None,
    ):
        (
            text,
            reference_path,
            text_lang,
            prompt_lang,
            generation_payload,
        ) = self._validate_tts_request(
            text,
            reference_audio,
            text_lang,
            prompt_lang,
            generation_params,
        )
        if weights_override is None:
            character_weights(character_id)
        self.start()
        self.wait_ready()
        if weights_override is None:
            self.select_character(character_id)
        else:
            self.select_character(character_id, weights_override=weights_override)
        payload = {
            "text": text,
            "text_lang": text_lang,
            "ref_audio_path": str(reference_path),
            "prompt_lang": prompt_lang,
            "prompt_text": prompt_text or "",
            # 短句要求整句一次送入模型，避免默认 cut5 拆分后出现断句、
            # 漏句或多个片段拼接产生的电音。
            "text_split_method": "cut0",
            "media_type": "wav",
            "streaming_mode": False,
        }
        payload.update(generation_payload)
        payload["streaming_mode"] = False
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib_request.Request(
            self.base_url + "/tts",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib_request.urlopen(request, timeout=180) as response:
                audio = response.read()
        except urllib_error.HTTPError as error:
            raise RuntimeError(
                "GPT-SoVITS 生成失败：{}".format(_http_error_detail(error))
            )
        except urllib_error.URLError as error:
            raise RuntimeError("GPT-SoVITS 生成失败：{}".format(error.reason))
        if not audio.startswith(b"RIFF"):
            raise RuntimeError("GPT-SoVITS 返回的不是 WAV 音频。")
        try:
            with wave.open(io.BytesIO(audio), "rb") as reader:
                valid = (
                    reader.getnchannels() > 0
                    and reader.getsampwidth() > 0
                    and reader.getframerate() > 0
                    and reader.getnframes() > 0
                )
        except (EOFError, wave.Error):
            valid = False
        if not valid:
            raise RuntimeError("GPT-SoVITS 返回了空音频。")
        if output_path is None:
            output_path = ROOT / "temp" / "tts_output.wav"
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(audio)
        return output_path

    @staticmethod
    def _parse_stream_format(header):
        if len(header) < 44 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
            raise RuntimeError("GPT-SoVITS 流式响应不是有效 WAV 数据。")
        format_pos = header.find(b"fmt ")
        if format_pos < 0 or format_pos + 24 > len(header):
            raise RuntimeError("GPT-SoVITS 流式响应缺少 WAV 格式信息。")
        channels = int.from_bytes(header[format_pos + 10:format_pos + 12], "little")
        sample_rate = int.from_bytes(header[format_pos + 12:format_pos + 16], "little")
        bits_per_sample = int.from_bytes(
            header[format_pos + 22:format_pos + 24],
            "little",
        )
        sample_width = (bits_per_sample + 7) // 8
        if min(channels, sample_rate, sample_width) <= 0:
            raise RuntimeError("GPT-SoVITS 流式音频格式无效。")
        return sample_rate, channels, sample_width

    def stream_pcm(
        self,
        text,
        character_id,
        reference_audio,
        on_audio,
        prompt_text="",
        text_lang="zh",
        prompt_lang="zh",
        streaming_mode=3,
        min_chunk_length=8,
        speed_factor=1.0,
        timeout=180,
        generation_params=None,
    ):
        """按流读取 GPT-SoVITS PCM，并在收到数据时立即交给播放队列。"""
        if not callable(on_audio):
            raise ValueError("流式 TTS 缺少音频回调。")
        request_params = dict(generation_params or {})
        configured_streaming_mode = request_params.get(
            "streaming_mode",
            streaming_mode,
        )
        # 角色配置长期沿用普通合成的 false/0。流式入口不能把这个值
        # 原样送给服务端，否则服务端会等整段音频生成完才返回首块 PCM。
        if isinstance(configured_streaming_mode, bool):
            configured_streaming_mode = (
                streaming_mode
                if not configured_streaming_mode
                else 1
            )
        else:
            try:
                numeric_mode = int(float(configured_streaming_mode))
            except (TypeError, ValueError, OverflowError):
                numeric_mode = 0
            if numeric_mode == 0:
                configured_streaming_mode = streaming_mode
            else:
                configured_streaming_mode = numeric_mode
        if configured_streaming_mode not in (1, 2, 3):
            configured_streaming_mode = streaming_mode
        request_params["streaming_mode"] = configured_streaming_mode
        request_params.setdefault("min_chunk_length", min_chunk_length)
        request_params.setdefault("speed_factor", speed_factor)
        (
            text,
            reference_path,
            text_lang,
            prompt_lang,
            generation_payload,
        ) = self._validate_tts_request(
            text,
            reference_audio,
            text_lang,
            prompt_lang,
            request_params,
        )
        character_weights(character_id)
        self.start()
        self.wait_ready()
        self.select_character(character_id)
        payload = {
            "text": text,
            "text_lang": text_lang,
            "ref_audio_path": str(reference_path),
            "prompt_lang": prompt_lang,
            "prompt_text": prompt_text or "",
            "media_type": "wav",
            "text_split_method": "cut0",
            "streaming_mode": generation_payload.get(
                "streaming_mode",
                configured_streaming_mode,
            ),
            "min_chunk_length": generation_payload.get("min_chunk_length", 16),
            "speed_factor": generation_payload.get("speed_factor", 1.0),
        }
        payload.update(generation_payload)
        if payload.get("streaming_mode") not in (1, 2, 3):
            payload["streaming_mode"] = configured_streaming_mode
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib_request.Request(
            self.base_url + "/tts",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            response = urllib_request.urlopen(request, timeout=timeout)
        except urllib_error.HTTPError as error:
            raise RuntimeError(
                "GPT-SoVITS 流式生成失败：{}".format(_http_error_detail(error))
            )
        except urllib_error.URLError as error:
            raise RuntimeError("GPT-SoVITS 流式生成失败：{}".format(error.reason))

        header_buffer = b""
        format_info = None
        total_bytes = 0
        pending_pcm = bytearray()
        output_chunk_size = 4096
        first_chunk_emitted = False

        def emit_pcm(payload):
            nonlocal first_chunk_emitted
            if not payload:
                return
            pending_pcm.extend(payload)
            if not first_chunk_emitted and pending_pcm:
                on_audio(bytes(pending_pcm), format_info)
                pending_pcm.clear()
                first_chunk_emitted = True
                return
            while len(pending_pcm) >= output_chunk_size:
                on_audio(bytes(pending_pcm[:output_chunk_size]), format_info)
                del pending_pcm[:output_chunk_size]

        try:
            with response:
                while True:
                    chunk = response.read(1024)
                    if not chunk:
                        break
                    if format_info is None:
                        header_buffer += chunk
                        data_pos = header_buffer.find(b"data")
                        if data_pos < 0:
                            if len(header_buffer) > 4096:
                                raise RuntimeError("GPT-SoVITS 流式响应缺少 WAV 数据段。")
                            continue
                        if len(header_buffer) < data_pos + 8:
                            continue
                        header = header_buffer[:data_pos + 8]
                        format_info = self._parse_stream_format(header)
                        payload = header_buffer[data_pos + 8:]
                        header_buffer = b""
                    else:
                        payload = chunk
                    if not payload:
                        continue
                    emit_pcm(payload)
                    total_bytes += len(payload)
        except urllib_error.URLError as error:
            raise RuntimeError("GPT-SoVITS 流式读取失败：{}".format(error.reason))
        if pending_pcm:
            on_audio(bytes(pending_pcm), format_info)
            pending_pcm.clear()
        if format_info is None or total_bytes <= 0:
            raise RuntimeError("GPT-SoVITS 没有返回有效流式音频。")
        return format_info, total_bytes
