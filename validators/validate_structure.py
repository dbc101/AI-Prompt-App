from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any


@dataclass
class StructuralIssue:
    severity: str
    field: str
    message: str
    fix_hint: str
    code: str


@dataclass
class StructuralResult:
    passed: bool
    parsed_output: Any
    format_errors: list[str] = field(default_factory=list)
    issues: list[StructuralIssue] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._current_row: list[str] | None = None
        self._current_cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._current_row = []
        if tag in {"td", "th"}:
            self._current_cell = []

    def handle_data(self, data: str) -> None:
        if self._current_cell is not None:
            self._current_cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._current_row is not None and self._current_cell is not None:
            self._current_row.append(" ".join("".join(self._current_cell).split()))
            self._current_cell = None
        if tag == "tr" and self._current_row is not None:
            if self._current_row:
                self.rows.append(self._current_row)
            self._current_row = None


def validate_output(raw_output: str, output_format: str, rules: dict[str, Any]) -> StructuralResult:
    result = StructuralResult(passed=False, parsed_output=None)
    parsed = parse_output(raw_output, output_format, result)
    result.parsed_output = parsed

    if output_format == "JSON" and result.format_errors:
        result.issues.append(
            StructuralIssue(
                severity="error",
                field="format",
                message="Output is not valid JSON.",
                fix_hint="Select JSON only when the agent returns strict JSON with quoted field names and no surrounding prose.",
                code="invalid_json",
            )
        )
        result.passed = False
        return result

    if output_format in {"Plain Language Summary", "Markdown Table", "HTML Table"}:
        _check_customer_facing_labels(raw_output, rules, result)
        _check_customer_output_limits(raw_output, rules, result)

    if isinstance(parsed, dict):
        _validate_object(parsed, rules, result)
    elif output_format in {"Markdown Table", "HTML Table"}:
        _validate_table(parsed, rules, result, output_format)
    else:
        _validate_plain_text(raw_output, rules, result)

    result.passed = not any(issue.severity == "error" for issue in result.issues)
    return result


def parse_output(raw_output: str, output_format: str, result: StructuralResult) -> Any:
    raw_output = raw_output.strip()
    if not raw_output:
        result.format_errors.append("No output was provided.")
        return {}

    if output_format == "JSON":
        try:
            return json.loads(raw_output)
        except json.JSONDecodeError as exc:
            result.format_errors.append(str(exc))
            return {}

    if output_format == "Markdown Table":
        return _parse_markdown_table(raw_output)

    if output_format == "HTML Table":
        parser = _TableParser()
        parser.feed(raw_output)
        return _rows_to_table(parser.rows)

    if output_format == "Workflow Variables":
        return _parse_workflow_variables(raw_output)

    return raw_output


def _parse_markdown_table(raw_output: str) -> dict[str, Any]:
    rows = []
    for line in raw_output.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or not stripped.endswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if all(re.fullmatch(r":?-{3,}:?", cell or "") for cell in cells):
            continue
        rows.append(cells)
    return _rows_to_table(rows)


def _rows_to_table(rows: list[list[str]]) -> dict[str, Any]:
    if not rows:
        return {"columns": [], "rows": []}
    columns = [_normalize_field_name(value) for value in rows[0]]
    table_rows = []
    for row in rows[1:]:
        table_rows.append({columns[index]: value for index, value in enumerate(row[: len(columns)])})
    return {"columns": columns, "rows": table_rows}


