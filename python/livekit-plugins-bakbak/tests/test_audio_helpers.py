import array
import struct

from livekit.plugins.bakbak.tts import _f32le_to_s16le_pcm


def test_f32le_to_s16le_silence() -> None:
    f = array.array("f", [0.0, 0.0])
    out = _f32le_to_s16le_pcm(f.tobytes())
    assert out == struct.pack("<hh", 0, 0)


def test_f32le_to_s16le_full_scale() -> None:
    f = array.array("f", [1.0, -1.0])
    out = _f32le_to_s16le_pcm(f.tobytes())
    assert struct.unpack("<h", out[:2])[0] == 32767
    assert struct.unpack("<h", out[2:])[0] == -32767
