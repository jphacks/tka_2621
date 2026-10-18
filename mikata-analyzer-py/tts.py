"""すごい場面の「実況」(たとえの一言二言)を、Gemini TTS で音声ファイル(WAV)にする。

使い方:
    python tts.py 動画ID            (例: python tts.py 8E-gLmRwiaw)
    python tts.py out/動画ID.json
    python tts.py 動画ID --voice Kore --force

- 解析結果(out/動画ID.json)の sugoi=true の場面ごと・ジャンルごとに、
  analogies[].live(実況の一言二言)を out/動画ID/秒数-ジャンル.wav にする。
- できた音声の長さは、out/動画ID.json の meta.audio に書き戻す(ビューアが読む)。
- 同じ文章の音声は作り直さない(--force で全部作り直す)。
- APIキーは analyze.py と同じ(.env の GEMINI_API_KEY)。
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sys
import time
import wave
from pathlib import Path

DEFAULT_TTS_MODEL = "gemini-3.8-flash-lite-tts"
DEFAULT_VOICE = "Zephyr"


def wav_seconds(wav: bytes) -> float:
    with wave.open(io.BytesIO(wav), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def _is_rate_limit(err: Exception) -> bool:
    text = f"{type(err).__name__} {err}".lower()
    return any(k in text for k in ("429", "resource_exhausted", "quota", "rate limit", "too many requests"))


def synthesize(client, text: str, model: str = DEFAULT_TTS_MODEL, voice: str = DEFAULT_VOICE,
               retries: int = 6, backoff_s: float = 2.0, rate_wait_s: float = 20.0, sleep=time.sleep) -> bytes:
    """文章を WAV のバイト列にする。回数制限(429)は長めに、それ以外は短く待ってやり直す。"""
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            interaction = client.interactions.create(
                model=model,
                input=[{"type": "user_input", "content": [{"type": "text", "text": text}]}],
                response_format={"type": "audio"},
                generation_config={"speech_config": [{"voice": voice}]},
            )
            wav = base64.b64decode(interaction.output_audio.data)
            wav_seconds(wav)  # 壊れた音声ならここで例外
            return wav
        except Exception as err:  # noqa: BLE001
            last = err
            if attempt < retries:
                sleep(min(rate_wait_s * attempt, 65.0) if _is_rate_limit(err) else backoff_s * attempt)
    raise RuntimeError(f"音声を作れませんでした: {last}")


def generate(client, saved: dict, audio_dir: Path, model: str = DEFAULT_TTS_MODEL,
             voice: str = DEFAULT_VOICE, force: bool = False, log=print,
             min_interval: float = 0.0, on_progress=None, sleep=time.sleep, clock=time.monotonic) -> dict:
    """saved(={meta,data})の音声を作り、meta.audio を更新して返す。
    meta.audio = { "秒数": { "a": {file, sec, source, voice, model}, "b": {...} } }"""
    audio_dir.mkdir(parents=True, exist_ok=True)
    old = (saved.get("meta") or {}).get("audio") or {}
    audio: dict = {k: v for k, v in old.items()}   # 途中で止まっても、前の分を失わない
    saved.setdefault("meta", {})["audio"] = audio
    done: set[str] = set()
    last_call = [None]

    def make(text: str) -> bytes:
        """回数制限を超えないよう、呼び出しの間隔をあけてから音声を作る。"""
        if min_interval and last_call[0] is not None:
            wait = min_interval - (clock() - last_call[0])
            if wait > 0:
                sleep(wait)
        last_call[0] = clock()
        return synthesize(client, text, model, voice, sleep=sleep)

    for e in sorted(saved["data"]["events"], key=lambda x: x["seconds"]):
        if not e.get("sugoi") or str(e["seconds"]) in done:
            continue
        key = str(e["seconds"])
        done.add(key)
        clips: dict = {}
        for a in e.get("analogies", []):
            text = (a.get("live") or "").strip()
            genre = a.get("genre")
            if not text or not genre:
                continue
            fname = f"{key}-{genre}.wav"
            prev = (old.get(key) or {}).get(genre)
            if (not force and prev and prev.get("source") == text and prev.get("voice") == voice
                    and prev.get("model") == model and (audio_dir / fname).is_file()):
                clips[genre] = prev
                log(f"  {key}秒 {genre}: 作成済み({prev['sec']:.1f}秒)")
                continue
            wav = make(text)
            (audio_dir / fname).write_bytes(wav)
            sec = wav_seconds(wav)
            clips[genre] = {"file": fname, "sec": round(sec, 2), "source": text, "voice": voice, "model": model}
            log(f"  {key}秒 {genre}: {sec:.1f}秒「{text}」")
            audio[key] = {**(old.get(key) or {}), **clips}
            if on_progress:
                on_progress(saved)  # 1つ作るたびに保存。途中で止まっても、続きから再開できる
        if clips:
            audio[key] = clips
        else:
            audio.pop(key, None)
    for k in [k for k in audio if k not in done]:
        audio.pop(k)  # すごい場面でなくなった分は消す
    return saved


def _resolve(target: str, out_dir: Path) -> tuple[Path, Path]:
    p = Path(target)
    if p.suffix == ".json":
        return p, p.with_suffix("")
    return out_dir / f"{target}.json", out_dir / target


def main() -> int:
    from analyze import _load_dotenv, validate_api_key  # 同じキーの読み方を使う

    ap = argparse.ArgumentParser(description="すごい場面の読み上げ音声を作る(Gemini TTS)")
    ap.add_argument("target", help="動画ID、または out/動画ID.json")
    ap.add_argument("--out", default="out")
    ap.add_argument("--model", help=f"音声モデル(既定: 環境変数 GEMINI_TTS_MODEL か {DEFAULT_TTS_MODEL})")
    ap.add_argument("--voice", default=os.environ.get("GEMINI_TTS_VOICE") or DEFAULT_VOICE)
    ap.add_argument("--force", action="store_true", help="作成済みも作り直す")
    ap.add_argument("--rpm", type=float, default=6.0,
                    help="1分あたりの最大リクエスト数(既定: 6)。回数制限に当たるなら下げる(例: --rpm 3)")
    args = ap.parse_args()

    _load_dotenv()
    json_path, audio_dir = _resolve(args.target, Path(args.out))
    if not json_path.is_file():
        print(f"解析結果が見つかりません: {json_path}\n先に analyze.py で解析してください", file=sys.stderr)
        return 2
    saved = json.loads(json_path.read_text(encoding="utf-8"))
    if "data" not in saved:
        saved = {"meta": {}, "data": saved}
    key = (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()
    problem = validate_api_key(key) if key else "環境変数 GEMINI_API_KEY を設定してください(.env)"
    if problem:
        print(problem, file=sys.stderr)
        return 2

    from google import genai

    client = genai.Client(api_key=key)
    model = args.model or os.environ.get("GEMINI_TTS_MODEL") or DEFAULT_TTS_MODEL
    print(f"音声を作ります: {json_path.name}  モデル: {model}  声: {args.voice}")
    def save(_s=None):
        json_path.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")

    interval = 60.0 / args.rpm if args.rpm > 0 else 0.0
    try:
        saved = generate(client, saved, audio_dir, model, args.voice, args.force,
                         min_interval=interval, on_progress=save)
    except Exception as err:  # noqa: BLE001
        save()  # ここまでに作れた分は残す
        print(f"\n失敗しました: {err}\n作れた分は保存済みです。もう一度同じコマンドを実行すると、続きから作ります。"
              f"\n回数制限なら、少し待つか --rpm 3 のように下げるか、--model gemini-3.8-flash-tts を試してください", file=sys.stderr)
        return 1
    json_path.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n完了: {len(saved['meta']['audio'])}場面 → {audio_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
