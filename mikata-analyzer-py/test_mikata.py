"""実行: pytest(VS Code では左のテスト(フラスコ)アイコンから)
実際のGeminiは呼ばない。品質チェッカーと、再試行などの流れを偽のクライアントで確認する。
"""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from analyze import AnalysisFailed, analyze_video, build_prompt, extract_video_id, validate_api_key
from check import check_result, parse_time
from schema import JSON_SCHEMA

HERE = Path(__file__).parent
GOOD = json.loads((HERE / "sample" / "good.json").read_text(encoding="utf-8"))
BAD = json.loads((HERE / "sample" / "bad.json").read_text(encoding="utf-8"))
URL = "https://youtu.be/abc123XYZ_-"


def test_parse_time():
    assert parse_time("01:05") == 65
    assert parse_time("0:10") == 10
    assert parse_time("1:75") is None
    assert parse_time("abc") is None


def test_extract_video_id():
    assert extract_video_id("https://www.youtube.com/watch?v=abc123XYZ_-") == "abc123XYZ_-"
    assert extract_video_id("https://youtu.be/abc123XYZ_-?t=5") == "abc123XYZ_-"
    assert extract_video_id("https://www.youtube.com/shorts/abc123XYZ_-") == "abc123XYZ_-"
    with pytest.raises(ValueError):
        extract_video_id("https://example.com/")


def test_good_sample_has_no_errors():
    assert check_result(GOOD, duration=30, video_id="vid").errors == []


def test_bad_sample_is_detected():
    r = check_result(BAD, duration=120, video_id="vid")
    errors = "\n".join(r.errors)
    for pat in ["戻っています", "一致しません", "動画の長さ", "confidence が0〜1の範囲外", "explain が長すぎます",
                "sugoi なのに expertWhy が空", "sugoi なのに sugoiType が none", "「実際との違い」(differs)がありません",
                "gist が空です", "gist が長すぎます"]:
        assert pat in errors, pat
    warnings = "\n".join(r.warnings)
    for pat in ["watchPoints は3つ", "basics は最大3つ", "同じです", "見落とし",
                "サッカー用語「オフサイド」", "sugoi が false なのに sugoiType", "sugoi が false なのに expertWhy"]:
        assert pat in warnings, pat
    reasons = "\n".join(x["reason"] for x in r.review)
    assert "自信が低い" in reasons
    assert "other" in reasons
    assert "固有名の可能性: 「田中選手」" in reasons
    assert "専門用語「エントリー」" in reasons
    assert "専門用語「クラッチ」" in reasons
    assert all(x["link"].startswith("https://youtu.be/vid?t=") for x in r.review)


def test_good_sample_lists_sugoi_scenes_for_expert_review():
    r = check_result(GOOD, duration=30, video_id="vid")
    assert r.stats["sugoi"] == 2
    assert sum("玄人の確認" in x["reason"] for x in r.review) == 2
    assert r.warnings == []


def test_too_many_sugoi_is_warned():
    d = copy.deepcopy(GOOD)
    for e in d["events"]:
        e.update(sugoi=True, sugoiType="technique", expertWhy="難しい技術。", analogies=[])
    assert any("すごい場面が多すぎます" in w for w in check_result(d, duration=30).warnings)


def test_no_sugoi_is_warned():
    d = copy.deepcopy(GOOD)
    for e in d["events"]:
        e.update(sugoi=False, sugoiType="none", expertWhy="", analogies=[])
    assert any("1件もありません" in w for w in check_result(d, duration=30).warnings)


def test_missing_genre_is_warned():
    d = copy.deepcopy(GOOD)
    d["events"][0]["analogies"] = d["events"][0]["analogies"][:1]
    assert any("たとえが無いジャンル: fps" in w for w in check_result(d, duration=30).warnings)


def test_gist_length_is_warned_before_it_is_an_error():
    d = copy.deepcopy(GOOD)
    d["events"][0]["gist"] = "あ" * 30
    r = check_result(d, duration=30)
    assert not r.errors and any("1行に収まりません" in w for w in r.warnings)


def test_unknown_genre_is_format_error():
    d = copy.deepcopy(GOOD)
    d["events"][0]["analogies"][0]["genre"] = "moba"
    assert any("形式エラー" in e for e in check_result(d).errors)


def test_broken_shape_gives_format_error():
    r = check_result({"preview": {}, "events": "x"})
    assert r.errors and "形式エラー" in r.errors[0]


def test_strict_types_reject_string_numbers():
    d = copy.deepcopy(GOOD)
    d["events"][0]["seconds"] = "12"
    assert any("形式エラー" in e for e in check_result(d).errors)


