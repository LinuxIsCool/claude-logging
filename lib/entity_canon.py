"""entity_canon — canonicalize entities extracted by the LLM enrichment.

task-4158 (entity fix). The LLM pulls clean entity strings but with surface
variants ("Shawn"/"shawn", "Indigenomics"/"IndigenomicsAI"/"IAI"). This resolves
them to canonical names keyed off MEMORY.md key-people + the claude-ventures
registry, so prompt→person / prompt→venture joins aggregate correctly.

Rule-based + alias maps (no LLM). Unknown names fall back to a normalized
title-cased form. Conservative: only merges aliases we're confident about.
"""
from __future__ import annotations

import re

# canonical -> set of lowercased aliases (MEMORY.md key-people, correct spellings)
_PEOPLE = {
    "Shawn": {"shawn"},
    "Eve": {"eve"},
    "Darren Zal": {"darren", "darren zal"},
    "Carol Anne Hilton": {"carol anne", "carol anne hilton", "carolanne", "caroline"},
    "Pravin": {"pravin", "prav", "pravin pillay"},
    "Hash": {"hash", "mehmet", "mehmet dogan", "hash n"},
    "Jeff Emmett": {"jeff", "jeff emmett"},
    "Jessica Zartler": {"jessica", "jessica zartler"},
    "Gregory Landua": {"gregory", "gregory landua", "landua"},
    "Samu": {"samu", "sammy"},
    "Patricia Parkinson": {"patricia", "patricia parkinson"},
    "Aaron Perry": {"aaron", "aaron perry"},
    "Casson": {"casson"},
    "Kevin": {"kevin"},
    "Dave": {"dave"},
    "Becca": {"becca"},
    "Christina Bowen": {"christina", "christina bowen"},
    "Pete Cork": {"pete", "pete cork"},
    "Daniel Ortiz": {"daniel", "daniel ortiz"},
    "Chris Mills": {"chris", "chris mills"},
    "Eric": {"eric"},
    "Roshan": {"roshan"},
    "Octopus": {"octopus"},
    "Brandon": {"brandon", "brawlaphant"},
    "Darren's NUC (Dobby)": {"dobby"},
    "Kai (dog)": {"kai"},
    "Mom": {"mom"},
}

# canonical -> aliases (claude-ventures registry + MEMORY)
_VENTURES = {
    "Indigenomics AI": {"indigenomics", "indigenomics ai", "indigenomicsai", "iai"},
    "Regen AI": {"regenai", "regen ai"},
    "Regen Network": {"regen network"},
    "BCRG / Avalanche": {"bcrg", "avalanche", "avalanche foundation", "bcrg / avalanche foundation"},
    "Longtail Financial Corp.": {"longtail financial", "ltf", "longtail financial corp", "longtail"},
    "Ecoscene Oasis": {"oasis", "ecoscene oasis", "ecoscene"},
    "Salish Sea Dreaming": {"ssd", "salish sea dreaming"},
    "Civic Intelligence Engine": {"cie", "civic intelligence engine"},
    "Symbiocene Labs": {"symbiocene labs", "symbiocene"},
    "Cascadia Systems": {"cascadia systems", "cascadia"},
    "Kwaxala": {"kwaxala"},
    "TELUS": {"telus"},
    "Legion": {"legion"},
}


def _build(table: dict[str, set[str]]) -> dict[str, str]:
    out = {}
    for canon, aliases in table.items():
        out[canon.lower()] = canon
        for a in aliases:
            out[a] = canon
    return out


_P = _build(_PEOPLE)
_V = _build(_VENTURES)


def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip()).strip(".,").lower()


def canon(name: str, kind: str) -> str | None:
    """kind in {people, ventures, projects, topics}. Returns canonical string
    or None for empties. People/ventures resolve via alias maps; projects/topics
    are normalized + title-cased only."""
    n = _norm(name)
    if not n or len(n) < 2:
        return None
    if kind == "people":
        return _P.get(n) or name.strip().title()
    if kind == "ventures":
        return _V.get(n) or name.strip()
    # projects / topics — light normalize, keep original casing if multiword caps
    return name.strip()
