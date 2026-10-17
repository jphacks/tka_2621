"""ビューア用サーバーのテスト。実行: pytest"""
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from serve import SAMPLE_ID, list_videos, load_video, make_server

HERE = Path(__file__).parent
SAMPLE = HERE / "sample"
GOOD = json.loads((SAMPLE / "good.json").read_text(encoding="utf-8"))


def test_empty_out_falls_back_to_sample(tmp_path):
    v = list_videos(tmp_path / "none", SAMPLE)
    assert [x["id"] for x in v] == [SAMPLE_ID] and v[0]["sample"]
    s = load_video(SAMPLE_ID, tmp_path, SAMPLE)
    assert s["meta"]["videoId"] is None and s["data"]["events"]


def test_saved_results_listed_and_meta_defaults(tmp_path):
    (tmp_path / "abc123XYZ_-.json").write_text(json.dumps(GOOD), encoding="utf-8")  # metaなしでも読める
    (tmp_path / "broken.json").write_text("{", encoding="utf-8")
    (tmp_path / "x.failed.txt").write_text("", encoding="utf-8")
    v = list_videos(tmp_path, SAMPLE)
    assert [x["id"] for x in v] == ["abc123XYZ_-"]
    assert load_video("abc123XYZ_-", tmp_path, SAMPLE)["meta"]["videoId"] == "abc123XYZ_-"


@pytest.mark.parametrize("bad", ["../secret", "a/b", "a b", "", "x" * 65])
def test_bad_ids_rejected(tmp_path, bad):
    assert load_video(bad, tmp_path, SAMPLE) is None


def test_http_routes(tmp_path):
    srv = make_server(tmp_path, SAMPLE, "127.0.0.1", 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{port}"
        html = urllib.request.urlopen(base + "/").read().decode("utf-8")
        assert "<title>" in html
        assert json.load(urllib.request.urlopen(base + "/api/videos"))[0]["id"] == SAMPLE_ID
        assert json.load(urllib.request.urlopen(base + "/api/videos/sample"))["data"]["events"]
        for path in ["/api/videos/nope", "/api/videos/..%2Fserve.py", "/serve.py"]:
            with pytest.raises(urllib.error.HTTPError) as ei:
                urllib.request.urlopen(base + path)
            assert ei.value.code == 404
    finally:
        srv.shutdown()
        srv.server_close()
