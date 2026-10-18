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
    assert set(audio["12"]) == {"a", "b"}         # ジャンルごと
    assert audio["12"]["a"]["source"].startswith("相手の注意が前に集まった")
    assert audio["12"]["a"]["file"] == "12-a.wav" and (tmp_path / "12-a.wav").is_file()
    assert abs(audio["12"]["a"]["sec"] - len(audio["12"]["a"]["source"]) * 0.2) < 0.05
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
    assert set(out["meta"]["audio"]["12"]) == {"b"}


def test_audio_endpoint_and_traversal(tmp_path):
    out = tmp_path / "out"; (out / "abc").mkdir(parents=True)
    (out / "abc" / "12-a.wav").write_bytes(make_wav(0.1))
    (out / "secret.txt").write_text("x")
    srv = make_server(out, HERE / "sample", port=0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        r = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/audio/abc/12-a.wav")
        assert r.status == 200 and r.headers["Content-Type"] == "audio/wav"
        for bad in ("abc/../secret.txt", "abc/secret.txt", "../abc/12-a.wav", "abc/12-a.wav/x"):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/audio/{bad}")
                assert False, bad
            except urllib.error.HTTPError as e:
                assert e.code == 404
    finally:
        srv.shutdown()


class FlakyClient(FakeClient):
    """最初の n 回は 429(回数制限)で失敗し、そのあと成功する。さらに after 回目以降はずっと失敗にもできる。"""
    def __init__(self, fail_first=0, fail_from=None):
        super().__init__()
        self.calls = 0
        self.fail_first, self.fail_from = fail_first, fail_from
        real = self.interactions.create

        class _I:
            def create(inner, **kw):
                self.calls += 1
                if self.calls <= self.fail_first or (self.fail_from and self.calls >= self.fail_from):
                    raise RuntimeError("429 RESOURCE_EXHAUSTED: quota exceeded")
                return real(**kw)
        self.interactions = _I()


def test_rate_limit_is_retried_with_long_wait():
    waits = []
    wav = tts.synthesize(FlakyClient(fail_first=2), "こんにちは", sleep=waits.append)
    assert tts.wav_seconds(wav) > 0
    assert waits == [20.0, 40.0]  # 回数制限は、短い待ちではなく長めに待つ


def test_other_errors_use_short_wait():
    ok = FakeClient()
    state = {"n": 0}

    class _I:
        def create(self, **kw):
            state["n"] += 1
            if state["n"] == 1:
                raise RuntimeError("500 internal")
            return ok.interactions.create(**kw)

    class C:
        interactions = _I()

    waits = []
    tts.synthesize(C(), "x", sleep=waits.append)
    assert waits == [2.0]


def test_calls_are_spaced_by_min_interval(tmp_path):
    t = [0.0]
    waits = []
    def sleep(s): waits.append(round(s, 2)); t[0] += s
    tts.generate(FakeClient(), saved_sample(), tmp_path, log=lambda _m: None,
                 min_interval=10.0, sleep=sleep, clock=lambda: t[0])
    assert len(waits) == 3 and all(abs(w - 10.0) < 0.01 for w in waits)  # 4回の呼び出しの間


def test_progress_is_saved_and_resumes_after_failure(tmp_path):
    saved = saved_sample()
    steps = []
    client = FlakyClient(fail_from=3)  # 2つ作ったところで、ずっと失敗する
    try:
        tts.generate(client, saved, tmp_path, log=lambda _m: None, on_progress=lambda s: steps.append(1),
                     sleep=lambda _s: None)
        assert False
    except RuntimeError:
        pass
    assert len(steps) == 2                       # 1つ作るたびに保存の合図
    assert sum(len(v) for v in saved["meta"]["audio"].values()) == 2   # 途中までの分が残っている
    ok = FakeClient()
    out = tts.generate(ok, saved, tmp_path, log=lambda _m: None)
    assert len(ok.texts) == 2                    # 続きの2つだけ作る
    assert sum(len(v) for v in out["meta"]["audio"].values()) == 4
