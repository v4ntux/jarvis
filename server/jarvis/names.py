"""Matching the names people say out loud with the people they mean.

«Сардору», «Sardor» and «сардор» are one name: letters go to Latin and Russian endings are cut before comparing.
"""
import re

TRANSLIT = str.maketrans({"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z",
                          "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
                          "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh",
                          "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya"})


def name_words(text: str) -> list[str]:
    """Latin letters, endings cut: «Сардору», «Sardor» and «сардор» all become «sardor»."""
    latin = text.lower().translate(TRANSLIT).replace("kh", "h")
    return [re.sub(r"(?<=\w{3})[aeiouy]+$", "", w) for w in re.findall(r"[a-z0-9]+", latin)]


def _covers(wanted: list[str], have: list[str]) -> bool:
    # Short words must match whole: «Сар» should not quietly become Сардор
    return bool(wanted) and all(any(h == w or (len(w) >= 4 and h.startswith(w)) for h in have) for w in wanted)


def name_matches(query: str, user) -> bool:
    """Every word asked for is the start of one of the person's names (first, last, username)."""
    have = name_words(" ".join(p for p in (getattr(user, "first_name", None), getattr(user, "last_name", None),
                                           getattr(user, "username", None)) if p))
    return _covers(name_words(query), have)


def mentioned(query: str, text: str) -> bool:
    """Did the owner say this person (a name, @username, link or number) in `text`?"""
    query = query.strip()
    if query.startswith("@") or "t.me/" in query:
        return query.rstrip("/").split("/")[-1].lstrip("@").lower() in text.lower()
    digits = re.sub(r"\D", "", query)
    if len(digits) >= 7:
        return digits in re.sub(r"\D", "", text)
    return _covers(name_words(query), name_words(text))