def test_generic_player_words_are_not_flagged():
    d = copy.deepcopy(GOOD)
    d["events"][0]["scene"] = "赤い選手が相手選手をかわす"
    r = check_result(d, duration=30)
    assert not any("固有名" in x["reason"] for x in r.review)


class FakeClient:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []
        self.interactions = SimpleNamespace(create=self._create)

    def _create(self, **req):
        self.calls.append(req)
        o = self.outputs.pop(0)
        if isinstance(o, Exception):
            raise o
        return SimpleNamespace(output_text=o)


class ApiError(Exception):
    def __init__(self, msg, code):
        super().__init__(msg)
        self.code = code


def test_request_shape():
    c = FakeClient([json.dumps(GOOD)])
    r = analyze_video(c, URL, model="m", duration=30)
    assert r["attempts"] == 1
    req = c.calls[0]
    assert req["model"] == "m"
    assert req["input"][0] == {"type": "video", "uri": URL}
    assert req["input"][1]["type"] == "text"
    assert req["response_format"] == {"type": "text", "mime_type": "application/json", "schema": JSON_SCHEMA}


def test_processing_is_passed_through():
    c = FakeClient([json.dumps(GOOD)])
    p = {"type": "static", "start_offset": "10s", "end_offset": "40s", "fps": 2}
    analyze_video(c, URL, processing=p, duration=30)
    assert c.calls[0]["input"][0]["processing"] == p


def test_quality_error_triggers_retry_with_feedback():
    c = FakeClient([json.dumps(BAD), json.dumps(GOOD)])
    r = analyze_video(c, URL, duration=30, backoff_s=0)
    assert r["attempts"] == 2
    text = c.calls[1]["input"][1]["text"]
    assert "前回の出力の問題" in text and "戻っています" in text


def test_non_json_and_code_fence():
    c = FakeClient(["こんにちは", "```json\n" + json.dumps(GOOD) + "\n```"])
    r = analyze_video(c, URL, duration=30, backoff_s=0)
    assert r["attempts"] == 2
    assert "JSONとして読めませんでした" in c.calls[1]["input"][1]["text"]


def test_429_is_retried_but_400_fails_fast():
    c1 = FakeClient([ApiError("rate", 429), json.dumps(GOOD)])
    assert analyze_video(c1, URL, duration=30, backoff_s=0)["attempts"] == 2

    c2 = FakeClient([ApiError("bad request", 400), json.dumps(GOOD)])
    with pytest.raises(ApiError):
        analyze_video(c2, URL, backoff_s=0)
    assert len(c2.calls) == 1


def test_all_attempts_fail_raises_with_last_result():
    c = FakeClient([json.dumps(BAD), json.dumps(BAD)])
    with pytest.raises(AnalysisFailed) as ei:
        analyze_video(c, URL, duration=120, max_retries=2, backoff_s=0)
    assert ei.value.last_result["report"].errors


def test_prompt_contains_key_instructions():
    p = build_prompt()
    for k in ["MM:SS", "60字以内", "確実に読み取れた", "confidence", "terms", "gist", "sugoi", "expertWhy", "differs", "rpg", "fps", "context"]:
        assert k in p, k


def test_real_sdk_serializes_request_as_expected(monkeypatch):
    """本物のSDKを通して、送信される中身を確認する(通信だけ差し替え。APIは呼ばない)。"""
    httpx = pytest.importorskip("httpx")
    genai = pytest.importorskip("google.genai")
    captured = {}

    def fake_send(self, request, **kw):
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content or b"{}")
        resp = {
            "id": "i1", "status": "completed", "model": "m",
            "steps": [{"type": "model_output",
                       "content": [{"type": "text", "text": json.dumps(GOOD, ensure_ascii=False)}]}],
        }
        return httpx.Response(200, json=resp, request=request)

    monkeypatch.setattr(httpx.Client, "send", fake_send)
    client = genai.Client(api_key="dummy-key")
    p = {"type": "static", "start_offset": "0s", "end_offset": "30s", "fps": 2}
    r = analyze_video(client, URL, model="m", processing=p, duration=30)
    assert r["attempts"] == 1 and not r["report"].errors

    body = captured["body"]
    assert captured["url"].endswith("/interactions")
    parts = body["input"][0]["content"]
    assert [x["type"] for x in parts] == ["video", "text"]  # 動画 → テキストの順
    assert parts[0]["uri"] == URL
    assert parts[0]["processing"]["start_offset"] == "0s"  # 区間は文字列
    assert body["response_format"]["mime_type"] == "application/json"
    assert body["response_format"]["schema"]["required"] == ["preview", "events"]


def test_validate_api_key():
    assert validate_api_key("AIzaSyA-1234567890abcdefghijklmnopqrstu") is None
    assert "英数字以外" in validate_api_key("ここにキー")
    assert "空白" in validate_api_key("AIza abc")