def _parse_workflow_variables(raw_output: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in raw_output.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
        elif "=" in line:
            key, value = line.split("=", 1)
        else:
            continue
        values[_normalize_field_name(key)] = value.strip().strip('"')
    return values


def _validate_object(output: dict[str, Any], rules: dict[str, Any], result: StructuralResult) -> None:
    required_fields = rules["required_fields"]
    for field_name in required_fields:
        if field_name not in output:
            _add_issue(result, "error", field_name, f"Missing required field `{field_name}`.", "Add the field and always return it.", "missing_required_field")
            continue
        if _is_blank(output[field_name]) and not _field_can_be_empty(field_name, output, rules):
            _add_issue(result, "error", field_name, f"`{field_name}` is blank.", "Return a value or use the required missing-information phrase.", "blank_required_field")
        else:
            result.checks.append(f"{field_name} is present.")

    for field_name, allowed_values in rules.get("allowed_values", {}).items():
        value = output.get(field_name)
        if value is not None and value not in allowed_values:
            _add_issue(
                result,
                "error",
                field_name,
                f"`{field_name}` uses unsupported value `{value}`.",
                f"Use one of: {', '.join(allowed_values)}.",
                "unsupported_allowed_value",
            )

    for object_name, child_fields in rules.get("object_required_fields", {}).items():
        value = output.get(object_name)
        if value is None:
            continue
        if not isinstance(value, dict):
            _add_issue(result, "error", object_name, f"`{object_name}` must be an object.", "Return child fields under a structured object.", "invalid_object")
            continue
        for child_field in child_fields:
            child_value = value.get(child_field)
            if _is_blank(child_value):
                _add_issue(
                    result,
                    "error",
                    f"{object_name}.{child_field}",
                    f"`{object_name}.{child_field}` is missing or blank.",
                    "Return every required child field or use the missing-information phrase.",
                    "missing_object_field",
                )

    for array_name, array_rules in rules.get("array_fields", {}).items():
        value = output.get(array_name)
        if _array_is_required(array_rules, output):
            if not isinstance(value, list) or not value:
                _add_issue(
                    result,
                    "error",
                    array_name,
                    f"`{array_name}` must be a non-empty array for this status.",
                    "Return at least one array item with evidence, rationale, impact, mitigation, and owner.",
                    "missing_required_array",
                )
                continue
        elif value is not None and not isinstance(value, list):
            _add_issue(result, "error", array_name, f"`{array_name}` must be an array.", "Return an array, even when it is empty.", "invalid_array")
            continue

        if isinstance(value, list):
            for index, item in enumerate(value):
                _validate_array_item(array_name, item, index, rules, result)

    if "unfavorable_terms" in output:
        _validate_agreement_desk_triage(output, rules, result)

    recommended_action = output.get("recommended_action")
    if not isinstance(recommended_action, dict):
        _add_issue(result, "error", "recommended_action", "`recommended_action` must be an object.", "Return action, owner, rationale, and next_step as child fields.", "invalid_recommended_action")
    else:
        for field_name in rules.get("recommended_action_required_fields", []):
            value = recommended_action.get(field_name)
            if _is_blank(value):
                _add_issue(result, "error", f"recommended_action.{field_name}", f"`recommended_action.{field_name}` is missing or blank.", "Make the recommended action specific and complete.", "missing_recommended_action_field")
        _check_generic_recommended_action(recommended_action, rules, result)

    _check_hallucination_signals(output, rules, result)


def _validate_agreement_desk_triage(output: dict[str, Any], rules: dict[str, Any], result: StructuralResult) -> None:
    unfavorable_terms = output.get("unfavorable_terms")
    triage_result = output.get("triage_result")
    if triage_result in {"YELLOW", "RED"}:
        if not isinstance(unfavorable_terms, list) or not unfavorable_terms:
            _add_issue(
                result,
                "error",
                "unfavorable_terms",
                "`unfavorable_terms` must be a non-empty array when triage_result is YELLOW or RED.",
                "Require at least one unfavorable term with evidence and fallback guidance.",
                "missing_required_array",
            )
    elif unfavorable_terms is not None and not isinstance(unfavorable_terms, list):
        _add_issue(result, "error", "unfavorable_terms", "`unfavorable_terms` must be an array.", "Return an array, even when it is empty.", "invalid_array")


def _validate_unfavorable_term(term: Any, index: int, rules: dict[str, Any], result: StructuralResult) -> None:
    path = f"unfavorable_terms[{index}]"
    if not isinstance(term, dict):
        _add_issue(result, "error", path, "Each unfavorable term must be an object.", "Return structured term objects, not plain strings.", "invalid_unfavorable_term")
        return

    for field_name in rules["unfavorable_term_required_fields"]:
        value = term.get(field_name)
        if _is_blank(value):
            _add_issue(result, "error", f"{path}.{field_name}", f"`{field_name}` is missing or blank.", "Include extracted language, risk rationale, playbook position, fallback, and owner for every term.", "missing_unfavorable_term_field")

    extracted = str(term.get("extracted_language", "")).strip()
    missing_phrase = rules["missing_information_phrase"]
    if extracted and extracted.lower() not in {missing_phrase.lower(), "not found"} and len(extracted.split()) < 4:
        _add_issue(result, "warning", f"{path}.extracted_language", "`extracted_language` is too thin to be useful evidence.", "Use direct agreement language where available.", "weak_extracted_language")


def _validate_array_item(array_name: str, item: Any, index: int, rules: dict[str, Any], result: StructuralResult) -> None:
    if array_name == "unfavorable_terms":
        _validate_unfavorable_term(item, index, rules, result)
        return

    path = f"{array_name}[{index}]"
    if not isinstance(item, dict):
        _add_issue(result, "error", path, f"Each `{array_name}` item must be an object.", "Return structured array items, not plain strings.", "invalid_array_item")
        return

    for field_name in rules.get("array_item_required_fields", {}).get(array_name, []):
        value = item.get(field_name)
        if _is_blank(value):
            _add_issue(
                result,
                "error",
                f"{path}.{field_name}",
                f"`{field_name}` is missing or blank.",
                "Include every required field for each risk item.",
                "missing_array_item_field",
            )

    extracted = str(item.get("extracted_language", "")).strip()
    missing_phrase = rules.get("missing_information_phrase", "Not found in agreement")
    if extracted and extracted.lower() not in {missing_phrase.lower(), "not found"} and len(extracted.split()) < 4:
        _add_issue(result, "warning", f"{path}.extracted_language", "`extracted_language` is too thin to be useful evidence.", "Use direct agreement language where available.", "weak_extracted_language")


def _validate_table(parsed: dict[str, Any], rules: dict[str, Any], result: StructuralResult, output_format: str) -> None:
    columns = set(parsed.get("columns", []))
    rows = parsed.get("rows", [])
    if not columns:
        _add_issue(result, "error", "format", f"No {output_format.lower()} columns were found.", "Return a table with the required schema columns.", "missing_table")
        return

    for field_name in rules["required_fields"]:
        if field_name not in columns:
            _add_issue(result, "error", field_name, f"Missing required table column `{field_name}`.", "Add exact column names from the expected output schema.", "missing_required_column")
        else:
            result.checks.append(f"{field_name} column is present.")

    if not rows:
        _add_issue(result, "error", "format", "The table has no data rows.", "Return at least one row with the triage output.", "empty_table")
        return

    first_row = rows[0]
    for field_name, allowed_values in rules.get("allowed_values", {}).items():
        value = first_row.get(field_name)
        if value and value not in allowed_values:
            _add_issue(result, "error", field_name, f"`{field_name}` uses unsupported value `{value}`.", f"Use one of: {', '.join(allowed_values)}.", "unsupported_allowed_value")


def _validate_plain_text(raw_output: str, rules: dict[str, Any], result: StructuralResult) -> None:
    normalized = raw_output.lower()
    required_fields = rules.get("customer_facing_required_fields", rules["required_fields"])
    for field_name in required_fields:
        if not _field_label_present(raw_output, field_name):
            _add_issue(result, "error", field_name, f"Plain text output does not clearly label `{field_name}`.", "Label each required field explicitly or switch to JSON/table output.", "missing_plain_text_label")
        else:
            result.checks.append(f"{field_name} label appears in the text.")

    for unsupported in ["manual review"]:
        if re.search(rf"triage(?:\s+result)?\s*[:=-]\s*{unsupported}", normalized):
            _add_issue(result, "error", "triage_result", "`Manual Review` is not an allowed triage_result.", "Use GREEN, YELLOW, or RED.", "unsupported_allowed_value")


def _check_customer_facing_labels(raw_output: str, rules: dict[str, Any], result: StructuralResult) -> None:
    fields = set(rules.get("required_fields", []))
    fields.update(rules.get("recommended_action_required_fields", []))
    for child_fields in rules.get("object_required_fields", {}).values():
        fields.update(child_fields)
    for child_fields in rules.get("array_item_required_fields", {}).values():
        fields.update(child_fields)

    bad_labels = []
    for field_name in sorted(field for field in fields if "_" in field):
        if re.search(rf"(?<![A-Za-z0-9]){re.escape(field_name)}(?![A-Za-z0-9])", raw_output):
            bad_labels.append(field_name)

    if bad_labels:
        examples = ", ".join(f"`{label}`" for label in bad_labels[:5])
        _add_issue(
            result,
            "error",
            "customer_facing_labels",
            f"Customer-facing output exposes machine field names with underscores: {examples}.",
            "Use readable labels such as `Agreement name`, `Renewal status`, and `Recommended action` for Chat, plain-language, markdown, and HTML outputs.",
            "machine_labels_in_customer_output",
        )


def _check_customer_output_limits(raw_output: str, rules: dict[str, Any], result: StructuralResult) -> None:
    limits = rules.get("customer_output_limits", {})
    if not limits:
        return

    word_count = len(re.findall(r"\b[\w'-]+\b", raw_output))
    heading_count = len(re.findall(r"^#{1,4}\s+", raw_output, flags=re.MULTILINE))
    bullet_count = len(re.findall(r"^\s*[-*]\s+", raw_output, flags=re.MULTILINE))
    table_rows = [
        line
        for line in raw_output.splitlines()
        if line.strip().startswith("|")
        and line.strip().endswith("|")
        and not re.fullmatch(r"\|\s*:?-{3,}:?\s*\|\s*:?-{3,}:?\s*\|", line.strip())
    ]
    data_row_count = max(0, len(table_rows) - 1)

    if word_count > limits.get("max_words", 10_000):
        _add_issue(
            result,
            "error",
            "demo_concision",
            f"Customer-facing output is too long for a live demo ({word_count} words).",
            f"Keep the response under {limits['max_words']} words and summarize into a brief.",
            "verbose_customer_output",
        )
    if heading_count > limits.get("max_headings", 10_000):
        _add_issue(
            result,
            "error",
            "demo_concision",
            f"Customer-facing output has too many sections ({heading_count} headings).",
            f"Use no more than {limits['max_headings']} headings.",
            "verbose_customer_output",
        )
    if data_row_count > limits.get("max_table_rows", 10_000):
        _add_issue(
            result,
            "error",
            "demo_concision",
            f"Customer-facing output has too many table rows ({data_row_count}).",
            f"Use no more than {limits['max_table_rows']} table rows.",
            "verbose_customer_output",
        )
    if bullet_count > limits.get("max_bullets", 10_000):
        _add_issue(
            result,
            "error",
            "demo_concision",
            f"Customer-facing output has too many bullets ({bullet_count}).",
            f"Use no more than {limits['max_bullets']} bullets.",
            "verbose_customer_output",
        )


def _field_label_present(raw_output: str, field_name: str) -> bool:
    words = [re.escape(word) for word in field_name.split("_")]
    readable_pattern = r"(?<![A-Za-z0-9])" + r"[\s-]+".join(words) + r"(?![A-Za-z0-9])"
    machine_pattern = rf"(?<![A-Za-z0-9]){re.escape(field_name)}(?![A-Za-z0-9])"
    return bool(re.search(readable_pattern, raw_output, flags=re.IGNORECASE) or re.search(machine_pattern, raw_output, flags=re.IGNORECASE))


def _check_generic_recommended_action(recommended_action: dict[str, Any], rules: dict[str, Any], result: StructuralResult) -> None:
    combined = " ".join(str(recommended_action.get(field, "")) for field in ["action", "next_step"]).lower()
    for phrase in rules.get("generic_action_phrases", []):
        if phrase in combined:
            _add_issue(
                result,
                "error",
                "recommended_action",
                f"`recommended_action` uses generic phrasing: `{phrase}`.",
                "Pair the recommendation with an owner, rationale, business impact, and concrete next step.",
                "generic_recommended_action",
            )
            return


def _check_hallucination_signals(output: dict[str, Any], rules: dict[str, Any], result: StructuralResult) -> None:
    missing_phrase = rules["missing_information_phrase"].lower()
    watch_terms = rules.get("hallucination_watch_terms", [])
    full_text = json.dumps(output).lower()
    evidence_array_names = rules.get("evidence_array_fields", ["unfavorable_terms"])
    evidence_items = []
    for array_name in evidence_array_names:
        value = output.get(array_name)
        if isinstance(value, list):
            evidence_items.extend(value)

    if not evidence_items:
        for term in watch_terms:
            if term in full_text:
                _add_issue(
                    result,
                    "error",
                    "source_evidence",
                    f"Potential unsourced `{term}` term was referenced without an unfavorable_terms evidence object.",
                    f"Use extracted agreement language or `{rules['missing_information_phrase']}` instead of implying source terms.",
                    "hallucination_risk",
                )
                break
        return

    evidenced_terms = []
    for index, item in enumerate(evidence_items):
        if not isinstance(item, dict):
            continue
        extracted = str(item.get("extracted_language", "")).strip().lower()
        evidence_name = " ".join(str(item.get(key, "")) for key in ["term_name", "risk_type"]).lower()
        evidenced_terms.append(evidence_name)
        if any(watch in evidence_name for watch in watch_terms) and extracted and missing_phrase not in extracted and len(extracted.split()) < 4:
            _add_issue(
                result,
                "warning",
                f"evidence[{index}].extracted_language",
                "A watched commercial/legal term has weak source evidence.",
                "Require direct extracted language or the missing-information phrase.",
                "hallucination_risk",
            )

    non_schema_text = " ".join(
        json.dumps(value).lower()
        for key, value in output.items()
        if key not in set(rules["required_fields"])
    )
    for term in watch_terms:
        if term in non_schema_text and not any(term in evidenced_term for evidenced_term in evidenced_terms):
            _add_issue(
                result,
                "error",
                "source_evidence",
                f"Potential unsourced `{term}` term was referenced without a matching unfavorable_terms evidence object.",
                f"Add an unfavorable term with extracted language, or use `{rules['missing_information_phrase']}` when the source is missing.",
                "hallucination_risk",
            )
            break


def _field_can_be_empty(field_name: str, output: dict[str, Any], rules: dict[str, Any]) -> bool:
    field_rules = rules.get("array_fields", {}).get(field_name, {})
    allowed_empty_when = field_rules.get("allowed_empty_when", {})
    for controlling_field, values in allowed_empty_when.items():
        if output.get(controlling_field) in values:
            return True
    return False


def _array_is_required(array_rules: dict[str, Any], output: dict[str, Any]) -> bool:
    required_when = array_rules.get("required_when", {})
    for controlling_field, values in required_when.items():
        if output.get(controlling_field) in values:
            return True
    return False


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, list):
        return len(value) == 0
    if isinstance(value, dict):
        return len(value) == 0
    return False


def _normalize_field_name(value: str) -> str:
    value = re.sub(r"<[^>]+>", "", value)
    value = re.sub(r"[^A-Za-z0-9]+", "_", value.strip()).strip("_").lower()
    return value


def _add_issue(result: StructuralResult, severity: str, field: str, message: str, fix_hint: str, code: str) -> None:
    result.issues.append(StructuralIssue(severity=severity, field=field, message=message, fix_hint=fix_hint, code=code))
