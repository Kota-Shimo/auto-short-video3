# topic_picker.py – AUTOの多様化 & 日替わり固定 + 会話視点 (speaker/listener)
# 改訂点（要約）
#  1) 日替わり固定（DAILY_LOCK=1）の実装：UTC日付＋言語ごとに安定的に1テーマを選択。
#  2) ROLE_MODE 対応：fixed / rotate / random を実装（rotate は直近ロールを回す）。
#     - ROLE_FIXED="speaker,listener" で固定指定可（例: "guest,receptionist"）
#  3) 類似回避を強化：直近とのキー被りだけでなく、“機能-シーン”の粗い重なりも抑制。
#  4) フック互換のタイトル形式（functional – scene）出力を追加（テーマ表示を安定化）。
#     - THEME_STYLE="plain|functional"（既定: plain）。"functional" で「機能 – シーン」化。
#  5) 戻り値は従来どおり：vocab では spec dict、return_context=False ならテーマ文字列。
#  6) 追加：TOPIC_PURE_RANDOM=1 で “完全ランダム抽選（履歴＆日替わり固定を無視）” を選択可能。

import os, json, hashlib, datetime as dt, re, random, secrets
from pathlib import Path
from typing import Any, Tuple
from config import TEMP

# ========= 基本設定 =========
RECENT_BLOCK_N = int(os.getenv("RECENT_BLOCK_N", "7"))      # 直近ブロック数
DIFF_LEVEL     = os.getenv("DIFF_LEVEL", "A2")              # A1/A2/B1/B2
WORDS_DEFAULT  = int(os.getenv("VOCAB_WORDS", "6"))         # 単語数
DAILY_LOCK     = os.getenv("TOPIC_DAILY", "1") == "1"       # 日替わり固定（UTC基準）
ROLE_MODE      = os.getenv("ROLE_MODE", "rotate").lower()   # fixed / rotate / random
ROLE_FIXED     = os.getenv("ROLE_FIXED", "").strip()        # "guest,receptionist" のように指定
THEME_STYLE    = os.getenv("THEME_STYLE", "plain").lower()  # plain / functional
TOPIC_PURE_RANDOM = os.getenv("TOPIC_PURE_RANDOM", "0") == "1"  # ★ 完全ランダム切替

# ========= テーマ候補プール =========
POOL = [
    # 接客・旅行
    "hotel check-in small talk","polite requests at a restaurant","ordering coffee at a cafe",
    "asking for directions in a station","train tickets and platforms","airport check-in basics",
    "asking about opening hours","making a reservation by phone","confirming a booking",
    "check-out phrases","lost and found basics","simple troubleshooting at a counter",
    # 日常会話
    "weather and weekend plans","small talk at workplace","hobbies and free time",
    "giving short compliments","simple apologies and excuses","asking availability",
    "inviting and declining politely","giving short instructions","following up politely",
    # 買い物・支払
    "convenience store shopping","asking price and payment options","refunds and exchanges",
    "membership and points","delivery and pickup options",
    # 生活・健康
    "gym and health routine","simple doctor reception phrases","pharmacy basics",
    # SNS/IT
    "sharing photos and links","simple app troubleshooting",
    # 観光
    "tickets and time for attractions","photo spots and recommendations",
]

# ========= 各シーンに対応する視点ペア =========
ROLE_MAP = {
    "restaurant": ("customer", "waiter"),
    "cafe": ("customer", "barista"),
    "hotel": ("guest", "receptionist"),
    "train": ("traveler", "station staff"),
    "airport": ("traveler", "staff"),
    "shop": ("customer", "clerk"),
    "pharmacy": ("customer", "pharmacist"),
    "doctor": ("patient", "receptionist"),
    "work": ("colleague", "colleague"),
    "gym": ("member", "trainer"),
    "sns": ("friend", "friend"),
    "weather": ("friend", "friend"),
    "reservation": ("customer", "operator"),
    "lost": ("traveler", "staff"),
    "photo": ("traveler", "local"),
}

# ========= 類似テーマ判定 =========
KEYMAP = {
    "restaurant": ["restaurant","order","menu","request","polite","table","bill"],
    "cafe":       ["cafe","coffee","barista","order","latte","americano"],
    "hotel":      ["hotel","check-in","check-out","room","reception","front desk"],
    "train":      ["train","station","platform","ticket","line","car"],
    "airport":    ["airport","flight","check-in","boarding","gate","luggage"],
    "work":       ["workplace","follow up","instruction","colleague","office","task"],
    "shop":       ["shopping","store","price","refund","exchange","payment","receipt"],
    "health":     ["doctor","pharmacy","medicine","reception"],
    "tour":       ["photo","attractions","tickets","time","recommendations"],
    "daily":      ["weather","weekend","hobbies","availability","invite","decline","compliments","apologies","instructions"],
}
def _keyset(t: str) -> set[str]:
    s = (t or "").lower()
    keys = set()
    for k, words in KEYMAP.items():
        if any(w in s for w in words):
            keys.add(k)
    return keys

