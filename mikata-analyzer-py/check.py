"""解析結果JSONの品質チェッカー。

使い方: python check.py out/<videoId>.json [--duration 秒]
errors があると終了コード1。warnings と review は人の目で見る材料。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field

from pydantic import ValidationError

from schema import GENRES, Result

LIMITS = {
    "scene": 50, "gist": 24, "gist_hard": 50, "explain": 60, "explain_hard": 120, "context": 70, "context_hard": 140,
    "expertWhy": 100, "expertWhy_hard": 200, "analogy": 90, "analogy_hard": 180, "differs": 60, "differs_hard": 120,
}
MAX_SUGOI_RATIO = 0.6  # すごい場面がこれを超えると、何でもすごいと言っている疑い
# たとえに出てくると、その遊びを知らない人に通じにくい言葉(論文でも指摘された「entry fraggerが分からない」型)
GAME_JARGON = [
    "エントリー", "フラガー", "クラッチ", "ピーク", "エイム", "ヘッショ", "スナイプ", "ウルト", "リスポーン",
    "ADS", "キャリー", "ヒーラー", "タンク", "デバフ", "バフ", "アグロ", "ヘイト", "DPS", "ガンク",
]
# たとえの中に出てくると、ルールを知らない人に通じなくなるサッカー用語
SOCCER_JARGON = [
    "オフサイド", "クロス", "ビルドアップ", "ボランチ", "サイドバック", "ワンツー", "トラップ", "ライン間",
    "ハーフスペース", "ポジショナル", "プレス", "カウンター", "ファーストタッチ", "サイドチェンジ", "ゾーン",
]
MAX_GAP_SEC = 45  # これ以上イベントが空くと「見落とし」を疑う
MAX_PER_MIN = 12  # 1分あたりの件数がこれを超えると多すぎ(ハイライトは密集するので緩め)
LOW_CONFIDENCE = 0.6
# 「○○選手」のうち、固有名ではない一般的な言い方
GENERIC_PLAYER = {
    "相手", "味方", "守備", "攻撃", "ディフェンダー", "フォワード", "ミッドフィルダー",
    "ゴールキーパー", "キーパー", "控え", "交代", "この", "その", "あの", "他の", "ほかの",
    "赤い", "青い", "白い", "黒い", "黄色い", "緑の", "赤の", "青の", "白の", "黒の",
    "ユニフォームが赤い", "ユニフォームが青い", "ユニフォームが白い", "背番号10の",
}
_PLAYER_RE = re.compile(r"([^\s、。「」()（）]{1,12})選手")
_TIME_RE = re.compile(r"^(\d{1,3}):([0-5]\d)$")


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    review: list[dict] = field(default_factory=list)  # {seconds, time, reason, link}
    stats: dict = field(default_factory=dict)


def parse_time(t: str) -> int | None:
    m = _TIME_RE.match(str(t))
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def check_result(data: dict, duration: float | None = None, video_id: str | None = None) -> Report:
    rep = Report()

    try:
        parsed = Result.model_validate(data)
    except ValidationError as e:
        for err in e.errors()[:10]:
            loc = ".".join(str(x) for x in err["loc"]) or "(root)"
            rep.errors.append(f"形式エラー: {loc}: {err['msg']}")
        return rep

    preview, events = parsed.preview, parsed.events

    def link(sec: int) -> str:
        return f"https://youtu.be/{video_id}?t={sec}" if video_id else ""

    def flag(e, reason: str) -> None:
        rep.review.append({"seconds": e.seconds, "time": e.time, "reason": reason, "link": link(e.seconds)})

    if len(preview.watchPoints) != 3:
        rep.warnings.append(f"preview.watchPoints は3つが目安です(今: {len(preview.watchPoints)}つ)")
    if len(preview.basics) > 3:
        rep.warnings.append(f"preview.basics は最大3つが目安です(今: {len(preview.basics)}つ)")
    if len(events) < 3:
        rep.warnings.append(f"events が少なすぎます({len(events)}件)")

    prev = -1
    n_sugoi = 0
    seen_explain: dict[str, int] = {}
    for i, e in enumerate(events):
        tag = f"#{i + 1} [{e.time}]"
        sec = parse_time(e.time)

        if sec is None:
            rep.errors.append(f'{tag} time が MM:SS 形式ではありません: "{e.time}"')
        elif sec != e.seconds:
            rep.errors.append(f"{tag} time({sec}秒) と seconds({e.seconds}) が一致しません")

        if e.seconds < prev:
            rep.errors.append(f"{tag} 時刻が前の場面より戻っています({prev}秒 → {e.seconds}秒)")
        if duration and e.seconds > duration:
            rep.errors.append(f"{tag} 動画の長さ({duration:g}秒)を超えています")
        prev = max(prev, e.seconds)

        if not e.gist.strip():
            rep.errors.append(f"{tag} gist が空です")
        elif len(e.gist) > LIMITS["gist_hard"]:
            rep.errors.append(f"{tag} gist が長すぎます({len(e.gist)}字)")
        elif len(e.gist) > LIMITS["gist"]:
            rep.warnings.append(f"{tag} gist が{LIMITS['gist']}字を超えています({len(e.gist)}字)。1行に収まりません")
        if not e.explain.strip():
            rep.errors.append(f"{tag} explain が空です")
        if len(e.explain) > LIMITS["explain_hard"]:
            rep.errors.append(f"{tag} explain が長すぎます({len(e.explain)}字)")
        elif len(e.explain) > LIMITS["explain"]:
            rep.warnings.append(f"{tag} explain が{LIMITS['explain']}字を超えています({len(e.explain)}字)")
        if len(e.scene) > LIMITS["scene"]:
            rep.warnings.append(f"{tag} scene が{LIMITS['scene']}字を超えています({len(e.scene)}字)")
        if len(e.context) > LIMITS["context_hard"]:
            rep.errors.append(f"{tag} context が長すぎます({len(e.context)}字)")
        elif len(e.context) > LIMITS["context"]:
            rep.warnings.append(f"{tag} context が{LIMITS['context']}字を超えています({len(e.context)}字)")

        # すごさ・たとえ
        if e.sugoi:
            n_sugoi += 1
            if not e.expertWhy.strip():
                rep.errors.append(f"{tag} sugoi なのに expertWhy が空です")
            if e.sugoiType == "none":
                rep.errors.append(f"{tag} sugoi なのに sugoiType が none です")
            if not e.analogies:
                rep.warnings.append(f"{tag} sugoi ですが analogies が空です")
            flag(e, "玄人の確認: このすごさと理由は本当に合っているか")
        else:
            if e.expertWhy.strip() or e.analogies:
                rep.warnings.append(f"{tag} sugoi が false なのに expertWhy/analogies があります")
            if e.sugoiType != "none":
                rep.warnings.append(f"{tag} sugoi が false なのに sugoiType が {e.sugoiType} です")
        if len(e.expertWhy) > LIMITS["expertWhy_hard"]:
            rep.errors.append(f"{tag} expertWhy が長すぎます({len(e.expertWhy)}字)")
        elif len(e.expertWhy) > LIMITS["expertWhy"]:
            rep.warnings.append(f"{tag} expertWhy が{LIMITS['expertWhy']}字を超えています({len(e.expertWhy)}字)")

        genres_seen: set[str] = set()
        for a in e.analogies:
            gtag = f"{tag} たとえ({a.genre})"
            if a.genre in genres_seen:
                rep.warnings.append(f"{gtag} が同じジャンルで重複しています")
            genres_seen.add(a.genre)
            if not a.text.strip():
                rep.errors.append(f"{gtag} の text が空です")
            if not a.differs.strip():
                rep.errors.append(f"{gtag} に「実際との違い」(differs)がありません")
            if len(a.text) > LIMITS["analogy_hard"]:
                rep.errors.append(f"{gtag} が長すぎます({len(a.text)}字)")
            elif len(a.text) > LIMITS["analogy"]:
                rep.warnings.append(f"{gtag} が{LIMITS['analogy']}字を超えています({len(a.text)}字)")
            if len(a.differs) > LIMITS["differs_hard"]:
                rep.errors.append(f"{gtag} の differs が長すぎます({len(a.differs)}字)")
            elif len(a.differs) > LIMITS["differs"]:
                rep.warnings.append(f"{gtag} の differs が{LIMITS['differs']}字を超えています({len(a.differs)}字)")
            text = f"{a.text}{a.differs}"
            for w in GAME_JARGON:
                if w in text:
                    flag(e, f"たとえ({a.genre})に専門用語「{w}」: その遊びを知らない人に通じるか")
            for w in SOCCER_JARGON:
                if w in text:
                    rep.warnings.append(f"{gtag} にサッカー用語「{w}」があります(ルールを知らない人には通じません)")
        if e.sugoi and e.analogies:
            missing = [g for g in GENRES if g not in genres_seen]
            if missing:
                rep.warnings.append(f"{tag} たとえが無いジャンル: {', '.join(missing)}")

        if e.confidence < 0 or e.confidence > 1:
            rep.errors.append(f"{tag} confidence が0〜1の範囲外です({e.confidence})")

        # 用語が本文に出てこない
        body = f"{e.scene}{e.gist}{e.explain}{e.context}{e.expertWhy}"
        for t in e.terms:
            if t.term not in body:
                rep.warnings.append(f"{tag} 用語「{t.term}」が scene/explain/why に出てきません")

        # 同じ解説の使い回し
        key = e.explain.strip()
        if key in seen_explain:
            rep.warnings.append(f"{tag} explain が #{seen_explain[key]} と同じです")
        else:
            seen_explain[key] = i + 1

        # 人手確認リスト
        if e.confidence < LOW_CONFIDENCE:
            flag(e, f"自信が低い(confidence {e.confidence:g})")
        if e.event == "other":
            flag(e, "event が other(種類を判別できていない)")
        for m in _PLAYER_RE.finditer(body):
            who = re.sub(r"^.*[のはがをにで]", "", m.group(1))
            # ひらがなで終わる(「いた選手」「していた選手」など)のは名前ではなく説明の一部
            if who and not ("\u3041" <= who[-1] <= "\u309f") and who not in GENERIC_PLAYER and m.group(1) not in GENERIC_PLAYER:
                flag(e, f"固有名の可能性: 「{m.group(0)}」(選手名は人が確認)")

    # 全体の偏り
    for a, b in zip(events, events[1:]):
        gap = b.seconds - a.seconds
        if gap > MAX_GAP_SEC:
            rep.warnings.append(f"{a.time} → {b.time} が{gap}秒空いています(見落としの可能性)")
    span = max(duration or 0, events[-1].seconds if events else 0, 1)
    per_min = len(events) / (span / 60)
    if per_min > MAX_PER_MIN:
        rep.warnings.append(f"場面が多すぎます(1分あたり{per_min:.1f}件)")
    if events and n_sugoi == 0:
        rep.warnings.append("すごい場面(sugoi)が1件もありません")
    elif len(events) >= 4 and n_sugoi / len(events) > MAX_SUGOI_RATIO:
        rep.warnings.append(f"すごい場面が多すぎます({n_sugoi}/{len(events)}件)。何でもすごいと言っていないか確認")

    rep.stats = {
        "events": len(events),
        "sugoi": n_sugoi,
        "perMinute": round(per_min, 1),
        "avgConfidence": round(sum(e.confidence for e in events) / len(events), 2) if events else 0,
        "review": len(rep.review),
    }
    return rep


def format_report(rep: Report) -> str:
    s = rep.stats
    lines = [
        f"場面数 {s.get('events', '-')}(うちすごい場面 {s.get('sugoi', '-')}) / 1分あたり {s.get('perMinute', '-')}件 / 平均confidence {s.get('avgConfidence', '-')}",
        f"エラー {len(rep.errors)}件 / 警告 {len(rep.warnings)}件 / 人手確認 {len(rep.review)}件",
    ]
    if rep.errors:
        lines += ["", "■ エラー(直す必要あり)", *[f"  ✗ {x}" for x in rep.errors]]
    if rep.warnings:
        lines += ["", "■ 警告", *[f"  ! {x}" for x in rep.warnings]]
    if rep.review:
        lines += ["", "■ 人手で確認する場面(リンクを開いて、映像と合っているか見る)"]
        for r in rep.review:
            lines.append(f"  ? [{r['time']}] {r['reason']}")
            if r["link"]:
                lines.append(f"      {r['link']}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="解析結果JSONの品質チェック")
    ap.add_argument("file", help="結果のJSON(analyze.py の保存形式にも対応)")
    ap.add_argument("--duration", type=float, help="動画の長さ(秒)")
    args = ap.parse_args()

    with open(args.file, encoding="utf-8") as f:
        raw = json.load(f)
    data = raw.get("data", raw)  # analyze.py の保存形式(meta+data)にも対応
    video_id = (raw.get("meta") or {}).get("videoId")
    rep = check_result(data, duration=args.duration, video_id=video_id)
    print(format_report(rep))
    return 1 if rep.errors else 0


if __name__ == "__main__":
    sys.exit(main())
