"""YouTube動画のURLをGeminiに渡して、時刻ごとの初心者向け解説JSONを作る。

使い方:
    python analyze.py <YouTubeのURL> [--duration 秒] [--force]

APIキーは環境変数 GEMINI_API_KEY か、同じ階層の .env ファイルに書く(コードやチャットには書かない)。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from check import Report, check_result, format_report
from schema import JSON_SCHEMA

DEFAULT_MODEL = "gemini-3.8-flash"


class AnalysisFailed(Exception):
    """やり直しても品質チェックを通らなかった。last_result に最後の結果が入る。"""

    def __init__(self, message: str, last_result: dict | None = None):
        super().__init__(message)
        self.last_result = last_result


def extract_video_id(url: str) -> str:
    u = urlparse(url)
    if u.hostname == "youtu.be":
        return u.path.lstrip("/")
    v = parse_qs(u.query).get("v")
    if v:
        return v[0]
    m = re.match(r"^/(?:shorts|embed|live)/([\w-]{6,})", u.path)
    if m:
        return m.group(1)
    raise ValueError(f"YouTubeの動画IDを取り出せません: {url}")


def build_prompt(feedback: str = "") -> str:
    prompt = """あなたは、サッカーに詳しい経験者です。サッカーのルールをまったく知らない友人と、隣に座って一緒にこの動画を見ています。
経験者が「おっ、すごい!」と感じる場面で、友人も同じ熱量で盛り上がれるように、場面ごとの解説を指定のJSONで作ってください。

【読み手】
- 読み手は、サッカーのルールも用語も知りません。戦術を学びたい人ではなく、「隣の人がなぜ盛り上がっているのか」を知りたい人です。
- 説明しすぎは禁物です。見下されたと感じさせず、短く、必要なことだけを書きます。

【ルール】
- events は時刻順。見どころに絞る。ハイライト動画は場面が密集するので、目安は1分あたり3〜8件。
- time は動画の再生位置を MM:SS で書く。seconds は同じ時刻の秒数。映像で確認できた時刻だけを書き、推測で埋めない。
- event は決められた種類から選ぶ。当てはまらなければ other。
- scene: 映像で見えた事実だけを客観的に40字以内で書く。評価や解釈は入れない(あとで「あの場面」を探す手がかりにも使う)。
- gist: 動画の下の1行に出す、ひと目で読める一言を24字以内で書く。すごい場面は「何がすごいか」(例: 逆サイドへ一気に通した)、そうでなければ「何が起きたか」(例: クロスが上がる)。専門用語は避ける。\n- explain: 何が起きたかを、ルールを知らない人向けに一言(60字以内)で書く。専門用語を使うなら必ず terms に入れて説明する。
- context: ルールが絡んで、一目では何が起きたか分かりにくい場面(オフサイド、ファウルの判定など)だけ、補足を70字以内で書く。それ以外は空文字にする。

【「すごい」の判定(sugoi)】
- sugoi は、サッカー経験者が思わず「おっ」と言う場面だけ true にする。目安は全体の3〜5割まで。何でもすごいと言わない。
- ゴールやセーブのように、初心者にも分かる場面だけに偏らない。ボールを持たない動き、最初のタッチ、相手の守備を崩すパス、先読みの守備など、**初心者が見落としやすいすごさ**を優先して拾う。
- sugoiType は、すごさの種類を選ぶ。sugoi が false なら none。
- expertWhy: 経験者がなぜすごいと感じるかを、100字以内で書く。「上手い」「すごい」だけで終わらず、何が難しいか・何を防いだか・何を可能にしたかを書く。sugoi が false なら空文字。

【たとえ(analogies)】
- sugoi が true の場面には、ジャンル rpg と fps の両方で、たとえを1つずつ書く。sugoi が false なら空配列。
- たとえは、「経験者がすごいと感じる構造」をそのままゲームの場面に置き換える。表面的な似方(「ボールを蹴る=攻撃魔法」など)ではなく、何が難しいか・何を可能にしたかの構造を合わせる。
- そのゲームを知らない人にも通じるよう、ゲーム内の専門用語(例: エントリー、クラッチ、ヘイト)は使わない。サッカーの用語も、たとえの中には使わない。
- 構造がうまく合わないときは、無理にたとえず、そのジャンルは省く。
- differs には、そのたとえと実際のサッカーで違うところを、必ず60字以内で書く。
- text は90字以内。

