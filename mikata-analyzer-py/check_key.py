"""APIキーが使えるかを、小さな呼び出しで切り分ける。キー自体は表示しない。

使い方:  python check_key.py
  1. キーの形(先頭2文字・長さだけ表示)
  2. REST(models 一覧)           … キーそのものが有効か
  3. SDK の generate_content      … 通常の呼び出し
  4. SDK の interactions          … analyze.py / tts.py が使う呼び出し
どこまで通るかで、原因が分かる。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    from analyze import DEFAULT_MODEL, _load_dotenv, validate_api_key

    env = _load_dotenv()
    key = (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()
    print(f"読み込んだ .env: {env or '(なし)'}")
    if not key:
        print("GEMINI_API_KEY が見つかりません"); return 2
    problem = validate_api_key(key)
    if problem:
        print(problem); return 2
    print(f"1. キーの形: 先頭 {key[:2]}…  長さ {len(key)}文字")

    req = urllib.request.Request("https://generativelanguage.googleapis.com/v1beta/models?pageSize=1",
                                 headers={"x-goog-api-key": key})
    try:
        urllib.request.urlopen(req, timeout=20)
        print("2. REST(models一覧): OK … キーは有効です")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        reason = "ACCESS_TOKEN_TYPE_UNSUPPORTED" if "ACCESS_TOKEN_TYPE_UNSUPPORTED" in body else ""
        print(f"2. REST(models一覧): 失敗 HTTP {e.code} {reason}")
        print("   → このキーは Gemini API では使えない種類です。AI Studio で作り直すか、前に使えていたキーに戻してください")
        return 1
    except OSError as e:
        print(f"2. REST: 通信できません: {e}"); return 1

    from google import genai
    import google.genai as g
    print(f"   google-genai {getattr(g, '__version__', '?')}")
    client = genai.Client(api_key=key)
    try:
        client.models.generate_content(model=DEFAULT_MODEL, contents="1+1は?")
        print("3. SDK generate_content: OK")
    except Exception as e:  # noqa: BLE001
        print(f"3. SDK generate_content: 失敗 {str(e)[:160]}")
    try:
        client.interactions.create(model=DEFAULT_MODEL, input=[{"type": "text", "text": "1+1は?"}])
        print("4. SDK interactions: OK … analyze.py / tts.py も動くはずです")
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"4. SDK interactions: 失敗 {str(e)[:160]}")
        print("   → `pip install -U google-genai` で SDK を最新にして、もう一度試してください")
        return 1


if __name__ == "__main__":
    sys.exit(main())
