from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class QualityResult:
    average_score: float
    category_scores: dict[str, int]
    strengths: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)


def score_business_quality(
    parsed_output: Any,
    raw_output: str,
    audience: str,
    output_destination: str,
    rules: dict[str, Any],
) -> QualityResult:
    text = _as_text(parsed_output, raw_output)
    lower = text.lower()
    category_scores = {
        "Specificity": _score_specificity(lower),
        "Actionability": _score_actionability(lower),
        "Audience fit": _score_audience_fit(lower, audience),
        "Workflow readiness": _score_workflow_readiness(parsed_output, output_destination, rules),
        "Evidence quality": _score_evidence_quality(lower),
        "Risk clarity": _score_risk_clarity(lower),
        "Formatting clarity": _score_formatting_clarity(parsed_output, raw_output),
        "Demo usefulness": _score_demo_usefulness(lower),
    }
    average = round(sum(category_scores.values()) / len(category_scores), 1)
    issues = _quality_issues(category_scores, audience, output_destination)
    strengths = [name for name, score in category_scores.items() if score >= 4]
    return QualityResult(average_score=average, category_scores=category_scores, strengths=strengths, issues=issues)


def _score_specificity(text: str) -> int:
    signals = [
        "extracted_language",
        "because",
        "prior",
        "clause",
        "fallback",
        "owner",
        "next_step",
        "renewal_date",
        "notice_deadline",
        "notice_window",
        "auto_renewal",
        "renewal date",
        "notice deadline",
        "auto-renewal",
        "evidence",
    ]
    return _bounded_score(2 + sum(signal in text for signal in signals))


def _score_actionability(text: str) -> int:
    signals = ["route", "send", "revise", "approve", "escalate", "next_step", "owner", "action", "task", "salesforce", "confirm", "log"]
    generic_penalty = any(phrase in text for phrase in ["manual review required", "needs review", "follow up.", "reach out."])
    return _bounded_score(2 + sum(signal in text for signal in signals) - (2 if generic_penalty else 0))


def _score_audience_fit(text: str, audience: str) -> int:
    audience_lower = audience.lower()
    if "legal" in audience_lower:
        signals = ["risk", "fallback", "playbook", "rationale", "clause", "escalation"]
    elif "sales" in audience_lower or "account" in audience_lower:
        signals = ["status", "next", "owner", "impact", "customer", "timeline"]
    elif "executive" in audience_lower or "leader" in audience_lower:
        signals = ["impact", "trend", "posture", "decision", "risk", "business"]
    else:
        signals = ["risk", "owner", "next", "rationale", "action"]
    return _bounded_score(2 + sum(signal in text for signal in signals))


def _score_workflow_readiness(parsed_output: Any, output_destination: str, rules: dict[str, Any]) -> int:
    if output_destination != "Workflow":
        return 4 if isinstance(parsed_output, (dict, str)) else 3
    if not isinstance(parsed_output, dict):
        return 1
    score = 2
    score += sum(field in parsed_output for field in rules["required_fields"])
    for field_name, allowed_values in rules.get("allowed_values", {}).items():
        if parsed_output.get(field_name) in allowed_values:
            score += 1
    return _bounded_score(score)


def _score_evidence_quality(text: str) -> int:
    signals = ["extracted_language", "not found in agreement", "clause", "agreement", "language", "source", "renewal terms", "written notice", "evidence"]
    return _bounded_score(1 + sum(signal in text for signal in signals))


def _score_risk_clarity(text: str) -> int:
    signals = [
        "green",
        "yellow",
        "red",
        "risk_reason",
        "material",
        "prohibited",
        "high-risk",
        "escalation",
        "renewal_status",
        "notice_window_open",
        "notice_window_missed",
        "business_impact",
        "verdict",
        "notice window open",
        "auto-renewal",
        "why it matters",
    ]
    return _bounded_score(1 + sum(signal in text for signal in signals))


def _score_formatting_clarity(parsed_output: Any, raw_output: str) -> int:
    if isinstance(parsed_output, dict):
        return 5
    if "|" in raw_output or "<table" in raw_output.lower():
        return 4
    labeled_fields = len(re.findall(r"^[A-Za-z_ ]+\s*[:=]", raw_output, flags=re.MULTILINE))
    return _bounded_score(1 + labeled_fields)


def _score_demo_usefulness(text: str) -> int:
    signals = ["recommended_action", "next_step", "rationale", "business", "customer", "agreement desk", "workflow", "dashboard", "salesforce", "renewal", "account"]
    return _bounded_score(2 + sum(signal in text for signal in signals))


def _quality_issues(scores: dict[str, int], audience: str, output_destination: str) -> list[str]:
    issues = []
    for name, score in scores.items():
        if score <= 3:
            issues.append(f"{name} is below demo-ready quality.")
    if output_destination == "Workflow" and scores["Workflow readiness"] < 4:
        issues.append("Workflow output should be strict, structured, and machine-readable.")
    if "legal" in audience.lower() and scores["Evidence quality"] < 4:
        issues.append("Legal-facing output needs stronger extracted language, fallback position, and rationale.")
    return issues


def _as_text(parsed_output: Any, raw_output: str) -> str:
    if isinstance(parsed_output, (dict, list)):
        return json.dumps(parsed_output)
    return raw_output


def _bounded_score(value: int) -> int:
    return max(1, min(5, value))
