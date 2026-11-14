"""Request-local preparation deadlines and bounded verified-audio recovery."""

import atexit
import shutil
import threading
import time
import uuid
import wave
from pathlib import Path


def detect_sustained_tone(path):
    """Flag unusually stationary voiced sound; never trim or rewrite the source WAV."""
    import numpy as np

    with wave.open(str(path), "rb") as reader:
        rate = reader.getframerate()
        if reader.getsampwidth() != 2 or reader.getcomptype() != "NONE" or rate < 8000:
            return None
        channels = reader.getnchannels()
        samples = np.frombuffer(reader.readframes(reader.getnframes()), dtype="<i2")
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    frame_size, hop = int(rate * .08), int(rate * .1)
    if len(samples) < int(rate * 2.4):
        return None
    frequencies = np.fft.rfftfreq(frame_size, 1.0 / rate)
    edges = np.geomspace(80, min(8000, rate / 2), 49)
    masks = [(frequencies >= a) & (frequencies < b) for a, b in zip(edges[:-1], edges[1:])]
    taper = np.hanning(frame_size)
    spectra, levels, flatness = [], [], []
    for start in range(0, len(samples) - frame_size + 1, hop):
        frame = samples[start:start + frame_size].astype(float)
        levels.append(float(np.sqrt(np.mean(frame * frame))))
        power = np.abs(np.fft.rfft(frame * taper)) ** 2
        audible = power[(frequencies >= 80) & (frequencies <= min(8000, rate / 2))]
        flatness.append(float(np.exp(np.mean(np.log(audible + 1e-9))) / max(audible.mean(), 1e-9)))
        spectrum = np.array([np.sqrt(power[mask].sum()) for mask in masks])
        spectra.append(spectrum / max(float(np.linalg.norm(spectrum)), 1e-9))
    spectra, levels, flatness = np.array(spectra), np.array(levels), np.array(flatness)
    # Require 2.4 seconds of steady audible energy and an almost unchanged spectral envelope.
    for index in range(len(spectra) - 24 + 1):
        energy = levels[index:index + 24]
        if energy.min() < 180 or energy.max() / max(energy.min(), 1) > 2.0:
            continue
        if float(np.median(flatness[index:index + 24])) > .08:
            continue
        group = spectra[index:index + 24]
        center = group.mean(axis=0)
        similarity = group.dot(center) / max(float(np.linalg.norm(center)), 1e-9)
        if float(similarity.min()) >= .74 and float(np.median(similarity)) >= .90:
            return index * .1, (index + 23) * .1 + .08
    return None


class VoicePreparationBudget:
    def __init__(self, first_wait, idle_wait, total_wait):
        self.started = self.last_progress = time.monotonic()
        self.first_wait = first_wait
        self.idle_wait = idle_wait
        self.total_wait = total_wait
        self.progress_count = 0
        self.lock = threading.Lock()

    def advance(self):
        with self.lock:
            self.last_progress = time.monotonic()
            self.progress_count += 1

    def remaining(self):
        with self.lock:
            now = time.monotonic()
            idle = self.idle_wait if self.progress_count else self.first_wait
            return min(
                self.started + self.total_wait - now,
                self.last_progress + idle - now,
            )


class VerifiedReplyCache:
    """Keep only the latest reply's verified segments, never partial archives."""

    def __init__(self, directory, max_bytes=128 * 1024 * 1024, ttl=1800):
        self.directory = Path(directory)
        self.max_bytes = max_bytes
        self.ttl = ttl
        self.identity = None
        self.parts = {}
        self.updated = 0
        self.lock = threading.Lock()
        atexit.register(self.close)

    def close(self):
        with self.lock:
            self._clear()
            try:
                self.directory.rmdir()
            except OSError:
                pass

    @staticmethod
    def valid(path):
        try:
            with wave.open(str(path), "rb") as reader:
                count = reader.getnframes()
                data = reader.readframes(count)
                return count > 0 and len(data) == (
                    count * reader.getnchannels() * reader.getsampwidth()
                )
        except (OSError, EOFError, ValueError, wave.Error):
            return False

    def _clear(self):
        for path, _verdict, _text in self.parts.values():
            self._remove(path)
        self.parts.clear()

    @staticmethod
    def _remove(path):
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass

    def begin(self, identity):
        with self.lock:
            if identity != self.identity or time.monotonic() - self.updated > self.ttl:
                self._clear()
                self.identity = identity
            self.updated = time.monotonic()

    def get(self, identity, part):
        with self.lock:
            if identity != self.identity or time.monotonic() - self.updated > self.ttl:
                return None
            result = self.parts.get(part)
            if result is None:
                return None
            path, verdict, text = result
            if not self.valid(path):
                self._remove(path)
                del self.parts[part]
                return None
            # Each worker owns its copy, so cancellation cannot delete another's audio.
            target = self.directory / ("working_" + uuid.uuid4().hex + ".wav")
            try:
                shutil.copyfile(path, target)
            except OSError:
                self._remove(target)
                return None
            return target, verdict, text

    def put(self, identity, part, result):
        path, verdict, text = result
        with self.lock:
            if identity != self.identity or not self.valid(path):
                return
            try:
                size = Path(path).stat().st_size
                used = sum(
                    Path(item[0]).stat().st_size
                    for item in self.parts.values() if Path(item[0]).is_file()
                )
            except OSError:
                return
            if used + size > self.max_bytes or len(self.parts) >= 64:
                return
            target = self.directory / ("verified_" + uuid.uuid4().hex + ".wav")
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
            except OSError:
                self._remove(target)
                return
            previous = self.parts.get(part)
            if previous:
                self._remove(previous[0])
            self.parts[part] = (target, verdict, text)
            self.updated = time.monotonic()
