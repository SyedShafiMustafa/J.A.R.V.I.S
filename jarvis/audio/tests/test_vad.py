import threading
import time

import numpy as np
import pytest

from audio import vad


class FakeStream:
    def __init__(self, batches):
        self.batches = batches
        self.callback = None
        self.started = False
        self.stopped = False
        self.closed = False

    def start(self):
        self.started = True
        for batch in self.batches:
            self.callback(batch, len(batch), None, None)

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


def install_stream(monkeypatch, batches):
    stream = FakeStream(batches)

    def make_stream(**kwargs):
        stream.callback = kwargs["callback"]
        return stream

    monkeypatch.setattr(vad.sd, "InputStream", make_stream)
    monkeypatch.setattr(vad.sf, "write", lambda *args, **kwargs: None)
    monkeypatch.setattr(vad.tempfile, "NamedTemporaryFile", lambda **kwargs: type(
        "Temp", (), {"name": "capture.wav"}
    )())
    return stream


def audio_batch(volume):
    return np.full((32, 1), volume, dtype=np.float32)


def test_speech_and_silence_complete_recording(monkeypatch):
    stream = install_stream(
        monkeypatch,
        [audio_batch(0.02)] * 12 + [audio_batch(0.0)] * 16,
    )

    path = vad.VoiceRecorder(
        max_duration=1,
        speech_wait_timeout=1,
    ).record()

    assert path == "capture.wav"
    assert stream.started and stream.stopped and stream.closed


def test_sustained_noise_spike_does_not_start_recording(monkeypatch):
    stream = install_stream(
        monkeypatch,
        [audio_batch(0.02)] * 11 + [audio_batch(0.001)] * 4,
    )

    with pytest.raises(vad.NoSpeechError):
        vad.VoiceRecorder(
            max_duration=1,
            speech_wait_timeout=0.05,
        ).record()

    assert stream.stopped and stream.closed


@pytest.mark.parametrize(
    "kwargs,error_type",
    [
        ({"max_duration": 0.05, "speech_wait_timeout": 1}, vad.RecordingTimeoutError),
        ({"max_duration": 1, "speech_wait_timeout": 0.05}, vad.NoSpeechError),
    ],
)
def test_recording_timeouts_close_stream(monkeypatch, kwargs, error_type):
    stream = install_stream(monkeypatch, [])

    with pytest.raises(error_type):
        vad.VoiceRecorder(**kwargs).record()

    assert stream.stopped and stream.closed


def test_cancel_while_waiting_closes_stream(monkeypatch):
    stream = install_stream(monkeypatch, [])
    cancel = threading.Event()
    recorder = vad.VoiceRecorder(max_duration=1, speech_wait_timeout=1)

    result = {}
    def run():
        try:
            recorder.record(cancel)
        except Exception as error:
            result["error"] = error

    thread = threading.Thread(target=run)
    thread.start()
    time.sleep(0.05)
    cancel.set()
    thread.join(timeout=1)

    assert not thread.is_alive()
    assert isinstance(result["error"], vad.RecordingCancelledError)
    assert stream.stopped and stream.closed


def test_cancel_during_active_recording_closes_stream(monkeypatch):
    stream = install_stream(monkeypatch, [audio_batch(0.02)])
    cancel = threading.Event()
    recorder = vad.VoiceRecorder(max_duration=1, speech_wait_timeout=1)

    result = {}

    def run():
        try:
            recorder.record(cancel)
        except Exception as error:
            result["error"] = error

    thread = threading.Thread(target=run)
    thread.start()
    time.sleep(0.05)
    cancel.set()
    thread.join(timeout=1)

    assert not thread.is_alive()
    assert isinstance(result["error"], vad.RecordingCancelledError)
    assert stream.stopped and stream.closed


# --- pre-buffer tests: audio before the speech-start trigger must be kept ---


def capture_written_audio(monkeypatch):
    """Replace the sf.write stub with one that records the audio it is given."""
    captured = {}

    def capture_write(path, data, samplerate):
        captured["data"] = np.asarray(data).flatten()

    monkeypatch.setattr(vad.sf, "write", capture_write)
    return captured