def _same_bucket(a: str, b: str) -> bool:
    ka, kb = _keyset(a), _keyset(b)
    return bool(ka & kb)

def _too_similar(a: str, b: str) -> bool:
    if _same_bucket(a, b):
        return True
    wa, wb = set(re.findall(r"[a-z]+", (a or "").lower())), set(re.findall(r"[a-z]+", (b or "").lower()))
    return len(wa & wb) >= 3

# ========= 最近テーマ保存 =========
def _recent_path(lang: str) -> Path:
    p = TEMP / f"recent_topics_{lang}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p

def _load_recent(lang: str) -> list[str]:
    try:
        return json.loads(_recent_path(lang).read_text(encoding="utf-8"))
    except Exception:
        return []

def _save_recent(lang: str, topic: str):
    lst = [x for x in _load_recent(lang) if x] + [topic]
    if len(lst) > RECENT_BLOCK_N:
        lst = lst[-RECENT_BLOCK_N:]
    _recent_path(lang).write_text(json.dumps(lst, ensure_ascii=False, indent=2), encoding="utf-8")

# ========= 直近期のロール履歴 =========
def _role_hist_path(lang: str) -> Path:
    p = TEMP / f"recent_roles_{lang}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p

def _load_roles(lang: str) -> list[Tuple[str,str]]:
    try:
        return [tuple(x) for x in json.loads(_role_hist_path(lang).read_text(encoding="utf-8"))]
    except Exception:
        return []

def _save_role(lang: str, sp: str, ls: str):
    hist = _load_roles(lang)
    hist.append((sp, ls))
    if len(hist) > 12:
        hist = hist[-12:]
    _role_hist_path(lang).write_text(json.dumps(hist, ensure_ascii=False), encoding="utf-8")

# ========= 日替わりシード =========
def _daily_seed(lang: str) -> int:
    today = dt.datetime.utcnow().strftime("%Y%m%d")
    base = f"{lang}:{today}:{os.getenv('TOPIC_SALT','v1')}"
    return int(hashlib.sha256(base.encode()).hexdigest(), 16) % (10**9)

# ========= 視点ローテーション選択 =========
def _pick_role_for(theme: str, audio_lang: str | None = None) -> tuple[str, str]:
    # fixed
    if ROLE_MODE == "fixed" and ROLE_FIXED:
        parts = [p.strip() for p in ROLE_FIXED.split(",") if p.strip()]
        if len(parts) == 2:
            return (parts[0], parts[1])

    # ベース候補（テーマに基づく）
    s = (theme or "").lower()
    base_pair = None
    for k, v in ROLE_MAP.items():
        if k in s:
            base_pair = v
            break
    if not base_pair:
        base_pair = ("friend", "friend")

    if ROLE_MODE == "random":
        # base と friend↔friend のどちらかを軽くシャッフル
        if random.random() < 0.25:
            return ("friend", "friend")
        return base_pair

    if ROLE_MODE == "rotate":
        hist = _load_roles(audio_lang or "en")
        last = hist[-1] if hist else None
        # 同じなら裏返す or friend↔friend に落とす
        if last == base_pair:
            if base_pair == ("friend","friend"):
                # 友達同士が続いたら base を採用
                return base_pair
            # 役割を逆回し（例：customer↔waiter）
            return (base_pair[1], base_pair[0])
        return base_pair

    # default
    return base_pair

# ========= AUTOテーマ選択 =========
def _pick_theme_for(lang: str) -> str:
    # ★ 完全ランダムモード：履歴・日替わり固定を一切無視（重複を厳密に避けたい場合はOFFにする）
    if TOPIC_PURE_RANDOM:
        rnd = random.Random(secrets.randbits(64))
        return rnd.choice(POOL)

    recent = _load_recent(lang)

    # DAILY_LOCK：日替わりで安定的に1テーマ
    if DAILY_LOCK:
        rnd = random.Random(_daily_seed(lang))
        candidates = POOL[:]
        rnd.shuffle(candidates)
        for t in candidates:
            if all(not _too_similar(t, r) for r in recent[-RECENT_BLOCK_N:]):
                _save_recent(lang, t)
                return t
        t = "everyday small talk"
        _save_recent(lang, t)
        return t

    # 通常モード：完全ランダムシード＋類似回避
    rnd = random.Random(secrets.randbits(64))
    candidates = POOL[:]
    rnd.shuffle(candidates)

    for t in candidates:
        if all(not _too_similar(t, r) for r in recent[-RECENT_BLOCK_N:]):
            _save_recent(lang, t)
            return t

    t = "everyday small talk"
    _save_recent(lang, t)
    return t

