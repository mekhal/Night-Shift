"""Reviewer verdicts: parse agent output strictly; anything unclear becomes `escalate`."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

VERDICTS = ("approve", "changes", "escalate")
RISKS = ("green", "yellow", "red")
_RISK_ORDER = {r: i for i, r in enumerate(RISKS)}


@dataclass(frozen=True)
class Verdict:
    verdict: str
    risk: str
    findings: list[dict] = field(default_factory=list)
    reason: str = ""


def _escalate(reason: str) -> Verdict:
    return Verdict("escalate", "yellow", [], reason)


def _candidates(text: str):
    """JSON objects in the text, last first: fenced ```json blocks, then any decodable {...}."""
    for block in reversed(re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)):
        yield block
    decoder = json.JSONDecoder()
    for pos in reversed([m.start() for m in re.finditer(r"\{", text)]):
        try:
            obj, _ = decoder.raw_decode(text[pos:])
        except json.JSONDecodeError:
            continue
        yield json.dumps(obj)


def parse_verdict(text: str) -> Verdict:
    for candidate in _candidates(text):
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "verdict" in obj:
            break
    else:
        return _escalate("invalid reviewer output: no JSON verdict found")

    if obj.get("verdict") not in VERDICTS:
        return _escalate(f"invalid reviewer output: verdict {obj.get('verdict')!r}")
    if obj.get("risk") not in RISKS:
        return _escalate(f"invalid reviewer output: risk {obj.get('risk')!r}")
    if not isinstance(obj.get("findings"), list) or not all(isinstance(f, dict) for f in obj["findings"]):
        return _escalate("invalid reviewer output: findings must be a list of objects")

    v = Verdict(obj["verdict"], obj["risk"], obj["findings"], str(obj.get("reason", "")))
    if v.verdict == "approve" and v.risk == "red":
        return _escalate(f"reviewer rated the change red: {v.reason}")
    return v


def combine(verdicts: dict[str, Verdict]) -> Verdict:
    """Joint review: both must agree. Any escalate, or a disagreement, escalates."""
    if len(verdicts) == 1:
        return next(iter(verdicts.values()))
    kinds = {v.verdict for v in verdicts.values()}
    risk = max((v.risk for v in verdicts.values()), key=_RISK_ORDER.__getitem__)
    reason = "; ".join(f"{name}: {v.reason}" for name, v in verdicts.items())
    if "escalate" in kinds:
        return Verdict("escalate", risk, [], reason)
    if len(kinds) > 1:
        summary = ", ".join(f"{name}={v.verdict}" for name, v in verdicts.items())
        return Verdict("escalate", risk, [], f"reviewers disagree ({summary}); {reason}")
    findings = [dict(f, reviewer=name) for name, v in verdicts.items() for f in v.findings]
    return Verdict(kinds.pop(), risk, findings, reason)
