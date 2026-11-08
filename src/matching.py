from difflib import SequenceMatcher
from functools import lru_cache

from src import config

SENIORITY_LEVELS = {
    "senior": "senior", "junior": "junior",
    "i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5",
    "1": "1", "2": "2", "3": "3", "4": "4", "5": "5",
    "اول": "senior",
}

SENIORITY_IN_WORDS = {"高级": "senior", "资深": "senior", "初级": "junior", "中级": "mid"}

LEVEL_AS_FIRST_WORD = {"principal", "staff", "associate", "assistant"}
LEVEL_WORDS = set(SENIORITY_LEVELS) | LEVEL_AS_FIRST_WORD | {"intern", "lead", "head", "chief", "officer"}

STOP_WORDS = {
    "of", "and", "the", "for", "at", "in", "de", "des", "du", "la", "le",
    "pour", "en", "et", "y", "del", "der", "und",
}

TITLE_ABBREVIATIONS = {
    "sr": "senior", "snr": "senior", "jr": "junior",
    "mgr": "manager", "mngr": "manager",
    "eng": "engineer", "engr": "engineer",
    "dev": "developer", "devs": "developers",
    "asst": "assistant", "assoc": "associate",
    "dir": "director", "exec": "executive",
    "admin": "administrator", "mktg": "marketing", "ops": "operations",
    "vp": "vice president", "svp": "senior vice president",
    "evp": "executive vice president", "avp": "assistant vice president",
    "ceo": "chief executive officer", "cto": "chief technology officer",
    "cfo": "chief financial officer", "coo": "chief operating officer",
    "cmo": "chief marketing officer", "cio": "chief information officer",
    "swe": "software engineer", "sde": "software development engineer",
    "mle": "machine learning engineer", "ml": "machine learning",
    "ai": "artificial intelligence", "qa": "quality assurance",
    "hr": "human resources", "rn": "registered nurse",
}


@lru_cache(maxsize=200_000)
def title_key(t_norm):
    if not t_norm:
        return None
    words = []
    for word in t_norm.split():
        words.extend(TITLE_ABBREVIATIONS.get(word, word).split())
    return " ".join(words)


@lru_cache(maxsize=200_000)
def seniority(title):
    if not title:
        return frozenset()
    words = title.split()
    levels = {SENIORITY_LEVELS[w] for i, w in enumerate(words)
              if w in SENIORITY_LEVELS and (len(w) > 1 or i == len(words) - 1)}
    levels.update(level for word, level in SENIORITY_IN_WORDS.items() if word in title)
    if "intern" in words:
        levels.add("intern")
    if "lead" in words and len(words) >= 2:
        levels.add("lead")
    if len(words) >= 2 and words[0] in LEVEL_AS_FIRST_WORD:
        levels.add(words[0])
    if len(words) >= 3 and words[0] == "head" and words[1] == "of":
        levels.add("head")
    if len(words) >= 3 and words[0] == "chief" and words[-1] == "officer":
        levels.add("chief")
    return frozenset(levels)


@lru_cache(maxsize=200_000)
def initials(text):
    if not text:
        return None
    words = [w for w in text.split() if w not in STOP_WORDS]
    if len(words) < 2:
        return None
    return "".join(w[0] for w in words)


def compact(text):
    if text and " " not in text and 2 <= len(text) <= 6:
        return text
    return None


@lru_cache(maxsize=500_000)
def similarity(a, b):
    return SequenceMatcher(None, a, b).ratio()


def token_subset(a, b, min_long_token=4):
    ta, tb = set(a.split()), set(b.split())
    small, big = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if not small or not small <= big:
        return False
    return len(small) >= 2 or any(len(w) >= min_long_token for w in small)


class Position:
    __slots__ = ("n", "companies", "urls", "titles", "company_initials", "title_initials", "start", "end",
                 "anchor", "starts", "last_snap")

    def __init__(self, n):
        self.n = n
        self.companies = set()
        self.urls = set()
        self.titles = set()
        self.company_initials = set()
        self.title_initials = set()
        self.start = None
        self.end = None
        self.anchor = None
        self.starts = set()
        self.last_snap = 0

    def absorb(self, v, snap):
        if v.c:
            self.companies.add(v.c)
            if initials(v.c):
                self.company_initials.add(initials(v.c))
        if v.u:
            self.urls.add(v.u)
        if v.t:
            self.titles.add(v.t)
            if initials(v.t):
                self.title_initials.add(initials(v.t))
        if v.start is not None:
            if self.anchor is None:
                self.anchor = v.start
            self.starts.add(v.start)
            self.start = v.start
        if v.end is not None:
            self.end = v.end
        self.last_snap = snap


class Version:
    __slots__ = ("vid", "first_snap", "ord", "c", "u", "t", "start", "end")

    def __init__(self, vid, first_snap, first_ord, c_norm, u_norm, t_norm, start_day, end_day):
        self.vid = vid
        self.first_snap = first_snap
        self.ord = first_ord
        self.c = c_norm
        self.u = u_norm
        self.t = title_key(t_norm)
        self.start = start_day
        self.end = end_day


SAME = "same"
SIMILAR = "similar"
CLOSE = "close"
ACRONYM = "acronym"
MISSING = "missing"
DIFFERENT = "different"
DIFFERENT_SENIORITY = "different seniority"
RELATED = "related"
LEVEL_CHANGE = "level change"


