"""「共通の記録」のうち、動画解析で得られる部分のスキーマ。

- JSON_SCHEMA: Geminiに渡す。対応していない書き方で弾かれないよう最小限にしている。
- Result など: 受け取ったJSONの形の検証用(pydantic)。
- 厳しい検証(文字数・時刻の整合・たとえの構造など)は check.py 側で行う。

1場面の中身(新方向):
  gist       動画の下の1行に出す一言(24字以内)
  explain    何が起きたか(ルールを知らない人向けの一言)
  context    ルールが絡んで一目で分かりにくいときだけの補足(不要なら空文字)
  sugoi      玄人が「おっ」と思う場面か
  sugoiType  すごさの種類(蓄積・分類用)
  expertWhy  玄人がなぜすごいと感じるか
  analogies  ジャンル別のたとえ。text=たとえ / live=音声で読む実況の一言二言 / differs=実際との違い(必須)
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

EVENT_TYPES = [
    "goal", "shot", "save", "foul", "offside", "corner", "free_kick",
    "penalty", "card", "substitution", "pass", "dribble", "tackle", "other",
]

# すごさの種類。新しい種類が必要になったらここに足す(Geminiへの説明も同じ表から作る)
SUGOI_TYPES = {
    "none": "すごい場面ではない",
    "first_touch": "最初のタッチで相手を外す・次のプレーを楽にする",
    "off_ball": "ボールを持たない動き(走り込み、位置取り、おとり)",
    "line_break": "相手の守備の列を崩すパスや動き",
    "long_pass": "遠くへ正確に通すパス・サイドチェンジ",
    "press": "相手のミスを誘う守備の寄せ・連動",
    "defensive_read": "先読みしたカット・予測の守備",
    "technique": "個人技(ドリブル、ボールさばき、シュートの技術)",
    "goalkeeping": "ゴールキーパーのすごいプレー",
    "teamwork": "複数人の連係・パスワーク",
    "other": "上のどれでもないすごさ",
}

# たとえに使うゲーム。キー(rpg/fps)はデータ上の区別、値が実際にたとえに使うタイトル。
# 読み手(初心者)がよく知っているタイトルに変える。analyze.py の --game rpg=タイトル でも変えられる
DEFAULT_GAMES = {"rpg": "ドラゴンクエスト", "fps": "Apex Legends"}
GENRES = list(DEFAULT_GAMES)

JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "preview": {
            "type": "object",
            "description": "再生前に見せる予告",
            "properties": {
                "watchPoints": {
                    "type": "array",
                    "description": "この動画を見るときの注目ポイント(3つ)",
                    "items": {"type": "string"},
                },
                "basics": {
                    "type": "array",
                    "description": "この動画を理解するのに必要な前提ルール(最大3つ)",
                    "items": {"type": "string"},
                },
            },
            "required": ["watchPoints", "basics"],
        },
        "events": {
            "type": "array",
            "description": "時刻順の場面リスト",
            "items": {
                "type": "object",
                "properties": {
                    "time": {"type": "string", "description": "再生位置。MM:SS 形式"},
                    "seconds": {"type": "integer", "description": "time と同じ時刻の秒数"},
                    "event": {"type": "string", "enum": EVENT_TYPES},
                    "scene": {
                        "type": "string",
                        "description": "映像で見えた事実だけの客観的な描写(40字以内)。検索にも使う",
                    },
                    "gist": {
                        "type": "string",
                        "description": "動画の下の1行に出す、ひと目で読める一言(24字以内)。すごい場面は何がすごいかを、そうでなければ何が起きたかを",
                    },
                    "explain": {
                        "type": "string",
                        "description": "サッカーのルールを知らない人向けに、何が起きたかを一言で(60字以内)",
                    },
                    "context": {
                        "type": "string",
                        "description": "ルールが絡んで一目で分かりにくい場面だけ、その補足(70字以内)。不要なら空文字",
                    },
                    "sugoi": {
                        "type": "boolean",
                        "description": "サッカー経験者が思わず「おっ」と言う場面か",
                    },
                    "sugoiType": {
                        "type": "string",
                        "enum": list(SUGOI_TYPES),
                        "description": "すごさの種類。" + " / ".join(f"{k}={v}" for k, v in SUGOI_TYPES.items()),
                    },
                    "expertWhy": {
                        "type": "string",
                        "description": "経験者がなぜすごいと感じるか(100字以内)。sugoi が false なら空文字",
                    },
                    "analogies": {
                        "type": "array",
                        "description": "ジャンル別のたとえ。sugoi が false なら空配列",
                        "items": {
                            "type": "object",
                            "properties": {
                                "genre": {"type": "string", "enum": GENRES},
                                "text": {"type": "string", "description": "たとえ(90字以内)"},
                                "live": {
                                    "type": "string",
                                    "description": "音声で読む実況の一言二言(40字以内)。そのジャンルの言葉だけで、いま起きているように現在形で。例(RPG): 敵が前衛に集まった!手薄な後衛へ、一撃が通る!",
                                },
                                "differs": {
                                    "type": "string",
                                    "description": "このたとえと実際のサッカーで違うところ(60字以内)",
                                },
                            },
                            "required": ["genre", "text", "live", "differs"],
                        },
                    },
                    "terms": {
                        "type": "array",
                        "description": "explain/context に出てくるサッカー用語とその説明",
                        "items": {
                            "type": "object",
                            "properties": {
                                "term": {"type": "string"},
                                "meaning": {"type": "string"},
                            },
                            "required": ["term", "meaning"],
                        },
                    },
                    "confidence": {
                        "type": "number",
                        "description": "この場面の読み取りへの自信。0〜1",
                    },
                },
                "required": [
                    "time", "seconds", "event", "scene", "gist", "explain", "context",
                    "sugoi", "sugoiType", "expertWhy", "analogies", "terms", "confidence",
                ],
            },
        },
    },
    "required": ["preview", "events"],
}


class _Strict(BaseModel):
    # JSONで受け取った型をそのまま見る("12" を 12 に直したりしない)
    model_config = ConfigDict(strict=True, extra="ignore")


class Term(_Strict):
    term: str
    meaning: str


class Analogy(_Strict):
    genre: Literal["rpg", "fps"]
    text: str
    live: str
    differs: str


class Event(_Strict):
    time: str
    seconds: int
    event: Literal[
        "goal", "shot", "save", "foul", "offside", "corner", "free_kick",
        "penalty", "card", "substitution", "pass", "dribble", "tackle", "other",
    ]
    scene: str
    gist: str
    explain: str
    context: str
    sugoi: bool
    sugoiType: Literal[
        "none", "first_touch", "off_ball", "line_break", "long_pass", "press",
        "defensive_read", "technique", "goalkeeping", "teamwork", "other",
    ]
    expertWhy: str
    analogies: list[Analogy]
    terms: list[Term]
    confidence: float


class Preview(_Strict):
    watchPoints: list[str]  # noqa: N815 (JSONのキー名に合わせる)
    basics: list[str]


class Result(_Strict):
    preview: Preview
    events: list[Event]
