from __future__ import annotations

from typing import Any


ISSUE_FIXES = {
    "missing_required_field": "Add a stricter output schema and state that every required field must be returned.",
    "blank_required_field": "Tell the agent to use the correct missing-data value for the destination: schema-safe sentinels for structured fields and readable phrases for prose.",
    "unsupported_allowed_value": "List the allowed values and prohibit any other labels.",
    "missing_required_array": "Require unfavorable_terms to be a non-empty array when triage_result is YELLOW or RED.",
    "invalid_array": "Show unfavorable_terms as an array in the example output.",
    "invalid_object": "Show nested fields as structured objects instead of prose.",
    "missing_object_field": "Require every nested object field and use the correct schema-safe or prose missing-data value when unavailable.",
    "invalid_array_item": "Show risk or term arrays as arrays of structured objects.",
    "missing_array_item_field": "Require every risk item to include extracted language, risk reason, business impact, mitigation, and owner.",
    "missing_unfavorable_term_field": "Require extracted language, risk reason, playbook position, suggested fallback, and escalation owner for every flagged term.",
    "missing_recommended_action_field": "Require recommended_action to include action, owner, rationale, and next_step.",
    "generic_recommended_action": "Prohibit generic actions unless they include rationale, owner, business impact, and a next step.",
    "hallucination_risk": "Require source support for every legal or commercial claim; use agreement text when available, metadata/source references when raw text is unavailable, or the correct missing-data value.",
    "missing_required_column": "Use exact schema column names when table output is selected.",
    "missing_plain_text_label": "Use explicit labels for each required field or switch to JSON for stricter validation.",
    "invalid_json": "Ask for strict JSON only, without markdown fences or explanatory prose.",
    "machine_labels_in_customer_output": "For customer-facing output, prohibit underscore field names and require readable labels like `Agreement name` and `Renewal status`.",
    "verbose_customer_output": "Constrain customer-facing output to a concise demo brief with the answer first, source-backed findings, compact formatting, and a clear next action.",
}


def recommend_prompt_improvements(structural_issues: list[Any], quality_issues: list[str], audience: str, output_destination: str) -> list[str]:
    recommendations: list[str] = []
    seen = set()
    for issue in structural_issues:
        code = getattr(issue, "code", "")
        fix = ISSUE_FIXES.get(code, getattr(issue, "fix_hint", "Clarify this requirement in the prompt."))
        if fix not in seen:
            recommendations.append(fix)
            seen.add(fix)

    for quality_issue in quality_issues:
        if "Audience fit" in quality_issue:
            recommendations.append(f"Rewrite the instructions for a {audience} audience with language they can act on.")
        elif "Workflow" in quality_issue or output_destination == "Workflow":
            recommendations.append("Convert the output requirement to strict JSON with exact field names and allowed values.")
        elif "Evidence" in quality_issue:
            recommendations.append("Require direct extracted agreement language for each risk finding.")
        elif "Actionability" in quality_issue:
            recommendations.append("Require a concrete owner, action, rationale, business impact, and next step.")
        else:
            recommendations.append("Add a stronger example output that demonstrates the expected level of detail.")

    if not recommendations:
        recommendations.append("Keep the prompt as-is; the sample output is structurally complete and demo-ready.")
    return _dedupe(recommendations)


def build_optimized_prompt(base_prompt: str, recommendations: list[str], output_destination: str) -> str:
    reinforcement = ["Optimization reinforcement:"]
    if output_destination == "Workflow":
        reinforcement.extend(
            [
                "- Return every required field exactly as named in the schema.",
                "- Do not invent contract terms or imply facts that are not supported by extracted agreement language.",
                "- For structured fields, use schema-safe missing values such as `NOT_FOUND`, `Unknown`, or `null` only when allowed by the schema; do not put prose missing phrases into enum fields.",
                "- Make recommendations specific, owned, and actionable.",
            ]
        )
        reinforcement.append("- Because this output is for Workflow, return strict JSON with exact field names and allowed values only.")
    else:
        reinforcement.extend(
            [
                "- Keep customer-facing output concise and demo-ready.",
                "- Do not expose machine field names or underscores.",
                "- Derive findings from source evidence first, then write the answer-first summary.",
                "- Use compact tables only where they render clearly; otherwise use labeled bullets.",
                "- Do not invent contract terms or imply facts that are not supported by agreement language, metadata, or source references.",
            ]
        )
    for recommendation in recommendations[:6]:
        reinforcement.append(f"- {recommendation}")
    return f"{base_prompt.rstrip()}\n\n" + "\n".join(reinforcement)


def _dedupe(items: list[str]) -> list[str]:
    seen = set()
    deduped = []
    for item in items:
        if item not in seen:
            deduped.append(item)
            seen.add(item)
    return deduped