# ========= 機能 – シーン（hook互換の表示整形）=========
# THEME_STYLE="functional" の場合に、だいたいの「機能 – シーン」形に寄せる
def _functionalize(theme: str) -> str:
    if THEME_STYLE != "functional":
        return theme
    s = (theme or "").lower()

    # 粗い分類
    if "restaurant" in s or "cafe" in s:
        func = "polite requests"
        scene = "at a restaurant" if "restaurant" in s else "at a cafe"
        return f"{func} – {scene}"
    if "hotel" in s or "check-out" in s or "check-in" in s:
        return "hotel check-in – small talk" if "check-in" in s else "hotel phrases – front desk"
    if "train" in s or "station" in s:
        return "asking for directions – at a station"
    if "airport" in s:
        return "check-in basics – at an airport"
    if "price" in s or "refund" in s or "payment" in s or "shopping" in s or "store" in s:
        return "numbers and prices – shopping"
    if "doctor" in s or "pharmacy" in s:
        return "health basics – at a clinic"
    if "photo" in s or "attractions" in s or "tickets and time" in s:
        return "tickets and time – sightseeing"
    if "workplace" in s or "follow" in s or "instruction" in s:
        return "small talk – at work"
    if "weather" in s or "weekend" in s or "hobbies" in s:
        return "small talk – everyday life"
    # 既定
    return "everyday phrases – a simple situation"

# ========= context生成 =========
def _make_context(theme: str, lang: str, speaker: str, listener: str) -> str:
    # theme は表示用に functionalize したものを採用（例文の安定化に寄与）
    shown_theme = _functionalize(theme)
    if lang == "ja":
        return (
            f"{speaker}が{listener}に話す場面。テーマは「{shown_theme}」。"
            "予定や感想、時間や場所など、短く自然にやり取りする。"
            "専門用語は避け、日常でよく使う語を優先。"
        )
    return (
        f"A short, natural conversation where a {speaker} speaks to a {listener} "
        f"about '{shown_theme}'. Include small requests, opinions, or confirmations. Prefer everyday language."
    )

# ========= patternヒント補強 =========
def _pattern_hint_for(theme: str, speaker: str, listener: str) -> str:
    s = theme.lower()
    if "restaurant" in s or "cafe" in s:
        return "ordering, asking politely, follow-up requests" if speaker == "customer" else "offering, confirming, suggesting options"
    if "hotel" in s:
        return "check-in, confirming details, asking politely" if speaker == "guest" else "welcoming, explaining, offering help"
    if "train" in s or "station" in s or "airport" in s:
        return "asking time and place, tickets, directions, confirming details"
    if "shopping" in s or "price" in s or "payment" in s or "store" in s:
        return "asking price, options, short reasons" if speaker == "customer" else "explaining options, confirming total, politeness"
    return "short natural exchanges, opinions, confirmations"
    
# ========= トレンドspec =========
def build_trend_spec(theme: str, audio_lang: str, count: int | None = None) -> dict[str, Any]:
    c = int(count or WORDS_DEFAULT)
    sp, ls = _pick_role_for(theme, audio_lang)
    # ロール履歴を保存（rotate 制御のため）
    _save_role(audio_lang, sp, ls)
    return {
        "theme": _functionalize(theme),
        "context": (
            f"Casual talk between {sp} and {ls} about '{_functionalize(theme)}', including plans, tickets, and opinions. [TREND]"
            if audio_lang != "ja" else
            f"{sp}が{ls}に{_functionalize(theme)}について話す日常会話。[TREND]"
        ),
        "count": c,
        "relation_mode": "contextual",
        "pos": ["noun","verb"],
        "difficulty": DIFF_LEVEL,
        "trend": True,
        "speaker": sp,
        "listener": ls,
    }

# ========= AUTO本体 =========
def pick_by_content_type(content_mode: str, audio_lang: str, return_context: bool = False):
    if content_mode != "vocab":
        theme = _pick_theme_for(audio_lang)
        sp, ls = _pick_role_for(theme, audio_lang)
        _save_role(audio_lang, sp, ls)
        ctx = _make_context(theme, audio_lang, sp, ls)
        return (_functionalize(theme), ctx) if return_context else _functionalize(theme)

    theme = _pick_theme_for(audio_lang)
    sp, ls = _pick_role_for(theme, audio_lang)
    _save_role(audio_lang, sp, ls)

    spec = {
        "theme": _functionalize(theme),
        "context": _make_context(theme, audio_lang, sp, ls),
        "count": WORDS_DEFAULT,
        "relation_mode": "contextual",
        "pos": ["noun","verb"],
        "difficulty": DIFF_LEVEL,
        "pattern_hint": _pattern_hint_for(theme, sp, ls),
        "trend": False,
        "speaker": sp,
        "listener": ls,
    }
    return spec if return_context else spec["theme"]