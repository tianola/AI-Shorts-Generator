"""One brief for every title the app writes (clipper, live studio, narrate), and a checker.

The rules are the same everywhere: a title should make someone want to watch, and it must
be true to the clip. The checker enforces what can be checked in code (length, emoji count,
empty hype, the topic a live stream is about, and at least one word the clip really says),
so a model that ignores the brief cannot publish a generic or empty title.

Titles that promise something the clip does not show are not allowed: the judge and the
checker both reject them.
"""

import re
from typing import List, Optional, Tuple

MIN_CHARS = 12
MAX_CHARS = 45     # room for a "Subject: " prefix; the title card wraps 3 lines of 18
MAX_EMOJIS = 1

# Empty hype: words that say nothing about the clip. Removed from the title when found.
HYPE_PATTERNS = [
    r"you (won'?t|will not|wont) believe( (this|what|it))?",
    r"this is (wild|insane|crazy|unreal)",
    r"(insane|crazy|unreal|shocking|mind[- ]blowing)\b",
    r"wait (for|till|until) (the|it|this) (end|reaction)",
    r"the (secret|truth) no one (talks|tells) about",
]
_HYPE = re.compile("|".join(HYPE_PATTERNS), re.IGNORECASE)
_EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿⭐⭕]")
_STOP = set(
    "about after again against because before being between could does doing during "
    "each from have having here into just more most much only other over same should some such "
    "than that their them then there these they this those through under until very were what "
    "when where which while will with would your said like".split()
)


def brief() -> str:
    """The rules, as text for a prompt. Every pipeline puts this in its title instruction."""
    return (
        f"Title: {MIN_CHARS} to {MAX_CHARS} characters, at most {MAX_EMOJIS} emoji (optional). "
        "Shape it 'Subject: hook'. The subject comes first and names what the clip is about as "
        "a viewer would search for it: the brand, product, person, place or topic (Apple, "
        "iPhone 16, the EU, a farmer's tractor). Without it neither the viewer nor the platform "
        "can tell what the video is about. The hook makes someone need to watch. Pick the feeling "
        "that fits the clip and its topic: curiosity (a question, an unfinished reveal), tension "
        "or worry (a risk, a cost, a threat), or humour (an absurd detail). Build it on the most "
        "surprising concrete thing the clip says (a number, a name, a consequence) and hold back "
        "the answer. The clip must pay that off. Never promise a reveal, stakes or a result the "
        "clip does not show, and no number it does not say. Not a sentence fragment. No empty "
        "hype (\"you won't believe\", \"this is wild\", \"insane\"), no hashtags.\n"
        "  No subject (weak): Europe Takes Aim at Parts Pairing\n"
        "  Strong:            Apple: Europe Takes Aim at Parts Pairing\n"
        "  Describes (weak):  He talks about his first business failing\n"
        "  Tension (strong):  Apple: The $1,000 Fine Hidden in Its Contract\n"
        "  Curiosity:         Apple: Your Slow iPhone Was No Accident\n"
        "  Humour:            iPhone 16: Zap It and the Battery Lets Go"
    )


def clean(title: Optional[str]) -> str:
    """Trim spaces and quotes, and cut to MAX_CHARS at a word boundary."""
    text = re.sub(r"\s+", " ", str(title or "")).strip().strip("\"'“”‘’").strip()
    if len(text) > MAX_CHARS:
        cut = text[:MAX_CHARS + 1].rsplit(" ", 1)[0]
        text = cut if len(cut) >= MIN_CHARS else text[:MAX_CHARS]
    return text.rstrip(" ,;:-")


def _content_words(text: str) -> set:
    return {w for w in re.findall(r"[a-zA-Z]{4,}", text.lower()) if w not in _STOP}


def check_title(title: str, transcript: str = "", topic: str = "") -> List[str]:
    """The reasons a title is not good enough to publish. An empty list means it passes."""
    problems = []
    if not title:
        return ["empty title"]
    if len(title) < MIN_CHARS:
        problems.append(f"shorter than {MIN_CHARS} characters")
    if len(title) > MAX_CHARS:
        problems.append(f"longer than {MAX_CHARS} characters")
    if len(_EMOJI.findall(title)) > MAX_EMOJIS:
        problems.append(f"more than {MAX_EMOJIS} emoji")
    if _HYPE.search(title):
        problems.append("empty hype phrase")
    if topic and topic.lower() not in title.lower():
        problems.append(f"topic \"{topic}\" missing")
    if transcript and not (_content_words(title) & _content_words(transcript)):
        problems.append("no word the clip actually says")
    return problems


