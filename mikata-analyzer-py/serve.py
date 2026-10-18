"""解析結果を見るためのローカルWebサーバー(標準ライブラリだけで動く)。

使い方:
    python serve.py            → http://localhost:8000 を開く
    python serve.py --port 9000

- out/*.json(analyze.py の保存結果)を一覧にして、画面(viewer/index.html)に渡す。
- 保存結果が1つもないときは、sample/good.json を「サンプル」として出す(動画なしで操作を試せる)。
- 既定では自分のPCからしか見られない(127.0.0.1)。同じWi-Fiの仲間に見せるときだけ --host 0.0.0.0。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).parent
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_AUDIO_RE = re.compile(r"^[0-9]{1,6}-[a-z0-9]{1,8}\.wav$")
SAMPLE_ID = "sample"


def _read_saved(path: Path) -> dict | None:
    """保存結果を {meta, data} の形で返す。読めない/形が違うものは None。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    data = raw.get("data", raw)
    if not isinstance(data, dict) or not isinstance(data.get("events"), list):
        return None
    meta = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
    return {"meta": meta, "data": data}


def list_videos(out_dir: Path, sample_dir: Path) -> list[dict]:
    items = []
    if out_dir.is_dir():
        for p in sorted(out_dir.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
            saved = _read_saved(p)
            if saved is None or not _ID_RE.match(p.stem):
                continue
            items.append({
                "id": p.stem,
                "count": len(saved["data"]["events"]),
                "hasVideo": bool(saved["meta"].get("videoId") or _ID_RE.match(p.stem)),
                "sample": False,
            })
    if not items and (sample_dir / "good.json").is_file():
        saved = _read_saved(sample_dir / "good.json")
        if saved:
            items.append({"id": SAMPLE_ID, "count": len(saved["data"]["events"]), "hasVideo": False, "sample": True})
    return items


def load_video(video_id: str, out_dir: Path, sample_dir: Path) -> dict | None:
    if not _ID_RE.match(video_id):
        return None
    if video_id == SAMPLE_ID and not (out_dir / f"{SAMPLE_ID}.json").exists():
        saved = _read_saved(sample_dir / "good.json")
        if saved:
            saved["meta"] = {"videoId": None, "sample": True}
        return saved
    saved = _read_saved(out_dir / f"{video_id}.json")
    if saved is not None:
        # 画面側が埋め込みに使うID。保存結果のmetaに無ければ、ファイル名(=動画ID)を使う
        saved["meta"].setdefault("videoId", video_id)
    return saved


def make_server(out_dir: Path, sample_dir: Path, host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    viewer = HERE / "viewer" / "index.html"

    class Handler(BaseHTTPRequestHandler):
        server_version = "mikata-viewer"

        def log_message(self, fmt, *args):  # 静かに
            pass

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: int, obj) -> None:
            self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def do_HEAD(self):  # noqa: N802
            self.do_GET()

        def do_GET(self):  # noqa: N802
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                try:
                    self._send(200, viewer.read_bytes(), "text/html; charset=utf-8")
                except OSError:
                    self._send(500, "viewer/index.html が見つかりません".encode("utf-8"), "text/plain; charset=utf-8")
            elif path == "/api/videos":
                self._json(200, list_videos(out_dir, sample_dir))
            elif path.startswith("/api/videos/"):
                vid = path[len("/api/videos/"):]
                saved = load_video(vid, out_dir, sample_dir)
                if saved is None:
                    self._json(404, {"error": "見つかりません"})
                else:
                    self._json(200, saved)
            elif path.startswith("/api/audio/"):
                parts = path[len("/api/audio/"):].split("/")
                f = out_dir / parts[0] / parts[1] if len(parts) == 2 and _ID_RE.match(parts[0]) and _AUDIO_RE.match(parts[1]) else None
                try:
                    self._send(200, f.read_bytes(), "audio/wav") if f else self._json(404, {"error": "見つかりません"})
                except OSError:
                    self._json(404, {"error": "見つかりません"})
            else:
                self._json(404, {"error": "見つかりません"})

    return ThreadingHTTPServer((host, port), Handler)


def main() -> int:
    ap = argparse.ArgumentParser(description="解析結果のビューアを起動")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1", help="仲間にも見せるときだけ 0.0.0.0")
    ap.add_argument("--out", default="out", help="analyze.py の保存先")
    args = ap.parse_args()

    try:
        srv = make_server(Path(args.out), HERE / "sample", args.host, args.port)
    except OSError as err:
        print(f"起動できません: {err}\n別のポートで試してください(例: python serve.py --port 8001)", file=sys.stderr)
        return 1
    shown = "localhost" if args.host in ("127.0.0.1", "0.0.0.0") else args.host
    print(f"ビューアを起動しました → http://{shown}:{args.port}  (止めるには Ctrl+C)")
    if args.host == "0.0.0.0":
        print("注意: 同じネットワークの人から見える状態です")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n停止しました")
    return 0


if __name__ == "__main__":
    sys.exit(main())
