import base64
import io
import json
import threading
import urllib.error
import urllib.request
import wave
from pathlib import Path

import tts
from serve import make_server

HERE = Path(__file__).parent


def make_wav(seconds: float, rate: int = 8000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


class FakeClient:
    """文字数に比例した長さの音声を返す偽のクライアント(1文字0.2秒)。"""
    def __init__(self):
        self.texts = []
        outer = self

        class _I:
            def create(self, **kw):
                text = kw["input"][0]["content"][0]["text"]
                outer.texts.append(text)
                wav = make_wav(len(text) * 0.2)

                class R:
                    class output_audio:  # noqa: N801
                        data = base64.b64encode(wav).decode()
                return R
        self.interactions = _I()


def saved_sample():
    data = json.loads((HERE / "sample" / "good.json").read_text(encoding="utf-8"))
    return {"meta": {}, "data": data}


def test_wav_seconds():
    assert abs(tts.wav_seconds(make_wav(1.5)) - 1.5) < 0.01


def test_generate_makes_clip_per_genre_and_caches(tmp_path):
    saved, client = saved_sample(), FakeClient()
    out = tts.generate(client, saved, tmp_path, log=lambda _m: None)
    audio = out["meta"]["audio"]
    assert set(audio) == {"12", "23"}                 # sugoi の場面だけ
    assert set(audio["12"]) == {"rpg", "fps"}         # ジャンルごと
    assert audio["12"]["rpg"]["source"].startswith("敵が前に集まってる")
    assert audio["12"]["rpg"]["file"] == "12-rpg.wav" and (tmp_path / "12-rpg.wav").is_file()
    assert abs(audio["12"]["rpg"]["sec"] - len(audio["12"]["rpg"]["source"]) * 0.2) < 0.05
    n = len(client.texts)
    assert n == 4
    tts.generate(client, out, tmp_path, log=lambda _m: None)
    assert len(client.texts) == n                     # 2回目は作り直さない
    tts.generate(client, out, tmp_path, force=True, log=lambda _m: None)
    assert len(client.texts) == n + 4


def test_generate_skips_empty_live(tmp_path):
    saved = saved_sample()
    saved["data"]["events"][0]["analogies"][0]["live"] = ""
    out = tts.generate(FakeClient(), saved, tmp_path, log=lambda _m: None)
    assert set(out["meta"]["audio"]["12"]) == {"fps"}


def test_audio_endpoint_and_traversal(tmp_path):
    out = tmp_path / "out"; (out / "abc").mkdir(parents=True)
    (out / "abc" / "12-rpg.wav").write_bytes(make_wav(0.1))
    (out / "secret.txt").write_text("x")
    srv = make_server(out, HERE / "sample", port=0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        r = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/audio/abc/12-rpg.wav")
        assert r.status == 200 and r.headers["Content-Type"] == "audio/wav"
        for bad in ("abc/../secret.txt", "abc/secret.txt", "../abc/12-rpg.wav", "abc/12-rpg.wav/x"):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/audio/{bad}")
                assert False, bad
            except urllib.error.HTTPError as e:
                assert e.code == 404
    finally:
        srv.shutdown()