def compare_company(v, p):
    if v.u and v.u in p.urls:
        return SAME
    if v.c and v.c in p.companies:
        return SAME
    if v.c and p.companies:
        if any(similarity(v.c, c) >= config.COMPANY_SIMILARITY or token_subset(v.c, c) for c in p.companies):
            return SIMILAR
        if not (v.u and p.urls):
            cv, iv = compact(v.c), initials(v.c)
            if (cv and cv in p.company_initials) or (iv and any(compact(c) == iv for c in p.companies)):
                return ACRONYM
        return DIFFERENT
    if v.u and p.urls:
        return DIFFERENT
    return MISSING


def compare_title(v, p):
    if not v.t or not p.titles:
        return MISSING
    if v.t in p.titles:
        return SAME
    cv = compact(v.t)
    if cv and cv in p.title_initials:
        return SIMILAR
    iv = initials(v.t)
    if iv and any(compact(t) == iv for t in p.titles):
        return SIMILAR
    level = seniority(v.t)
    same_level = [t for t in p.titles if seniority(t) == level]
    if not same_level:
        words = meaningful_words(v.t)
        if any(words & meaningful_words(t) for t in p.titles):
            return LEVEL_CHANGE
        return DIFFERENT_SENIORITY
    if any(similarity(v.t, t) >= config.TITLE_SIMILARITY or token_subset(v.t, t, 99) for t in same_level):
        return SIMILAR
    words = meaningful_words(v.t)
    if any(words & meaningful_words(t) for t in same_level):
        return RELATED
    return DIFFERENT


@lru_cache(maxsize=200_000)
def meaningful_words(title):
    words = set()
    for w in title.split():
        for level_word in SENIORITY_IN_WORDS:
            w = w.replace(level_word, "")
        if w and w not in STOP_WORDS and w not in LEVEL_WORDS:
            words.add(w)
    return frozenset(words)


def compare_dates(v, p):
    if p.end is not None and v.start is not None and p.end < v.start:
        return DIFFERENT
    if v.end is not None and p.start is not None and v.end < p.start:
        return DIFFERENT
    if v.start is None or p.anchor is None:
        return MISSING
    if v.start in p.starts:
        return SAME
    if abs(v.start - p.anchor) <= config.START_DATE_TOLERANCE_DAYS:
        return CLOSE
    return DIFFERENT


def is_match(company, title, dates, replaced_last_snapshot):
    if dates == DIFFERENT or title == DIFFERENT_SENIORITY:
        return False
    if company == DIFFERENT:
        return (config.ALLOW_COMPANY_RENAME and title == SAME and dates == SAME
                and replaced_last_snapshot)
    if title == LEVEL_CHANGE:
        return (config.ALLOW_LEVEL_EDIT and company == SAME and dates == SAME
                and replaced_last_snapshot)
    if title == DIFFERENT:
        return False
    if title == RELATED:
        return (config.ALLOW_TITLE_EDIT and company == SAME and dates == SAME
                and replaced_last_snapshot)
    if company == ACRONYM:
        return title in (SAME, SIMILAR) and dates == SAME
    if company == MISSING:
        return title in (SAME, SIMILAR) and dates == SAME
    if title == MISSING:
        return company == SAME and dates == SAME
    if company == SIMILAR and title == SIMILAR:
        return dates in (SAME, CLOSE)
    return True


STRENGTH = {SAME: 3, SIMILAR: 2, ACRONYM: 2, CLOSE: 1}


def match_strength(company, title, dates):
    return sum(STRENGTH.get(f, 0) for f in (company, title, dates))


def compare(v, p, snap):
    dates = compare_dates(v, p)
    if dates == DIFFERENT:
        return None
    title = compare_title(v, p)
    if title == DIFFERENT_SENIORITY:
        return None
    company = compare_company(v, p)
    if is_match(company, title, dates, p.last_snap == snap - 1):
        return match_strength(company, title, dates)
    return None


def assign_user(versions, snapshots):
    by_vid = {v.vid: v for v in versions}
    pos_of = {}
    positions = []

    for snap, vids in snapshots:
        occupied = set()
        new_versions = []
        for vid in vids:
            if vid in pos_of:
                occupied.add(pos_of[vid])
            else:
                new_versions.append(by_vid[vid])

        if not new_versions:
            for vid in vids:
                positions[pos_of[vid] - 1].last_snap = snap
            continue

        pairs = []
        for v in new_versions:
            for p in positions:
                if p.n in occupied:
                    continue
                strength = compare(v, p, snap)
                if strength is not None:
                    pairs.append((-strength, -p.last_snap, p.n, v.ord, v.vid))
        pairs.sort()

        taken = set()
        for _, _, n, _, vid in pairs:
            if vid in pos_of or n in taken:
                continue
            pos_of[vid] = n
            taken.add(n)

        for v in sorted(new_versions, key=lambda v: (v.ord, v.vid)):
            if v.vid not in pos_of:
                p = Position(len(positions) + 1)
                positions.append(p)
                pos_of[v.vid] = p.n

        chosen = {}
        for rank, vid in enumerate(vids):
            v = by_vid[vid]
            n = pos_of[vid]
            key = (v.first_snap, -rank)
            if n not in chosen or key > chosen[n][0]:
                chosen[n] = (key, v)
        for n, (_, v) in chosen.items():
            positions[n - 1].absorb(v, snap)

    return pos_of