def test_pre_buffer_retains_audio_immediately_before_speech_start(monkeypatch):
    # 10 quiet onset blocks arrive before the 12 loud blocks that trip the
    # speech-start threshold; without the pre-buffer they would be discarded.
    batches = [audio_batch(0.001)] * 10 + [audio_batch(0.02)] * 12 + [audio_batch(0.0)] * 16
    install_stream(monkeypatch, batches)
    captured = capture_written_audio(monkeypatch)

    vad.VoiceRecorder(max_duration=1, speech_wait_timeout=1).record()

    data = captured["data"]
    # 10 quiet + 12 loud + 16 trailing silence blocks, in order.
    assert len(data) == (10 + 12 + 16) * 32
    # The recording begins with the quiet onset that preceded the trigger.
    assert np.allclose(data[: 10 * 32], 0.001)
    assert np.allclose(data[10 * 32 : (10 + 12) * 32], 0.02)
    assert np.allclose(data[(10 + 12) * 32 :], 0.0)


def test_recording_begins_with_prebuffered_audio_within_one_second_cap(monkeypatch):
    # 600 quiet blocks (19200 samples) exceed the 1 s (16000 samples) cap:
    # only the last 500 quiet blocks may be retained before the 12 loud ones.
    batches = [audio_batch(0.001)] * 600 + [audio_batch(0.02)] * 12 + [audio_batch(0.0)] * 16
    install_stream(monkeypatch, batches)
    captured = capture_written_audio(monkeypatch)

    vad.VoiceRecorder(max_duration=1, speech_wait_timeout=1).record()

    data = captured["data"]
    # The 1 s cap trims from the front as the 11 pre-trigger loud blocks
    # enter the window, so 489 quiet + 11 loud are flushed, then the 12th
    # loud block and the trailing silence are appended normally.
    assert len(data) == (489 + 12 + 16) * 32
    assert np.allclose(data[: 489 * 32], 0.001)
    assert np.allclose(data[489 * 32 : (489 + 12) * 32], 0.02)
    assert np.allclose(data[(489 + 12) * 32 :], 0.0)
    # The pre-trigger content never exceeds the 1 s (16000 sample) window.
    assert 489 * 32 <= 16000


def test_pre_buffer_cap_defaults_to_one_second(monkeypatch):
    # Blocks sized like the production stream (1280 samples = 80 ms): 13 of
    # them (10400 samples) fit under the 1 s cap, so all are retained.
    blocks = [np.full((1280, 1), 0.001, dtype=np.float32)]
    batches = blocks * 13 + [audio_batch(0.02)] * 12 + [audio_batch(0.0)] * 16
    install_stream(monkeypatch, batches)
    captured = capture_written_audio(monkeypatch)

    vad.VoiceRecorder(max_duration=1, speech_wait_timeout=1).record()

    data = captured["data"]
    # 12 quiet blocks (15360 samples) survive the cap; the 11 pre-trigger
    # loud blocks (352) join them at flush, then the 12th loud and 16
    # silence blocks are appended: 15360 + 384 + 512 = 16256.
    assert len(data) == 16256
    assert np.allclose(data[: 12 * 1280], 0.001)
    assert np.allclose(data[12 * 1280 : 12 * 1280 + 12 * 32], 0.02)
    assert np.allclose(data[12 * 1280 + 12 * 32 :], 0.0)


def test_cancel_while_prebuffering_closes_stream(monkeypatch):
    stream = install_stream(monkeypatch, [audio_batch(0.001)] * 5)
    cancel = threading.Event()
    recorder = vad.VoiceRecorder(max_duration=1, speech_wait_timeout=1)

    result = {}

    def run():
        try:
            recorder.record(cancel)
        except Exception as error:
            result["error"] = error

    thread = threading.Thread(target=run)
    thread.start()
    time.sleep(0.05)
    cancel.set()
    thread.join(timeout=1)

    assert not thread.is_alive()
    assert isinstance(result["error"], vad.RecordingCancelledError)
    assert stream.stopped and stream.closed


def test_no_speech_timeout_with_buffered_content_still_cleans_up(monkeypatch):
    # Quiet blocks fill the pre-buffer but never trip the threshold; the
    # speech-wait timeout must still fire and close the stream cleanly.
    stream = install_stream(monkeypatch, [audio_batch(0.001)] * 10)

    with pytest.raises(vad.NoSpeechError):
        vad.VoiceRecorder(max_duration=1, speech_wait_timeout=0.05).record()

    assert stream.stopped and stream.closed
