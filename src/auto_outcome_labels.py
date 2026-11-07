"""Conservative labels for unverified task-result claims in check-in prose.

This is a bounded text heuristic, not semantic validation. Quoted examples,
questions, conditional/reported claims and negated clauses provide no label.
Mixed positive/negative claims abstain: they can describe different tasks.
Explicit record_result remains the route for outcomes this heuristic omits.
"""

import re


AUTO_OUTCOME_RULE_VERSION = "guarded_clauses.v1"

_QUOTED = re.compile(
    r"```[\s\S]*?(?:```|$)|`[^`]*(?:`|$)|"
    r'"[^"]*(?:"|$)|“[^”]*(?:”|$)|'
    r"(?<!\w)'(?!s\b)[\s\S]*?(?:'(?!\w)|$)|‘[\s\S]*?(?:’(?!\w)|$)"
)
# A plural possessive inside a single-quoted span can look like its end.
# Residual closing quotes make the whole report ambiguous; do not infer from
# a potentially leaked fragment. Interword apostrophes and possessive 's stay.
_AMBIGUOUS_QUOTE = re.compile(r"['’](?!\w)")
_BLOCKQUOTE = re.compile(r"^[ \t]*>.*$", re.MULTILINE)
# Soft wraps and version/decimal dots must not shed a sentence's frame.
# Keep question marks so the resulting sentence can still be rejected.
_SENTENCES = re.compile(r"(?<!\d)\.|\.(?!\d)|(?<=[!?;])")
# Commas can introduce lists sharing a negation, so only explicit contrast
# begins a new polarity scope. 'and'/'or' keep their inherited negation too.
_CLAUSES = re.compile(r"\b(?:but|however|although)\b")
# A reported/example/hypothetical statement is not the author's task result.
# Reject at sentence scope, so a comma or conjunction cannot shed its frame.
_INDIRECT = re.compile(
    r"\b(?:if|unless|whether|assuming|suppose|hypothetical|hypothetically|"
    r"claims?|claimed|claiming|says?|said|saying|reports?|reported|reporting|"
    r"examples?|quoted?|quoting|discuss\w*|describ\w*|investigat\w*|"
    r"checking|testing|simulat\w*|would|could|might|should|may|will|can|"
    r"plan(?:s|ned|ning)?|intend\w*|expect\w*|hope\w*)\b|\b\w+['’]ll\b"
)
_NEGATED = re.compile(
    r"\b(?:no|not|never|without|neither|none)\b|\b\w+n['’]t\b"
)
_ZERO_COUNT = re.compile(
    r"\b(?:0|zero)\s+(?:errors?|crashes|failures?|regressions?|"
    r"(?:completed|failed|resolved)\s+(?:tasks?|checks?|tests?|findings?)|"
    r"(?:tasks?|checks?|tests?|findings?)\s+(?:completed|failed|resolved))\b|"
    r"\b(?:errors?|crashes|failures?|regressions?)\s*:\s*(?:0|zero)\b|"
    # Terse count lists ('12 passed, 0 failed'), but not 'worker 0 failed'.
    r"(?:^|[,(:]|\band\b)\s*(?:0|zero)\s+(?:failed|completed|resolved)\b"
)
COMPLETION_SIGNAL_RE = re.compile(
    r"\b(?:completed|implemented|deployed|finished|fixed|resolved|shipped|"
    r"merged|built|created|added|refactored|migrated)\b"
)
FAILURE_SIGNAL_RE = re.compile(
    r"\b(?:failed|errors?|broken|reverted|blocked|stuck|crash(?:ed|es)?|"
    r"regressions?)\b"
)


def infer_outcome_label(text: str) -> str | None:
    """Return one weak claim label, or abstain without inventing an outcome.

    Negated failures do not imply success. Positive claims do not override
    adverse ones. A rejected clause does not suppress an independent result
    elsewhere, e.g. 'No errors occurred, but deployment failed'.
    """
    labels = set()
    unquoted = _QUOTED.sub(" ", _BLOCKQUOTE.sub(" ", text.lower()))
    if _AMBIGUOUS_QUOTE.search(unquoted):
        return None
    for sentence in _SENTENCES.split(unquoted):
        if sentence.rstrip().endswith("?") or _INDIRECT.search(sentence):
            continue
        for clause in _CLAUSES.split(sentence):
            if _NEGATED.search(clause) or _ZERO_COUNT.search(clause):
                continue
            if COMPLETION_SIGNAL_RE.search(clause):
                labels.add("task_completed")
            if FAILURE_SIGNAL_RE.search(clause):
                labels.add("task_failed")
    return next(iter(labels)) if len(labels) == 1 else None
