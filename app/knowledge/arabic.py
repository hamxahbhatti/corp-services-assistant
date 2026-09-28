"""Light Arabic/English normalisation for keyword search (mirrors what the ar.microsoft / ar.lucene analyzers do)."""
import re

_DIACRITICS = re.compile(r"[ؗ-ًؚ-ْٰۖ-ۭ]")
_TATWEEL = "ـ"
_ARABIC = re.compile(r"[؀-ۿ]")
_TOKEN = re.compile(r"[\w؀-ۿ]+", re.UNICODE)

STOP = set("""the a an of to and or in on for is are be by with at from this that it as your you i my me we our
can how what which when who where do does did will shall must may about into than then there their them
في من على إلى الى عن ما ماذا هل كيف متى أين اين هذا هذه ذلك التي الذي او أو و ثم كل لا لم لن مع عند بعد قبل انا أنا لي
""".split())


def has_arabic(text: str) -> bool:
    return bool(_ARABIC.search(text or ""))


def normalize(text: str) -> str:
    t = _DIACRITICS.sub("", text or "").replace(_TATWEEL, "")
    t = re.sub("[إأآٱ]", "ا", t)
    t = t.replace("ى", "ي").replace("ة", "ه").replace("ؤ", "و").replace("ئ", "ي")
    return t.lower()


def tokens(text: str) -> list[str]:
    out = []
    for tok in _TOKEN.findall(normalize(text)):
        if tok in STOP or len(tok) < 2:
            continue
        if has_arabic(tok):
            for pre in ("وال", "بال", "كال", "فال", "لل", "ال"):
                if tok.startswith(pre) and len(tok) - len(pre) >= 3:
                    tok = tok[len(pre):]
                    break
        out.append(tok)
    return out
