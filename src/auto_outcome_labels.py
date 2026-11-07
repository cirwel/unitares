"""Conservative labels for unverified task-result claims in check-in prose.

This is a bounded text heuristic, not semantic validation. Quoted examples,
questions, conditional/reported claims and negated clauses provide no label.
Mixed positive/negative claims abstain: they can describe different tasks.
Explicit record_result remains the route for outcomes this heuristic omits.
"""

import re


AUTO_OUTCOME_RULE_VERSION = "guarded_clauses.v1"

_QUOTED = re.compile(
    r"```[\s\S]*?(?:```|$)|`[^`\n]*(?:`|$)|"
    r'"[^"\n]*(?:"|$)|“[^”\n]*(?:”|$)|'
    r"(?<!\w)'[^'\n]*(?:'|$)|‘[^’\n]*(?:’|$)"
)
_SENTENCES = re.compile(r"[^.!?;\n]+[.!?;\n]?")
_CLAUSES = re.compile(r",|\b(?:but|however|although)\b")
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
    r"\b(?:no|not|never|without|neither|none|zero)\b|\b\w+n['’]t\b|\b0\b"
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
    unquoted = _QUOTED.sub(" ", text.lower())
    for match in _SENTENCES.finditer(unquoted):
        sentence = match.group()
        if sentence.rstrip().endswith("?") or _INDIRECT.search(sentence):
            continue
        for clause in _CLAUSES.split(sentence):
            if _NEGATED.search(clause):
                continue
            if COMPLETION_SIGNAL_RE.search(clause):
                labels.add("task_completed")
            if FAILURE_SIGNAL_RE.search(clause):
                labels.add("task_failed")
    return next(iter(labels)) if len(labels) == 1 else None
