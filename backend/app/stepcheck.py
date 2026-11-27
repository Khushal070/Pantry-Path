"""Recipe steps that match the shopper's final list (spec: product-choice design, section 5).

A3 writes the steps and is told to mention no other ingredient, but a small model doesn't
always listen: a quiche list with milk got "stir in coconut cream". This finds such
strays: foods from a vocabulary (every catalog ingredient and every option the build
offered) named in a step but not on the list. The engine asks A3 once more, naming them;
what is still stray is repaired here: an option the shopper didn't choose becomes what
they chose in the same build step, and a step naming any other stray food is dropped.
"""

import re
from collections.abc import Iterable

ALWAYS = {"salt", "pepper", "black pepper", "water", "ice", "cooking spray", "oil"}
# "instead of bacon", "replace the bacon with ...": allowed ways to name a swapped item
SWAP_PHRASE = re.compile(r"\b(?:instead of|in place of|replace(?:s|d)? the|rather than)\s+"
                         r"[a-z' -]+?(?=[,.;)]| with\b|$)", re.IGNORECASE)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().replace("’", "'")).strip()


def _pattern(term: str) -> re.Pattern[str]:
    return re.compile(rf"\b{re.escape(term)}(?:e?s)?\b")


def is_allowed(term: str, allowed: Iterable[str]) -> bool:
    """On the list, or part of something on it ("cheese" in "swiss cheese", "bread" in
    "sourdough bread"), or a basic everyone has."""
    term = _norm(term)
    if term in ALWAYS:
        return True
    return any(term == a or _pattern(term).search(a) for a in map(_norm, allowed))


def strays(text: str, allowed: Iterable[str], vocabulary: Iterable[str]) -> list[str]:
    """The foods named in text that aren't on the list, longest names first (a longer
    match hides the words inside it: "coconut cream" isn't also reported as "cream")."""
    allowed = list(allowed)
    rest = SWAP_PHRASE.sub(" ", _norm(text))
    found = []
    for term in sorted({_norm(v) for v in vocabulary if v and len(v) > 2}, key=len,
                       reverse=True):
        pattern = _pattern(term)
        if pattern.search(rest):
            rest = pattern.sub(" ", rest)
            if not is_allowed(term, allowed):
                found.append(term)
    return found


def repair(texts: list[str], allowed: Iterable[str], vocabulary: Iterable[str],
           replacements: dict[str, str]) -> list[str]:
    """Each text with its strays replaced (replacements: an unchosen option -> what was
    chosen in its build step); a text with a stray that has no replacement is dropped."""
    allowed = list(allowed)
    out = []
    for text in texts:
        bad = strays(text, allowed, vocabulary)
        if any(_norm(b) not in replacements for b in bad):
            continue
        for b in bad:
            text = re.sub(rf"\b{re.escape(b)}(?:e?s)?\b", replacements[_norm(b)], text,
                          flags=re.IGNORECASE)
        out.append(text)
    return out


def correction(allowed: Iterable[str], found: Iterable[str]) -> str:
    """The extra request for A3's second try."""
    return ("In the steps and summary use only these ingredients: " + ", ".join(allowed) +
            ". Do not mention " + ", ".join(sorted(set(found))) + ".")
