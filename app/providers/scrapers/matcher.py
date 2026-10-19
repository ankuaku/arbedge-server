"""Cross-bookmaker event matching: same game, different spellings."""
import re
import unicodedata

# add your own: every left side is rewritten to the right side before matching
ALIASES = {
    "manchester united": "man united", "man utd": "man united",
    "manchester city": "man city",
    "tottenham hotspur": "tottenham", "spurs": "tottenham",
    "wolverhampton wanderers": "wolves", "wolverhampton": "wolves",
    "internazionale": "inter", "inter milan": "inter",
    "paris saint germain": "psg", "paris sg": "psg",
    "bayern munchen": "bayern munich", "fc bayern": "bayern munich",
    "atletico de madrid": "atletico madrid",
    "los angeles lakers": "lakers", "boston celtics": "celtics",
}
_STRIP = re.compile(r"\b(fc|cf|afc|sc|ac|bk|fk|club|the)\b")


def norm_name(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = ALIASES.get(s, s)
    s = _STRIP.sub("", s)
    return re.sub(r"\s+", " ", s).strip()


def event_key(sport: str, home: str, away: str, commence_ms: int) -> str:
    # 2-hour buckets tolerate small kickoff differences between books
    bucket = commence_ms // (2 * 3600 * 1000)
    a, b = sorted([norm_name(home), norm_name(away)])
    return f"{sport.lower()}|{a}|{b}|{bucket}"