def fix_title(title: Optional[str], transcript: str = "", topic: str = "", fallback: bool = True) -> Tuple[str, bool]:
    """Repair what can be repaired, and report whether the title was changed.

    Removes empty hype and extra emoji, cuts to length, and adds the topic in front when a
    live stream's title lost it. A title that still fails the check after that is replaced by
    the clip's own words, so the caller always gets something true to the clip.
    """
    original = str(title or "")
    text = clean(original)
    text = _HYPE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip(" ,;:-")
    emojis = _EMOJI.findall(text)
    for extra in emojis[MAX_EMOJIS:]:
        text = text.replace(extra, "", 1)
    if topic and topic.lower() not in text.lower():
        text = clean(f"{topic}: {text}") if text else topic
    text = re.sub(r"([!?.])\1+", r"\1", text)          # "!!!" -> "!"
    if fallback and (check_title(text, transcript, topic) or not text):
        text = derive_from_transcript(transcript, topic) or text
    if text and text[0].islower():
        text = text[0].upper() + text[1:]
    return text, text != original


_DANGLING = {
    "a", "an", "the", "at", "of", "to", "in", "on", "for", "with", "and", "or", "but", "so",
    "that", "which", "instead", "from", "by", "into", "about", "than", "as", "is", "was",
    "be", "my", "your", "his", "her", "their", "our", "it", "he", "she", "they", "we", "i",
    "last", "first", "next", "whole", "very", "really", "just", "only", "even", "more", "most",
}
_CLAUSE_SPLIT = re.compile(r"[.!?,;:]+\s+|\s+(?:and|but|so|because|when|while)\s+", re.IGNORECASE)


def _complete_phrase(text: str, budget: int) -> str:
    """The longest whole clause of the transcript that fits the budget, never ending on a dangling word.

    Prefers a clause ending at a natural break. Only when no clause fits does it fall back to
    the first words that fit, minus any trailing word that leaves a phrase hanging.
    """
    from services.metadata import _LEAD_FILLER

    spoken = " ".join(re.findall(r"[^\s]+", text or "")).strip()
    words = spoken.split()
    while words and words[0].lower().strip(".,!?") in _LEAD_FILLER:
        words = words[1:]
    spoken = " ".join(words)
    clauses = [c for c in _CLAUSE_SPLIT.split(spoken) if c]
    fitting = [c.strip() for c in clauses if 3 <= len(c.split()) and len(c.strip()) <= budget]
    if fitting:
        return max(fitting, key=lambda c: len(c))
    out: List[str] = []
    for word in words:
        if len(" ".join(out + [word])) > budget:
            break
        out.append(word)
    while out and out[-1].lower().strip(".,!?") in _DANGLING:
        out.pop()
    return " ".join(out)


def derive_from_transcript(transcript: str, topic: str = "") -> str:
    """A plain title from the clip's own words, with the topic in front when there is one.

    The words are cut to fit MAX_CHARS together with the topic, at a whole clause where one fits.
    """
    budget = MAX_CHARS - (len(topic) + 2 if topic else 0)
    head = _complete_phrase(transcript or "", budget).rstrip(" ,;:-")
    if not head:
        return topic or ""
    return clean(f"{topic}: {head}" if topic else head)


async def suggest_title(transcript: str, topic: str = "", count: int = 3) -> Optional[str]:
    """Ask the model for `count` titles in one call and return the first one that passes the checker.

    None when none passes: the caller then keeps what it had, rather than publish a weak title.
    """
    import json

    from services.llm_router import call_llm_json, pick_model

    about = f'The stream is about "{topic}". The title MUST contain "{topic}". ' if topic else ""
    prompt = (
        f"{about}A short video says:\n\"{transcript[:1500]}\"\n\n{brief()}\n"
        f"Give {count} different titles, the strongest first. Return JSON only: "
        '{"titles": ["...", "...", "..."]}'
    )
    raw = await call_llm_json(prompt=prompt, system="You write YouTube Shorts titles. JSON only.",
                              model=pick_model(), temperature=0.4, max_tokens=300)
    titles = json.loads(raw).get("titles", [])
    for candidate in titles:
        fixed, _changed = fix_title(str(candidate), transcript, topic, fallback=False)
        if fixed and not check_title(fixed, transcript, topic):
            return fixed
    return None