【その他】
- terms: explain と context に出てくるサッカー用語(例: オフサイド、クロス)と、一言の説明。
- 選手名・チーム名は、画面の表示や実況から確実に読み取れたものだけを書く。確かでなければ書かず、「ユニフォームが赤い選手」のように見えたとおりに描写する。
- 確信が持てない場面は confidence を低くする(0〜1)。推測で断定しない。
- preview.watchPoints: この動画を見る前に知っておくと分かりやすい注目ポイントを3つ。
- preview.basics: この動画を理解するのに必要な前提ルールを最大3つ。
- すべて日本語で書く。
"""
    if feedback:
        prompt += f"\n【前回の出力の問題】\n{feedback}\n上の問題を直して、JSON全体を出し直してください。\n"
    return prompt


def _strip_fence(text: str) -> str:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.I)
    return re.sub(r"\s*```$", "", t)


def _status(err: Exception):
    return getattr(err, "code", None) or getattr(err, "status_code", None)


def analyze_video(
    client,
    url: str,
    model: str = DEFAULT_MODEL,
    processing: dict | None = None,
    duration: float | None = None,
    max_retries: int = 3,
    log=lambda _msg: None,
    backoff_s: float = 2.0,
) -> dict:
    """成功すると {video_id, data, report, attempts} を返す。失敗すると AnalysisFailed。"""
    video_id = extract_video_id(url)
    feedback = ""
    last: dict | None = None

    for attempt in range(1, max_retries + 1):
        log(f"試行 {attempt}/{max_retries} ...")
        video_part = {"type": "video", "uri": url}
        if processing:
            video_part["processing"] = processing
        try:
            interaction = client.interactions.create(
                model=model,
                # 公式ガイドに合わせて、動画パートの後にテキストを置く
                input=[video_part, {"type": "text", "text": build_prompt(feedback)}],
                response_format={"type": "text", "mime_type": "application/json", "schema": JSON_SCHEMA},
            )
            text = interaction.output_text
        except Exception as err:  # noqa: BLE001
            status = _status(err)
            retryable = status == 429 or (isinstance(status, int) and status >= 500)
            log(f"API呼び出しに失敗: {err}{'(待って再試行)' if retryable else ''}")
            if not retryable or attempt == max_retries:
                raise
            time.sleep(backoff_s * 2 ** (attempt - 1))
            continue

        try:
            data = json.loads(_strip_fence(text or ""))
        except json.JSONDecodeError:
            feedback = "出力がJSONとして読めませんでした。JSONだけを出力してください。"
            last = {"report": Report(errors=[feedback]), "raw": text}
            continue

        report = check_result(data, duration=duration, video_id=video_id)
        last = {"report": report, "raw": text, "data": data}
        if not report.errors:
            return {"video_id": video_id, "data": data, "report": report, "attempts": attempt}

        feedback = "\n".join(f"- {e}" for e in report.errors)
        log(f"品質チェックでエラー {len(report.errors)}件。やり直します。")

    raise AnalysisFailed(f"{max_retries}回試しましたが、品質チェックを通りませんでした", last)


def _load_dotenv() -> str | None:
    """.env を読む。環境変数に古い値が残っていても、.env の値を優先する。読んだ .env のパスを返す。"""
    try:
        from dotenv import find_dotenv, load_dotenv
    except ImportError:
        return None
    path = find_dotenv(usecwd=False)
    if path:
        load_dotenv(path, override=True)
    return path or None


def validate_api_key(key: str) -> str | None:
    """キーの形に問題があれば、直し方つきのメッセージを返す。中身は表示しない。"""
    if not key.isascii():
        return ("APIキーに、英数字以外の文字が入っています。.env の GEMINI_API_KEY= の右側が、"
                "初期値(ここにキー)のままか、日本語が混ざっていませんか。AIzaで始まる英数字だけにしてください")
    if any(c.isspace() for c in key):
        return "APIキーの途中に空白や改行が入っています。.env の GEMINI_API_KEY= の行を確認してください"
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="YouTube動画 → 初心者向け解説JSON(Gemini)")
    ap.add_argument("url", help="YouTubeの動画URL(公開動画のみ)")
    ap.add_argument("--out", default="out", help="保存先(既定: out)")
    ap.add_argument("--model", help=f"モデル名(既定: 環境変数 GEMINI_MODEL か {DEFAULT_MODEL})")
    ap.add_argument("--retries", type=int, default=3, help="やり直し回数(既定: 3)")
    ap.add_argument("--duration", type=float, help="動画の長さ(秒)。分かれば品質チェックが厳しくなる")
    ap.add_argument("--start", type=float, help="解析する区間の開始(秒)。未検証の組み合わせ")
    ap.add_argument("--end", type=float, help="解析する区間の終了(秒)。未検証の組み合わせ")
    ap.add_argument("--fps", type=float, help="1秒あたりのフレーム数。未検証の組み合わせ")
    ap.add_argument("--force", action="store_true", help="保存済みでも解析し直す")
    args = ap.parse_args()

    env_path = _load_dotenv()
    try:
        video_id = extract_video_id(args.url)
    except ValueError as err:
        print(f"{err}\nYouTubeの動画URL(https://www.youtube.com/watch?v=... など)を渡してください", file=sys.stderr)
        return 2
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{video_id}.json"

    if out_file.exists() and not args.force:
        print(f"保存済みの結果があります: {out_file}(解析し直すなら --force)")
        saved = json.loads(out_file.read_text(encoding="utf-8"))
        print(format_report(check_result(saved["data"], duration=args.duration, video_id=video_id)))
        return 0

    api_key = (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()
    if not api_key:
        print("環境変数 GEMINI_API_KEY を設定してください(.env に書くのが楽。キーはチャットやコードに書かない)", file=sys.stderr)
        return 2
    problem = validate_api_key(api_key)
    if problem:
        print(f"{problem}\n読み込んだ .env: {env_path or '(見つかりません)'}", file=sys.stderr)
        return 2

    from google import genai

    client = genai.Client(api_key=api_key)

    processing = None
    if args.start is not None or args.end is not None or args.fps is not None:
        processing = {"type": "static"}
        if args.start is not None:
            processing["start_offset"] = f"{args.start:g}s"
        if args.end is not None:
            processing["end_offset"] = f"{args.end:g}s"
        if args.fps is not None:
            processing["fps"] = args.fps

    model = args.model or os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL
    print(f"読み込んだ .env: {env_path or '(なし)'}  キーの長さ: {len(api_key)}文字")
    print(f"解析開始: {args.url}\nモデル: {model}" + (f"\n区間/fps: {json.dumps(processing)}" if processing else ""))

    try:
        res = analyze_video(
            client, args.url, model=model, processing=processing,
            duration=args.duration, max_retries=args.retries, log=lambda m: print(f"  {m}"),
        )
    except AnalysisFailed as err:
        last = err.last_result or {}
        failed = out_dir / f"{video_id}.failed.txt"
        failed.write_text(last.get("raw") or "", encoding="utf-8")
        print(f"\n{err}\n", file=sys.stderr)
        if last.get("report"):
            print(format_report(last["report"]), file=sys.stderr)
        print(f"\n最後の生出力を保存しました: {failed}", file=sys.stderr)
        return 1
    except Exception as err:  # noqa: BLE001
        print(f"\n失敗しました: {err}", file=sys.stderr)
        return 1

    saved = {
        "meta": {
            "videoId": video_id, "url": args.url, "model": model,
            "analyzedAt": datetime.now(timezone.utc).isoformat(), "attempts": res["attempts"],
        },
        "data": res["data"],
    }
    out_file.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / f"{video_id}.report.txt").write_text(format_report(res["report"]), encoding="utf-8")
    print(f"\n保存しました: {out_file}\n")
    print(format_report(res["report"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
