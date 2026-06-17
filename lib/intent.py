"""intent — Tier-1 (regex/heuristic) intent classifier for human prompts.

task-4158 (EPIC 4152). Implements Stream B's 13-category taxonomy, Tier 1 only
(zero-cost regex/heuristic; ~50-70% confident coverage). Tiers 2 (embedding
prototype) and 3 (LLM batch) are deferred — see prompt-intent-taxonomy research.

13 codes (mutually exclusive primary intent, dominant wins):
  CMD command/direct · QRY query/learn · BST brainstorm · PLN plan ·
  DBG debug · RCL recall · DEC decide · COR correct · REV review ·
  VNT vent/reflect · MTA meta/system · SOC social · COO coordinate

Modifier flags (non-exclusive): URGENT UNCERTAIN DELEGATED LONG COMPOUND

Canonical — imported by the backfill and (future) capture hook. Mirrors the
provenance.py pattern so the two classifiers stay consistent.
"""
from __future__ import annotations

import re

HEURISTIC_CONF = 0.75
FALLBACK_CONF = 0.40

# Ordered rules: (code, regex). First match wins → put specific before generic.
# Patterns match against the normalized (lstripped, lowercased) prompt head.
_RULES: list[tuple[str, re.Pattern]] = [
    # SOC — pure greeting/thanks/closer, short
    ("SOC", re.compile(r"^(hi|hello|hey|good (morning|afternoon|evening)|thanks|thank you|"
                       r"great job|nice work|well done|amazing|perfect|love it|ok thanks|"
                       r"yes|yep|yeah|ok|okay|cool|got it|sounds good)[\s.!]*$")),
    # COR — corrective feedback on prior output
    ("COR", re.compile(r"^(no[,. ]|actually[,. ]|that'?s not|that is not|wrong[,. ]|"
                       r"not quite|not right|incorrect|i meant|more like|you (missed|forgot|"
                       r"misunderstood)|undo|revert that)")),
    # COO — multi-agent orchestration
    ("COO", re.compile(r"\b(ask (matt|darren|the \w+ agent|the researcher)|have (the|a) \w+ "
                       r"(agent|do|run)|dispatch|in (the )?background|check on the (background|"
                       r"agent|job|task)|what did (the )?\w+ (find|return|say)|spawn (an|a) )",
                       re.I)),
    # MTA — about the agent/system itself
    ("MTA", re.compile(r"^(/|\bcan you (recall|remember|access|see)|do you (know|have|remember)|"
                       r"what (tools|skills|plugins|personas|can you)|how do you|your (memory|"
                       r"capabilities|config)|/status|run /|use the \w+ skill)", re.I)),
    # DBG — diagnosing a failure
    ("DBG", re.compile(r"(traceback|stack ?trace|exception|error:|\berror\b|not working|"
                       r"doesn'?t work|won'?t \w+|bug\b|failing|failed\b|broken|crash|"
                       r"why is .{0,40}(happening|breaking|failing|not)|is bugging out|"
                       r"is down|unreachable|timed out)", re.I)),
    # RCL — surface prior knowledge/conversation/work
    ("RCL", re.compile(r"\b(remember when|what did we|earlier you said|you (told|said) me|"
                       r"find (the|my|a copy)|look up|look in|search (for|the|my)|recall|"
                       r"dig up|where did (i|we) (put|save)|do you have the context)", re.I)),
    # BST — divergent ideation
    ("BST", re.compile(r"\b(brainstorm|ideas? for|what are some|some ideas|options for|"
                       r"alternatives|explore (ways|options|the)|think broadly|divergent|"
                       r"ways (to|of)|50 ways|10 ways)\b", re.I)),
    # PLN — structuring future work
    ("PLN", re.compile(r"\b(create (a|the|complete).{0,30}plan|let'?s plan|plan for|"
                       r"how should (i|we)|best approach|roadmap|architect|design (the|a|process)|"
                       r"draft (a|the|out|some) (spec|plan|design)|spec(s)? out|sequenc|"
                       r"break (it|this) down|phases?\b|lifecycle)", re.I)),
    # DEC — wants a judgment/recommendation
    ("DEC", re.compile(r"\b(should i|should we|which is (better|best)|pros and cons|"
                       r"help me (choose|decide)|what'?s the (best|right) (choice|option)|"
                       r"\bvs\.?\b|or should|worth it|do you recommend|what would you (do|pick))",
                       re.I)),
    # REV — check/validate/critique an artifact
    ("REV", re.compile(r"\b(review (this|the|my|our)|check (this|for|that|the|if)|"
                       r"does this look|validate|critique|audit|feedback on|sanity check|"
                       r"is this (right|correct|good)|look(s)? (good|right)\?)", re.I)),
    # BST/PLN handled; QRY — information question
    ("QRY", re.compile(r"^(what|how (do|does|can|should i make|would)|why|who|when|where|"
                       r"is (it|there|the)|are (there|the|you)|can i|do you|does|did|have you|"
                       r"will you|explain|tell me about|eli5|what'?s the difference)\b", re.I)),
    # CMD — imperative task delegation
    ("CMD", re.compile(r"^(please\s+)?(can you\s+)?(write|run|build|create|make|add|implement|"
                       r"deploy|fix|update|generate|install|send|draft|set ?up|do|set|configure|"
                       r"refactor|migrate|delete|remove|rename|move|copy|export|import|"
                       r"analyze|process|research|study|investigate|prepare|grab|pull|"
                       r"give me|show me|get me|let'?s (do|build|create|start|make|work))\b", re.I)),
]

URGENT_RE = re.compile(r"\b(asap|immediately|right now|urgent|before (thursday|tomorrow|the|\d)|today\b)", re.I)
UNCERTAIN_RE = re.compile(r"\b(not sure|maybe|i think|might be|could be wrong|wondering if|perhaps|i guess|unsure)\b", re.I)
DELEG_RE = re.compile(r"\b(in (the )?background|have (the|a) \w+ (do|run)|delegate|dispatch|ask (matt|darren|the))\b", re.I)
IMPERATIVE_RE = re.compile(r"(?m)^\s*(please\s+)?[a-z]+\b")  # rough compound detector


def _norm(content: str) -> str:
    c = content.strip()
    if c.startswith("-") and (len(c) == 1 or c[1] in " \t\r\n"):
        c = c[1:].strip()
    # drop a leading "OK"/"So"/"Alright" discourse marker for matching
    c = re.sub(r"^(ok(ay)?|so|alright|right|well|um+|hi claude[,. ]*)[,. ]+", "", c, flags=re.I)
    return c


def classify(content: str | None) -> tuple[str, list[str], float]:
    """Return (intent_code, flags, confidence)."""
    if not content or not content.strip():
        return ("VNT", [], FALLBACK_CONF)
    c = _norm(content)
    flags: list[str] = []
    if URGENT_RE.search(content):
        flags.append("URGENT")
    if UNCERTAIN_RE.search(content):
        flags.append("UNCERTAIN")
    if DELEG_RE.search(content):
        flags.append("DELEGATED")
    if len(content) > 1200:
        flags.append("LONG")
    if content.count("?") >= 2 and len(content) > 200:
        flags.append("COMPOUND")

    for code, rx in _RULES:
        if rx.search(c):
            return (code, flags, HEURISTIC_CONF)

    # Fallbacks (low confidence — Tier 2/3 should re-judge)
    if "?" in c:
        return ("QRY", flags, FALLBACK_CONF)
    return ("CMD", flags, FALLBACK_CONF)
