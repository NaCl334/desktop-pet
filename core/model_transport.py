"""Per-request connections with separate connect/read limits and cancellation."""
import http.client
import json
import queue
import socket
import threading
import time
from datetime import datetime, timezone
from urllib import request as urllib_request
from urllib import error as urllib_error


CONNECT_SECONDS = 15.0
READ_SECONDS = 60.0
TOTAL_SECONDS = 150.0
_LOG_LOCK = threading.Lock()


class ModelDeadline(TimeoutError):
    pass


class ModelIOTimeout(TimeoutError):
    def __init__(self, phase):
        super().__init__(phase)
        self.phase = phase


def record_request(request_id, phase, attempt, elapsed, status=None, kind=""):
    # Deliberately omit endpoint, model, exception text, prompts and credentials.
    record = {"time": datetime.now(timezone.utc).isoformat(), "request_id": request_id,
              "phase": phase, "attempt": attempt, "elapsed": round(elapsed, 3),
              "status": status, "kind": kind}
    try:
        from core.config import ROOT
        path = ROOT / "data" / "logs" / "model_requests.jsonl"
        with _LOG_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > 2 * 1024 * 1024:
                path.replace(path.with_suffix(".previous.jsonl"))
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=True) + "\n")
    except (OSError, ImportError):
        pass


def open_model_request(request, connect_timeout, read_timeout, sockets, check):
    def connection_class(base):
        class Connection(base):
            def connect(self):
                check()
                super().connect()
                sockets.append(self.sock)
                check()
                check.phase = "read"
                self.sock.settimeout(read_timeout)

            def send(self, data):
                check()
                return super().send(data)
        return Connection

    class HTTPHandler(urllib_request.HTTPHandler):
        def http_open(self, req):
            return self.do_open(connection_class(http.client.HTTPConnection), req)

    class HTTPSHandler(urllib_request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(connection_class(http.client.HTTPSConnection), req,
                                context=self._context)

    # No global opener or connection patch: concurrent model calls stay isolated.
    opener = urllib_request.build_opener(HTTPHandler(), HTTPSHandler())
    return opener.open(request, timeout=connect_timeout)


def run_attempt(request, reader, check_current, deadline, read_seconds):
    results = queue.Queue(maxsize=1)
    stopped = threading.Event()
    sockets = []

    def check():
        check_current()
        if stopped.is_set() or time.monotonic() >= deadline:
            raise ModelDeadline("整轮请求超时")

    def worker():
        try:
            check()
            remaining = max(.01, deadline - time.monotonic())
            with open_model_request(request, min(CONNECT_SECONDS, remaining),
                                    min(read_seconds, remaining), sockets, check) as response:
                check()
                result = reader(response, check)
            results.put((True, result))
        except Exception as exc:
            reason = exc.reason if isinstance(exc, urllib_error.URLError) else exc
            if isinstance(reason, TimeoutError) and not isinstance(reason, ModelDeadline):
                exc = ModelIOTimeout(getattr(check, "phase", "connect"))
            results.put((False, exc))
        finally:
            if stopped.is_set():
                close_sockets()

    def close_sockets():
        for connection in list(sockets):
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass

    threading.Thread(target=worker, daemon=True, name="model-transport").start()
    try:
        while True:
            check()
            try:
                success, result = results.get(timeout=min(.05, max(.001, deadline - time.monotonic())))
            except queue.Empty:
                continue
            check()
            if success:
                return result
            raise result
    finally:
        stopped.set()
        close_sockets()
