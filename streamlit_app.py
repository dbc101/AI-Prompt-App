from __future__ import annotations

import csv
import json
import os
import re
import sys
import zipfile
from datetime import datetime
from html import escape
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

import streamlit as st
import streamlit.components.v1 as components
import yaml

APP_DIR = Path(__file__).resolve().parent
REPO_DIR = APP_DIR.parent if APP_DIR.name == "app" else APP_DIR

for import_path in (APP_DIR, REPO_DIR):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))

try:
    from validators.optimize_prompt import build_optimized_prompt, recommend_prompt_improvements
    from validators.validate_quality import score_business_quality
    from validators.validate_structure import validate_output
except ModuleNotFoundError as exc:
    deployment_modules = {
        "validators",
        "validators.optimize_prompt",
        "validators.validate_quality",
        "validators.validate_structure",
    }
    if exc.name not in deployment_modules:
        raise
    from optimize_prompt import build_optimized_prompt, recommend_prompt_improvements
    from validate_quality import score_business_quality
    from validate_structure import validate_output


ALLOWED_OUTPUT_DESTINATIONS = [
    "Chat",
    "Agent Studio",
    "Workflow",
]

ALLOWED_OUTPUT_FORMATS = [
    "JSON",
    "Markdown Table",
    "HTML Table",
    "Plain Language Summary",
    "Workflow Variables",
]

def _first_existing_path(*paths: Path) -> Path:
    for path in paths:
        if path.exists():
            return path
    return paths[0]


def _resolve_repo_file(path_or_name: str | Path) -> Path:
    path = Path(path_or_name)
    if path.is_absolute():
        return path

    candidates = [
        REPO_DIR / path,
        APP_DIR / path,
        REPO_DIR / path.name,
        APP_DIR / path.name,
    ]
    return _first_existing_path(*candidates)


TEMPLATE_PATH = _first_existing_path(
    REPO_DIR / "prompts" / "prompt_templates.yaml",
    REPO_DIR / "prompt_templates.yaml",
    APP_DIR / "prompt_templates.yaml",
)
PROMPT_LIBRARY_PATH = _first_existing_path(
    REPO_DIR / "data" / "agent_worksheet_prompts.xlsx",
    REPO_DIR / "agent_worksheet_prompts.xlsx",
    APP_DIR / "agent_worksheet_prompts.xlsx",
)
RULES_DIR = _first_existing_path(REPO_DIR / "rules", REPO_DIR, APP_DIR)
GEMINI_CHAT_URL = "https://gemini.google.com/app"
AGENT_STUDIO_URL = "https://apps-d.docusign.com/send/agents"

MODE_USE_LIBRARY = "Use Prompt Library"
MODE_CUSTOMIZE = "Customize Existing Prompt"
MODE_GEMINI = "Create New"

PROMPT_DESTINATION_AI_CHAT = "AI Chat"
PROMPT_DESTINATION_AGENT_STUDIO = "Agent Studio"

GENERATION_MODES = [
    MODE_USE_LIBRARY,
    MODE_CUSTOMIZE,
    MODE_GEMINI,
]

LEGACY_GENERATION_MODE_ALIASES = {
    "Customize existing prompt": MODE_CUSTOMIZE,
    "Generate with Gemini Agent": MODE_GEMINI,
    "Generate Gemini Brief": MODE_GEMINI,
}

DEFAULT_LIBRARY_TITLE_HINT = "Renewal Leverage Agent"

PROMPT_FIELD_OVERRIDES = {
    "rename ad request": ["request_type", "counterparty", "renamed_request_title"],
    "conformed agreements": ["counterparty", "agreement_set_summary", "prevailing_terms", "conflicts", "recommended_action"],
    "sales oppty readiness check": ["readiness_status", "missing_information", "risk_flags", "recommended_action"],
    "request triage + risk score + reviewer routing": ["contract_type", "paper_type", "triage_result", "flagged_playbook_terms", "next_review_queue"],
    "renewal leverage agent": ["contract_title", "counterparty", "renewal_period", "notice_period", "renegotiation_terms", "recommended_action"],
    "whitespace agent": ["account_name", "active_agreements", "whitespace_opportunities", "recommended_action"],
    "ip risk mitigation assistant": ["clause", "ip_risk", "mitigation", "owner", "recommended_action"],
    "agreement renewal risk radar": ["account_name", "renewal_risk", "notice_deadline", "commercial_leverage", "recommended_action"],
    "contract business value agent": ["business_value", "agreement_evidence", "stakeholder_impact", "recommended_action"],
    "vendor msa renewal agent": ["vendor", "renewal_date", "notice_period", "risk_terms", "recommended_action"],
    "civil law conversion agent": ["clause", "common_law_position", "civil_law_revision", "rationale"],
}


def main() -> None:
    st.set_page_config(page_title="Agent Prompt Builder", layout="wide")
    _inject_styles()

    templates = _load_yaml(TEMPLATE_PATH)["templates"]
    template_id = _first_active_template_id(templates)
    selected_template = templates[template_id]
    rules = _load_rules(template_id, selected_template)
    bundled_prompt_library = _load_default_prompt_library()

    with st.sidebar:
        _render_sidebar_brand()
        prompt_library = _render_prompt_library_admin(bundled_prompt_library)

    _render_app_header()
    _render_workflow_guide()

    context = _context_form(selected_template, rules, prompt_library)
    _render_section_break()

    if selected_template["status"] != "active":
        st.info("Select a supported prompt-library pattern to generate and check a complete MVP prompt.")
        return

    missing_fields = _missing_prompt_fields(context)
    generated_prompt = "" if missing_fields else _build_generated_artifact(context, selected_template, rules)

    with st.sidebar:
        _render_prompt_history_sidebar(context, generated_prompt, missing_fields)

    tab_labels = ["Generated Prompt"]
    show_agreement_builder = _supports_agreement_builder(context)
    if show_agreement_builder:
        tab_labels.append("Agreement Builder")
    tab_labels.append("Fine-Tune Guardrails")
    tabs = st.tabs(tab_labels)

    with tabs[0]:
        _render_prompt_builder(context, template_id, generated_prompt, missing_fields, rules)

    next_tab_index = 1
    if show_agreement_builder:
        with tabs[next_tab_index]:
            _render_agreement_pack_builder(context, template_id, missing_fields, rules)
        next_tab_index += 1

    with tabs[next_tab_index]:
        _guardrail_tuning_tab(template_id, context, generated_prompt, rules, missing_fields)


def _render_prompt_builder(
    context: dict[str, Any],
    template_id: str,
    generated_prompt: str,
    missing_fields: list[str],
    rules: dict[str, Any],
) -> None:
    _render_anchor("prompt-builder")
    _render_section_intro(
        "Output Generation",
        "Generate a copy-ready AI Chat prompt, Agent Studio prompt, or Agent Studio brief from the intake details above.",
    )
    generation_mode = _normalize_generation_mode(context["generation_mode"])
    _render_agent_studio_configuration(context)
    action_label = "Generate Agent Studio Brief" if generation_mode == MODE_GEMINI else "Generate Optimized Prompt"
    st.button(action_label, type="primary", disabled=bool(missing_fields))

    if generation_mode == MODE_GEMINI:
        st.subheader("Generated Agent Studio Brief")
        _render_agent_studio_mode_steps()
        if missing_fields:
            st.info(f"Complete {', '.join(missing_fields)} to generate the Agent Studio brief.")
        st.text_area("Copy-ready Agent Studio brief", value=generated_prompt, height=400, label_visibility="collapsed")
        _render_agent_studio_actions(generated_prompt, disabled=bool(missing_fields))
    else:
        st.subheader("Generated Optimized AI Agent Prompt")
        if missing_fields:
            st.info(f"Complete {', '.join(missing_fields)} to generate the optimized prompt.")
        st.text_area("Copy-ready optimized prompt", value=generated_prompt, height=420, label_visibility="collapsed")
        _render_prompt_copy_action(generated_prompt, disabled=bool(missing_fields))

    with st.expander("Output rules", expanded=False):
        _render_output_rules(rules, context)


def _render_agent_studio_configuration(context: dict[str, Any]) -> None:
    if not _uses_agent_studio_destination(context):
        return

    st.markdown("### Agent Studio Configuration")
    st.caption("Review the workflow fields inferred from the prompt context. Edits here update the generated prompt after the app refreshes.")
    left, right = st.columns(2)
    with left:
        st.text_area(
            "Data Outputs",
            height=150,
            key="workflow_data_outputs",
            help="Structured values Agent Studio can use when the agent is published or used in a workflow.",
        )
    with right:
        st.text_area(
            "Workflow Routing Logic",
            height=150,
            key="workflow_decision_logic",
            help="Decision rules Agent Studio should preserve when routing, prioritizing, or returning structured output.",
        )


def _render_agreement_pack_builder(
    context: dict[str, Any],
    template_id: str,
    missing_fields: list[str],
    rules: dict[str, Any],
) -> None:
    agreement_pack_brief = "" if missing_fields else _build_demo_agreement_pack_brief(context, rules)

    _render_anchor("agreement-builder")
    _render_section_intro(
        "Agreement Builder",
        "Create a Gemini-ready brief for downloadable mock signed agreement PDFs that support the selected demo story.",
    )
    st.button("Generate Agreement PDF Brief", type="primary", disabled=bool(missing_fields))
    st.subheader("Generated Agreement PDF Brief")
    if missing_fields:
        st.caption(f"Complete {', '.join(missing_fields)} to generate the Agreement PDF Brief.")
    st.text_area("Copy-ready Agreement PDF Brief", value=agreement_pack_brief, height=260, label_visibility="collapsed")
    _render_agreement_pack_actions(agreement_pack_brief, disabled=bool(missing_fields))


def _supports_agreement_builder(context: dict[str, Any]) -> bool:
    generation_mode = _normalize_generation_mode(context.get("generation_mode", MODE_USE_LIBRARY))
    if generation_mode in {MODE_CUSTOMIZE, MODE_GEMINI}:
        return True

    combined = " ".join(
        [
            context.get("library_title", ""),
            context.get("library_category", ""),
            context.get("library_description", ""),
            context.get("library_prompt", ""),
        ]
    ).lower()
    excluded_titles = ["rename ad request", "whitespace agent", "sales oppty readiness check"]
    if any(title in combined for title in excluded_titles):
        return False
    agreement_terms = [
        "agreement",
        "contract",
        "msa",
        "sow",
        "order form",
        "clause",
        "renewal",
        "vendor",
        "legal",
        "conformed",
        "ip risk",
        "civil law",
    ]
    return any(term in combined for term in agreement_terms)


def _render_app_header() -> None:
    st.markdown(
        """
        <div class="ds-hero">
            <div class="ds-brand-row">
                <span class="ds-pill">Prompt Builder</span>
            </div>
            <h1>Agent Prompt Builder</h1>
            <p>Generate and Optimize Agent Prompts</p>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_workflow_guide() -> None:
    st.markdown(
        """
        <div class="ds-workflow" aria-label="Demo prep workflow">
            <a class="ds-workflow-step" href="#prompt-destination">
                <span>1</span>
                <div>
                    <strong>Prompt Destination</strong>
                    <small>Choose AI Chat or Agent Studio</small>
                </div>
            </a>
            <a class="ds-workflow-step" href="#prompt-mode">
                <span>2</span>
                <div>
                    <strong>Prompt Mode</strong>
                    <small>Start from library, customize, or create new</small>
                </div>
            </a>
            <a class="ds-workflow-step" href="#customize-prompt">
                <span>3</span>
                <div>
                    <strong>Demo Details</strong>
                    <small>Add customer context and objective</small>
                </div>
            </a>
            <a class="ds-workflow-step" href="#prompt-builder">
                <span>4</span>
                <div>
                    <strong>Generate + Fine-Tune</strong>
                    <small>Copy prompt, build data, adjust guardrails</small>
                </div>
            </a>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_section_break() -> None:
    st.markdown('<div class="ds-section-break"></div>', unsafe_allow_html=True)


def _render_section_intro(title: str, body: str) -> None:
    st.markdown(
        f"""
        <div class="ds-section-intro">
            <div>{escape(title)}</div>
            <p>{escape(body)}</p>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_form_section_label(title: str, size: str = "secondary") -> None:
    class_name = f"ds-form-section-label ds-form-section-label--{escape(size)}"
    st.markdown(f'<div class="{class_name}">{escape(title)}</div>', unsafe_allow_html=True)


def _render_anchor(anchor_id: str) -> None:
    st.markdown(f'<div id="{escape(anchor_id)}" class="ds-anchor"></div>', unsafe_allow_html=True)


def _render_generation_mode_helper(generation_mode: str) -> None:
    mode_guidance = {
        MODE_USE_LIBRARY: "",
        MODE_CUSTOMIZE: "Adapt an existing prompt for a new customer or use case.",
        MODE_GEMINI: "Create an Agent Studio brief for building a net-new agent prompt.",
    }
    body = mode_guidance.get(_normalize_generation_mode(generation_mode), mode_guidance[MODE_USE_LIBRARY])
    if not body:
        return
    st.markdown(
        f"""
        <div class="ds-mode-helper">
            {escape(body)}
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_sidebar_brand() -> None:
    st.markdown(
        """
        <div class="ds-sidebar-brand">
            <div class="ds-sidebar-mark">D</div>
            <div>
                <div class="ds-sidebar-title">Docusign IAM</div>
                <div class="ds-sidebar-subtitle">SC demo workspace</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_prompt_history_sidebar(context: dict[str, Any], generated_prompt: str, missing_fields: list[str]) -> None:
    st.header("Prompt History")
    st.caption("Saved prompts from this session.")

    st.button("New Prompt", on_click=_reset_intake_state, use_container_width=True)
    st.button(
        "Save Current",
        disabled=bool(missing_fields) or not bool(generated_prompt.strip()),
        on_click=_save_history_entry,
        args=(context, generated_prompt),
        use_container_width=True,
    )

    history = st.session_state.get("prompt_history", [])
    if history:
        st.divider()
        for entry in history:
            st.button(
                entry["title"],
                key=f"history_{entry['id']}",
                on_click=_load_history_entry,
                args=(entry,),
                use_container_width=True,
            )
            st.caption(entry["subtitle"])
        st.divider()
        st.button("Clear History", on_click=_clear_prompt_history, use_container_width=True)
    else:
        st.markdown(
            """
            <div class="ds-empty-history">
                Generate a prompt or Agent Studio brief, then save it here for quick reuse during the session.
            </div>
            """,
            unsafe_allow_html=True,
        )


def _render_prompt_library_admin(bundled_library: list[dict[str, Any]]) -> list[dict[str, Any]]:
    configured_url = _configured_prompt_library_url()
    active_library = st.session_state.get("prompt_library_override", bundled_library)
    active_source = st.session_state.get("prompt_library_source", "Bundled Excel")

    with st.expander("Admin: Prompt Library", expanded=False):
        st.caption("Optional: load a living Google Sheet or Drive-hosted workbook.")
        library_url = st.text_input(
            "Google Sheet or Drive link",
            value=st.session_state.get("prompt_library_url", configured_url),
            key="prompt_library_url",
            placeholder="Paste a Google Sheets link or XLSX export URL",
        )
        refresh_left, reset_right = st.columns(2)

        if refresh_left.button("Refresh Library", use_container_width=True):
            _refresh_prompt_library_from_url(library_url)

        if reset_right.button("Use Bundled", use_container_width=True):
            _reset_prompt_library_source()
            active_library = bundled_library
            active_source = "Bundled Excel"

        if configured_url and not st.session_state.get("prompt_library_override") and not st.session_state.get("prompt_library_auto_loaded"):
            _refresh_prompt_library_from_url(configured_url, auto=True)

        active_library = st.session_state.get("prompt_library_override", bundled_library)
        active_source = st.session_state.get("prompt_library_source", active_source)
        error_message = st.session_state.get("prompt_library_error", "")
        entry_count = len(active_library)

        if error_message:
            st.info(error_message)
        elif active_source != "Bundled Excel":
            st.success(f"Using {active_source} ({entry_count} entries).")
        else:
            st.info(f"Using bundled Excel library ({entry_count} entries).")

        st.caption("Private Google files need an approved export link or Streamlit secrets.")

    return active_library


def _refresh_prompt_library_from_url(url: str, auto: bool = False) -> None:
    cleaned_url = url.strip()
    if not cleaned_url:
        st.session_state.prompt_library_error = "Paste a Google Sheet or Drive link first."
        return

    try:
        if not auto:
            _load_prompt_library_from_url.clear()
        entries = _load_prompt_library_from_url(cleaned_url)
    except (ValueError, HTTPError, URLError, TimeoutError, OSError) as error:
        st.session_state.pop("prompt_library_override", None)
        st.session_state.prompt_library_source = "Bundled Excel"
        st.session_state.prompt_library_error = f"Could not load the external library. Using bundled Excel instead. Detail: {error}"
        st.session_state.prompt_library_auto_loaded = True
        return

    if not entries:
        st.session_state.pop("prompt_library_override", None)
        st.session_state.prompt_library_error = "The external library loaded, but no prompt entries were found. Using bundled Excel instead."
        st.session_state.prompt_library_source = "Bundled Excel"
        st.session_state.prompt_library_auto_loaded = True
        return

    st.session_state.prompt_library_override = entries
    st.session_state.prompt_library_source = "Google library"
    st.session_state.prompt_library_error = ""
    st.session_state.prompt_library_auto_loaded = True


def _reset_prompt_library_source() -> None:
    for key in [
        "prompt_library_override",
        "prompt_library_source",
        "prompt_library_error",
        "prompt_library_auto_loaded",
    ]:
        st.session_state.pop(key, None)


def _configured_prompt_library_url() -> str:
    try:
        secret_url = st.secrets.get("PROMPT_LIBRARY_URL", "")
    except (FileNotFoundError, KeyError, AttributeError):
        secret_url = ""
    return str(secret_url or os.environ.get("PROMPT_LIBRARY_URL", "")).strip()


def _save_history_entry(context: dict[str, Any], artifact: str) -> None:
    history = st.session_state.get("prompt_history", [])
    if history and history[0].get("artifact") == artifact:
        return

    mode = _normalize_generation_mode(context.get("generation_mode", MODE_USE_LIBRARY))
    destination = context.get("output_destination", "Chat")
    artifact_label = _artifact_label(mode, destination)
    customer = context.get("customer_name", "").strip() or "Untitled"
    use_case = context.get("use_case", "").strip() or "Prompt"
    library_title = context.get("library_title", "").strip()
    timestamp = datetime.now().strftime("%I:%M %p").lstrip("0")

    entry = {
        "id": f"{datetime.now().timestamp()}",
        "title": f"{customer} - {library_title or artifact_label}",
        "subtitle": f"{use_case} | {destination} | {timestamp}",
        "artifact": artifact,
        "context": _serializable_context(context),
    }
    st.session_state.prompt_history = [entry, *history][:8]


def _load_history_entry(entry: dict[str, Any]) -> None:
    context = entry.get("context", {})
    st.session_state.loaded_history_entry = entry
    st.session_state.generation_mode = _normalize_generation_mode(context.get("generation_mode", MODE_USE_LIBRARY))
    st.session_state.library_prompt_type = context.get("library_prompt_type", "AI Agent")
    st.session_state.library_category = context.get("library_category", "")
    st.session_state.library_title = context.get("library_title", "")
    st.session_state.existing_prompt = context.get("existing_prompt", "")
    st.session_state.customer_name = context.get("customer_name", "")
    st.session_state.audience = context.get("audience", "")
    st.session_state.contract_type = context.get("contract_type", "")
    st.session_state.industry = context.get("industry", "")
    output_destinations = context.get("output_destinations", [])
    saved_destination = str(context.get("output_destination", ""))
    st.session_state.prompt_destination = (
        PROMPT_DESTINATION_AGENT_STUDIO
        if "Agent Studio" in output_destinations or "Agent Studio" in saved_destination
        else PROMPT_DESTINATION_AI_CHAT
    )
    st.session_state.use_case = context.get("use_case", "")
    st.session_state.document_scope = context.get("document_scope", "")
    st.session_state.agent_objective = context.get("agent_objective", "")
    st.session_state.workflow_data_outputs = "\n".join(context.get("required_fields", []))
    st.session_state.workflow_decision_logic = context.get("decision_logic", "")


def _reset_intake_state() -> None:
    for key in [
        "loaded_history_entry",
        "generation_mode",
        "library_prompt_type",
        "library_category",
        "library_title",
        "existing_prompt",
        "customer_name",
        "audience",
        "contract_type",
        "industry",
        "output_destination",
        "prompt_destination",
        "destination_chat",
        "destination_agent_studio",
        "destination_workflow",
        "use_case",
        "document_scope",
        "agent_objective",
        "workflow_data_outputs",
        "workflow_data_outputs_signature",
        "workflow_decision_logic",
        "validation_report",
    ]:
        st.session_state.pop(key, None)


def _clear_prompt_history() -> None:
    st.session_state.prompt_history = []
    st.session_state.pop("loaded_history_entry", None)


def _artifact_label(mode: str, destination: str) -> str:
    mode = _normalize_generation_mode(mode)
    if mode == MODE_GEMINI:
        return "Agent Studio Brief"
    if mode == MODE_CUSTOMIZE:
        return "Customized Prompt"
    if mode == MODE_USE_LIBRARY:
        return "Library Prompt"
    return f"{destination} Prompt"


def _normalize_generation_mode(mode: str) -> str:
    return LEGACY_GENERATION_MODE_ALIASES.get(mode, mode)


def _serializable_context(context: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in context.items()
        if isinstance(value, (str, int, float, bool, list, dict)) or value is None
    }


def _prompt_library_form(default_library: list[dict[str, Any]]) -> dict[str, Any]:
    _render_form_section_label("Prompt Library")
    st.markdown(
        """
        <div class="ds-library-note">
            Choose the closest AI Agent prompt pattern from the library. Filter by business area or search by title.
        </div>
        """,
        unsafe_allow_html=True,
    )
    library_entries = [entry for entry in default_library if entry.get("prompt_type") == "AI Agent"]

    if not library_entries:
        st.info("No AI Agent prompt library entries were found. The app will use the built-in renewal prompt pattern.")
        return {}

    categories = ["All categories", *_ordered_unique(category for entry in library_entries for category in _entry_categories(entry))]
    filter_left, filter_right = st.columns([1, 1.6])
    with filter_left:
        selected_category = st.selectbox("Business area", categories, key="library_category_filter")
    with filter_right:
        search_text = st.text_input("Search prompts", key="library_search", placeholder="Search by title, category, or description")

    filtered_entries = _filter_library_entries(library_entries, selected_category, search_text)
    if not filtered_entries:
        st.info("No prompt patterns match the current filters. Showing the full library instead.")
        filtered_entries = library_entries

    title_to_entry: dict[str, dict[str, Any]] = {}
    for entry in filtered_entries:
        title = entry.get("title", "Untitled")
        if title not in title_to_entry:
            title_to_entry[title] = entry

    titles = list(title_to_entry)
    saved_title = st.session_state.get("library_title", "")
    if saved_title and saved_title not in titles:
        st.session_state.pop("library_title", None)
        saved_title = ""
    title_index = _library_title_index(list(title_to_entry.values()), saved_title)
    selected_label = st.selectbox("Agent prompt", titles, index=title_index, key="library_title")
    selected_entry = title_to_entry[selected_label]

    _render_library_entry_preview(selected_entry)
    return selected_entry


def _filter_library_entries(entries: list[dict[str, Any]], selected_category: str, search_text: str) -> list[dict[str, Any]]:
    query = search_text.strip().lower()
    filtered = []
    for entry in entries:
        categories = _entry_categories(entry)
        if selected_category != "All categories" and selected_category not in categories:
            continue
        searchable = " ".join(
            [
                entry.get("title", ""),
                " ".join(categories),
                entry.get("description", ""),
                entry.get("prompt", ""),
                entry.get("data_outputs", ""),
            ]
        ).lower()
        if query and query not in searchable:
            continue
        filtered.append(entry)
    return filtered


def _entry_categories(entry: dict[str, Any]) -> list[str]:
    raw_category = entry.get("category", "General") or "General"
    if raw_category == "Sales & Legal":
        return ["Sales", "Legal"]
    return [raw_category]


def _entry_category_label(entry: dict[str, Any]) -> str:
    return ", ".join(_entry_categories(entry))


def _render_library_entry_preview(entry: dict[str, Any]) -> None:
    description = entry.get("description") or "No description provided in the library."
    business_value, agent_details = _split_library_description(description)
    data_outputs = entry.get("data_outputs") or "No data outputs listed."
    source_prompt = entry.get("prompt") or "No full source prompt listed yet. The app will use the description and data outputs as the starting point."

    with st.expander("Library Prompt Details", expanded=False):
        st.markdown(
            f"""
            <div class="ds-library-detail">
                <div class="ds-library-kicker">AI Agent Prompt · {escape(_entry_category_label(entry))}</div>
                <div class="ds-library-title">{escape(entry.get('title', 'Untitled'))}</div>
                <div class="ds-library-detail-grid">
                    <div>
                        <div class="ds-library-detail-label">Business value</div>
                        <div class="ds-library-detail-value">{_html_multiline(business_value or "Not provided")}</div>
                    </div>
                    <div>
                        <div class="ds-library-detail-label">What the agent does</div>
                        <div class="ds-library-detail-value">{_html_multiline(agent_details or "Not provided")}</div>
                    </div>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.markdown("**Starting prompt**")
        st.text_area("Starting prompt", value=source_prompt, height=180, label_visibility="collapsed", disabled=True)
        st.markdown("**Data outputs**")
        st.text_area("Data outputs", value=data_outputs, height=120, label_visibility="collapsed", disabled=True)
        if entry.get("link_to_assets"):
            st.markdown(f"**Link to assets:** {entry['link_to_assets']}")


def _split_library_description(description: str) -> tuple[str, str]:
    business_marker = "Business value:"
    agent_marker = "What the agent does:"
    if business_marker in description and agent_marker in description:
        after_business = description.split(business_marker, 1)[1]
        business_value, agent_details = after_business.split(agent_marker, 1)
        return business_value.strip(), agent_details.strip()
    if agent_marker in description:
        return "", description.split(agent_marker, 1)[1].strip()
    return "", description.strip()


def _html_multiline(value: str) -> str:
    return escape(value).replace("\n", "<br>")


def _context_form(template: dict[str, Any], rules: dict[str, Any], prompt_library: list[dict[str, Any]]) -> dict[str, Any]:
    defaults = template.get("context_defaults", {})

    _render_anchor("prompt-destination")
    _initialize_destination_state(defaults)
    _render_form_section_label("Prompt Destination", size="primary")
    st.caption("Choose AI Chat for ad-hoc Iris prompts, or Agent Studio when publishing this prompt as a reusable IAM Agent.")
    prompt_destination = st.radio(
        "Prompt Destination",
        [PROMPT_DESTINATION_AI_CHAT, PROMPT_DESTINATION_AGENT_STUDIO],
        horizontal=True,
        key="prompt_destination",
        label_visibility="collapsed",
    )

    _render_anchor("prompt-mode")
    _render_form_section_label("Prompt Mode", size="primary")
    if "generation_mode" in st.session_state:
        st.session_state.generation_mode = _normalize_generation_mode(st.session_state.generation_mode)
    generation_mode = st.radio(
        "Prompt Mode",
        GENERATION_MODES,
        horizontal=True,
        key="generation_mode",
        label_visibility="collapsed",
    )
    generation_mode = _normalize_generation_mode(generation_mode)
    _render_generation_mode_helper(generation_mode)

    selected_library_entry: dict[str, Any] = {}
    if generation_mode == MODE_USE_LIBRARY:
        _render_anchor("prompt-library")
        selected_library_entry = _prompt_library_form(prompt_library)

    existing_prompt = ""
    if generation_mode == MODE_CUSTOMIZE:
        _render_anchor("existing-prompt")
        existing_prompt = st.text_area(
            "Paste a prompt that already works well",
            value="",
            height=220,
            key="existing_prompt",
        )

    output_destinations = _selected_output_destinations(prompt_destination)
    output_destination = _destination_label(output_destinations)
    workflow_selected = prompt_destination == PROMPT_DESTINATION_AGENT_STUDIO

    _render_anchor("customize-prompt")
    _render_form_section_label("Demo Context", size="primary")
    left, right = st.columns(2)
    with left:
        customer_name = st.text_input("Customer", value="", key="customer_name")
        audience = st.text_input("LOB", value="", key="audience")
    with right:
        contract_type = st.text_input("Agreement type", value="", key="contract_type")
        industry = st.text_input("Industry", value="", key="industry")

    use_case = st.text_area(
        "Customer Use Case",
        value="",
        height=80,
        key="use_case",
        placeholder="What customer problem or story are you trying to show?",
    )

    _render_form_section_label("Agent Objective")
    document_scope = st.text_area(
        "What documents or data will the agent be reviewing?",
        value="",
        height=80,
        key="document_scope",
    )
    agent_objective = st.text_area(
        "What should the agent accomplish?",
        value="",
        height=80,
        key="agent_objective",
    )

    required_output_format = _default_output_format(output_destination)
    context_for_outputs = {
        "customer_name": customer_name,
        "industry": industry,
        "audience": audience,
        "use_case": use_case,
        "contract_type": contract_type,
        "agent_objective": agent_objective,
        "document_scope": document_scope,
        "output_destination": output_destination,
        "output_destinations": output_destinations,
        "required_output_format": required_output_format,
        "generation_mode": generation_mode,
        "existing_prompt": existing_prompt,
        "library_prompt_type": selected_library_entry.get("prompt_type", ""),
        "library_category": _entry_category_label(selected_library_entry) if selected_library_entry else "",
        "library_title": selected_library_entry.get("title", ""),
        "library_description": selected_library_entry.get("description", ""),
        "library_prompt": selected_library_entry.get("prompt", ""),
        "library_data_outputs": selected_library_entry.get("data_outputs", ""),
    }
    required_fields_default = _data_output_defaults(context_for_outputs, selected_library_entry, rules)
    decision_logic_default = defaults.get("decision_logic") or _definitions_as_lines(rules)

    if workflow_selected:
        _sync_generated_workflow_outputs(selected_library_entry, required_fields_default)
        if "workflow_decision_logic" not in st.session_state:
            st.session_state.workflow_decision_logic = decision_logic_default
        required_fields = st.session_state.get("workflow_data_outputs", required_fields_default)
        decision_logic = st.session_state.get("workflow_decision_logic", decision_logic_default)
    else:
        required_fields = required_fields_default
        decision_logic = decision_logic_default

    return {
        "customer_name": customer_name,
        "industry": industry,
        "audience": audience,
        "use_case": use_case,
        "contract_type": contract_type,
        "agent_objective": agent_objective,
        "document_scope": document_scope,
        "output_destination": output_destination,
        "output_destinations": output_destinations,
        "required_output_format": required_output_format,
        "required_fields": _lines(required_fields),
        "decision_logic": decision_logic,
        "risk_tolerance": defaults.get("risk_tolerance", "Balanced"),
        "business_outcome": agent_objective,
        "generation_mode": generation_mode,
        "existing_prompt": existing_prompt,
        "library_prompt_type": selected_library_entry.get("prompt_type", ""),
        "library_category": _entry_category_label(selected_library_entry) if selected_library_entry else "",
        "library_title": selected_library_entry.get("title", ""),
        "library_description": selected_library_entry.get("description", ""),
        "library_prompt": selected_library_entry.get("prompt", ""),
        "library_data_outputs": selected_library_entry.get("data_outputs", ""),
        "library_link_to_assets": selected_library_entry.get("link_to_assets", ""),
        "library_source_sheet": selected_library_entry.get("source_sheet", ""),
    }


def _library_field_overrides_text(entry: dict[str, Any]) -> str:
    title_key = entry.get("title", "").strip().lower()
    fields = PROMPT_FIELD_OVERRIDES.get(title_key, [])
    return "\n".join(fields)


def _data_output_defaults(context: dict[str, Any], selected_library_entry: dict[str, Any], rules: dict[str, Any]) -> str:
    library_outputs = selected_library_entry.get("data_outputs", "").strip()
    library_overrides = _library_field_overrides_text(selected_library_entry)
    if selected_library_entry:
        return _normalize_data_output_text(
            library_outputs
            or library_overrides
            or _objective_based_data_outputs(context, rules)
        )

    generation_mode = _normalize_generation_mode(context.get("generation_mode", ""))
    if generation_mode == MODE_CUSTOMIZE:
        inferred_outputs = _infer_data_outputs_from_prompt(context.get("existing_prompt", ""))
        if inferred_outputs:
            return _normalize_data_output_text(inferred_outputs)

    return _normalize_data_output_text(_objective_based_data_outputs(context, rules))


def _objective_based_data_outputs(context: dict[str, Any], rules: dict[str, Any]) -> str:
    combined = " ".join(
        str(context.get(key, ""))
        for key in [
            "library_title",
            "library_description",
            "library_prompt",
            "existing_prompt",
            "industry",
            "audience",
            "use_case",
            "contract_type",
            "document_scope",
            "agent_objective",
        ]
    ).lower()

    profile_outputs = [
        (
            ["renewal", "auto-renew", "auto renew", "notice", "renegotiate", "supplier", "vendor", "procurement"],
            [
                "agreement_name",
                "counterparty",
                "renewal_status",
                "renewal_date",
                "notice_deadline",
                "auto_renewal",
                "unfavorable_terms",
                "cost_exposure",
                "recommended_action",
                "owner",
            ],
        ),
        (
            ["clause", "redline", "playbook", "fallback", "deviation", "standard position", "non-standard"],
            [
                "agreement_name",
                "counterparty",
                "clause_name",
                "deviation_summary",
                "risk_level",
                "preferred_position",
                "suggested_language",
                "recommended_action",
                "owner",
            ],
        ),
        (
            ["whitespace", "revenue", "upsell", "cross-sell", "sales", "account", "opportunity", "coverage"],
            [
                "account_name",
                "agreement_name",
                "active_products",
                "coverage_gap",
                "revenue_opportunity",
                "source_evidence",
                "recommended_action",
                "owner",
            ],
        ),
        (
            ["triage", "route", "routing", "approval", "review queue", "intake", "assign"],
            [
                "request_type",
                "agreement_name",
                "counterparty",
                "risk_level",
                "review_queue",
                "required_approver",
                "routing_reason",
                "recommended_action",
            ],
        ),
        (
            ["privacy", "security", "data protection", "ip", "intellectual property", "liability", "indemnity"],
            [
                "agreement_name",
                "counterparty",
                "clause_name",
                "risk_type",
                "risk_level",
                "source_evidence",
                "mitigation",
                "recommended_action",
                "owner",
            ],
        ),
        (
            ["extract", "extraction", "metadata", "report", "inventory", "list", "dashboard"],
            [
                "agreement_name",
                "counterparty",
                "agreement_type",
                "effective_date",
                "expiration_date",
                "key_term",
                "source_evidence",
                "missing_information",
                "recommended_action",
            ],
        ),
    ]

    for keywords, outputs in profile_outputs:
        if any(keyword in combined for keyword in keywords):
            return "\n".join(outputs)

    fallback = rules.get("required_fields", [])
    if fallback:
        return "\n".join(fallback)

    return "\n".join(
        [
            "agreement_name",
            "counterparty",
            "key_finding",
            "business_impact",
            "source_evidence",
            "recommended_action",
            "owner",
        ]
    )


def _infer_data_outputs_from_prompt(prompt: str) -> str:
    candidates: list[str] = []
    prompt_text = prompt.strip()
    if not prompt_text:
        return ""

    lines = [line.strip() for line in prompt_text.splitlines()]
    capture = False
    for line in lines:
        lower_line = line.lower()
        if any(marker in lower_line for marker in ["data output", "data field", "required field", "columns:"]):
            capture = True
            after_colon = line.split(":", 1)[1].strip() if ":" in line else ""
            if after_colon:
                candidates.extend(_split_output_label_list(after_colon))
            continue
        if capture and not line:
            break
        if capture:
            if lower_line.endswith(":") and not any(separator in line for separator in ["|", ",", ";"]):
                break
            candidates.extend(_split_output_label_list(line))

    for row in re.findall(r"([A-Za-z][A-Za-z0-9_ /-]+(?:\s*\|\s*[A-Za-z][A-Za-z0-9_ /-]+){2,})", prompt_text):
        candidates.extend(_split_output_label_list(row))

    json_like_keys = re.findall(r'"([A-Za-z][A-Za-z0-9_ -]{2,48})"\s*:', prompt_text)
    candidates.extend(json_like_keys)

    return "\n".join(_dedupe_data_output_fields(candidates))


def _split_output_label_list(value: str) -> list[str]:
    cleaned = value.strip().lstrip("-*•").strip()
    if not cleaned:
        return []
    parts = re.split(r"\s*\|\s*|\s*,\s*|\s*;\s*", cleaned)
    return [part.strip().strip("`'\"") for part in parts if part.strip()]


def _normalize_data_output_text(value: str) -> str:
    return "\n".join(_dedupe_data_output_fields(value.splitlines()))


def _dedupe_data_output_fields(labels: list[str]) -> list[str]:
    fields: list[str] = []
    seen: set[str] = set()
    ignored = {
        "data outputs",
        "required fields",
        "columns",
        "json",
        "output",
        "table",
    }
    for label in labels:
        field = _data_output_field_name(label)
        if not field or field in ignored or field in seen:
            continue
        fields.append(field)
        seen.add(field)
        if len(fields) >= 10:
            break
    return fields


def _data_output_field_name(label: str) -> str:
    field = re.sub(r"[^A-Za-z0-9]+", "_", label.strip()).strip("_").lower()
    field = re.sub(r"_+", "_", field)
    if not field or len(field) < 3:
        return ""
    if field[0].isdigit():
        field = f"field_{field}"
    return field


def _initialize_destination_state(defaults: dict[str, Any]) -> None:
    if "prompt_destination" in st.session_state:
        return

    saved_destination = str(st.session_state.get("output_destination", defaults.get("output_destination", "Chat")))
    st.session_state.prompt_destination = (
        PROMPT_DESTINATION_AGENT_STUDIO
        if "Agent Studio" in saved_destination
        else PROMPT_DESTINATION_AI_CHAT
    )


def _selected_output_destinations(prompt_destination: str) -> list[str]:
    if prompt_destination == PROMPT_DESTINATION_AGENT_STUDIO:
        return ["Agent Studio", "Workflow"]
    return ["Chat"]


def _destination_label(destinations: list[str]) -> str:
    if not destinations:
        return "No destination selected"
    if destinations == ["Chat"]:
        return PROMPT_DESTINATION_AI_CHAT
    if "Agent Studio" in destinations:
        return PROMPT_DESTINATION_AGENT_STUDIO
    return " + ".join(destinations)


def _uses_workflow_destination(context: dict[str, Any]) -> bool:
    destinations = context.get("output_destinations")
    if isinstance(destinations, list):
        return "Workflow" in destinations
    return "Workflow" in str(context.get("output_destination", ""))


def _uses_chat_destination(context: dict[str, Any]) -> bool:
    destinations = context.get("output_destinations")
    if isinstance(destinations, list):
        return "Chat" in destinations
    output_destination = str(context.get("output_destination", ""))
    return "Chat" in output_destination or "AI Chat" in output_destination


def _uses_agent_studio_destination(context: dict[str, Any]) -> bool:
    destinations = context.get("output_destinations")
    if isinstance(destinations, list):
        return "Agent Studio" in destinations
    return "Agent Studio" in str(context.get("output_destination", ""))


def _sync_generated_workflow_outputs(entry: dict[str, Any], generated_outputs: str) -> None:
    signature = "|".join([entry.get("title", ""), generated_outputs])
    if st.session_state.get("workflow_data_outputs_signature") != signature:
        st.session_state.workflow_data_outputs = generated_outputs
        st.session_state.workflow_data_outputs_signature = signature


def _missing_prompt_fields(context: dict[str, Any]) -> list[str]:
    required_labels = {
        "customer_name": "Customer",
        "audience": "LOB",
        "contract_type": "Agreement type",
        "industry": "Industry",
        "use_case": "Customer Use Case",
        "document_scope": "What documents or data will the agent be reviewing?",
        "agent_objective": "What should the agent accomplish?",
    }
    if _normalize_generation_mode(context.get("generation_mode", "")) == MODE_CUSTOMIZE:
        required_labels["existing_prompt"] = "Paste a prompt that already works well"
    missing = [label for field_name, label in required_labels.items() if not str(context.get(field_name, "")).strip()]
    if not context.get("output_destinations"):
        missing.append("Prompt Destination")
    return missing


def _default_output_format(output_destination: str) -> str:
    if output_destination == "Workflow":
        return "JSON"
    return "Plain Language Summary"


def _validation_destination(context: dict[str, Any], output_format: str) -> str:
    if _uses_agent_studio_destination(context):
        return "Chat"
    if _uses_workflow_destination(context) and output_format in {"JSON", "Workflow Variables"}:
        return "Workflow"
    if _uses_workflow_destination(context) and not _uses_chat_destination(context):
        return "Workflow"
    return "Chat"


def _build_generated_artifact(context: dict[str, Any], template: dict[str, Any], rules: dict[str, Any]) -> str:
    generation_mode = _normalize_generation_mode(context.get("generation_mode", MODE_USE_LIBRARY))
    if generation_mode == MODE_GEMINI:
        return _build_gemini_agent_brief(context, template, rules)
    if generation_mode == MODE_CUSTOMIZE:
        return _build_customized_prompt(context, template, rules)
    return build_prompt(context, template, rules)


def _build_customized_prompt(context: dict[str, Any], template: dict[str, Any], rules: dict[str, Any]) -> str:
    base_prompt = context.get("existing_prompt", "").strip()

    return f"""AI Agent Prompt

Role:
You are an AI Agent for a Docusign IAM demo. Use the proven prompt pattern as a reference, but adapt it to the customer context and response contract below.

Customer and evaluation context:
- Customer: {context['customer_name']}
- Industry: {context['industry']}
- LOB: {context['audience']}
- Customer Use Case: {context['use_case']}
- Agreement type: {context['contract_type']}
- Prompt destination: {context['output_destination']}

Documents and data reviewed:
{context['document_scope']}

Agent objective:
{context['agent_objective']}

Source prompt to adapt:
{base_prompt}

Adaptation rules:
- Preserve the strongest role, task, data-scope, and evidence guardrails from the source prompt.
- Replace any old customer, industry, LOB, use case, agreement type, objective, or prompt destination with the context above.
- If the source prompt conflicts with the response contract below, follow the response contract.
- Do not invent agreement terms, dates, commercial values, or source evidence.

{_prompt_behavior_contract(context, rules)}
"""


def _build_gemini_agent_brief(context: dict[str, Any], template: dict[str, Any], rules: dict[str, Any]) -> str:
    rules = _rules_for_validation(rules, context)
    output_rules = _library_output_style_instructions(context)
    required_fields = _required_fields_text(context, rules)
    allowed_values = _allowed_values_text(rules, context)
    action_rules = _agent_studio_action_rules_text(context, rules)
    prompt_type = "AI Agent"

    return f"""Agent Studio Brief: Net-New {prompt_type} Prompt

Role:
You are helping a DocuSign Solution Consultant create a customer-ready Agent Studio prompt for a demo or POC.

Goal:
Generate a net-new Agent Studio prompt from the customer context below. The prompt should be copy-ready for the AI-generated agent prompt area inside Docusign IAM Agent Studio.

Customer and demo context:
- Customer: {context['customer_name']}
- Industry: {context['industry']}
- LOB: {context['audience']}
- Customer Use Case: {context['use_case']}
- Agreement type: {context['contract_type']}
- Prompt destination: {context['output_destination']}

Documents and data the agent will review:
{context['document_scope']}

Agent objective:
{context['agent_objective']}

Required behavior:
- Write the final Agent Studio prompt, not an explanation of the prompt.
- Make the agent's user-facing response concise, deterministic, and easy for an SC to use in a live customer demo.
- The prompt itself may be explicit and detailed; do not remove role, scope, evidence, missing-data, formatting, or action-planning guardrails just to shorten the prompt.
- Include clear role, task, evidence, hallucination, missing-information, output, and recommendation instructions.
- Include an Agent Studio operating contract that respects Docusign safety/privacy/compliance rules and configured tools/schemas before the prompt-specific behavior requirements.
- Include Agent Studio action-planning rules so the agent chooses the right action family, gathers required inputs, and avoids guessing configured IDs, schemas, fields, templates, statuses, or routes.
- Do not include internal implementation notes, schema jargon, or developer-facing language unless needed for Agent Studio data outputs.
- Do not invent agreement terms, dates, renewal rights, commercial values, source evidence, or business facts.
- Include destination-specific missing-data rules so prose responses use human-readable phrases and structured Data Outputs use schema-safe sentinel values.

Agent Studio action-planning rules to incorporate:
{action_rules}

Output requirements:
{output_rules}

Data outputs or visible fields:
{required_fields}

Allowed values:
{allowed_values}

Decision logic:
{context['decision_logic']}

Return format:
1. First return only the copy-ready {prompt_type} prompt.
2. Then add a short "What changed" section with no more than 5 bullets explaining the prompt design choices.
"""


def _build_demo_agreement_pack_brief(context: dict[str, Any], rules: dict[str, Any]) -> str:
    rules = _rules_for_validation(rules, context)
    data_outputs = _required_fields_text(context, rules)
    allowed_values = _allowed_values_text(rules, context)

    return f"""Demo Agreement PDF Brief: Mock Executed Agreement Set

Role:
You are creating demo-only source documents for a DocuSign Solution Consultant. The documents must be mock, safe for customer-facing demos, formatted as signed PDFs, and designed to produce a clear, compelling AI Agent response when used with the related agent prompt.

Important safety rules:
- Do not use real customer names, real people, real addresses, real signatures, real emails, real phone numbers, or confidential data.
- Mark every document and PDF footer as "Demo document - not legally binding."
- Use realistic contract language, but keep all parties, dates, amounts, products, and clauses mock/demo-only.
- Include mock executed signature blocks using invented signer names, invented titles, mock signature timestamps, and typed `/s/ [Invented Name]` signatures.
- Do not create or imitate any real person's handwritten signature.

Demo context:
- Customer/demo account: {context['customer_name']}
- Industry: {context['industry']}
- LOB: {context['audience']}
- Customer Use Case: {context['use_case']}
- Agreement type: {context['contract_type']}
- Prompt destination: {context['output_destination']}
- Selected prompt-library pattern: {context.get('library_title', 'Not selected')} ({context.get('library_prompt_type', 'Prompt')})

Documents or data the agent should review:
{context['document_scope']}

Agent objective:
{context['agent_objective']}

Create exactly 5 downloadable signed PDF files for this demo agreement pack. The files should represent a realistic set of related but distinct source documents, not one single agreement repeated five times:
1. One primary executed {context['contract_type']} PDF with a clear favorable or low-risk term profile.
2. One executed {context['contract_type']} PDF with an unfavorable renewal, commercial, legal, or operational term the agent should flag.
3. One executed order form, statement of work, purchase schedule, or service addendum PDF that changes the commercial picture.
4. One executed amendment, renewal notice, or extension PDF that creates deadline pressure or modifies a key term.
5. One metadata or account summary PDF with owner, business unit, spend, term dates, counterparty, status, and any source values needed to support the agent's Data Outputs.

Vary the five files intentionally:
- Use different mock counterparties, effective dates, expiration dates, renewal language, notice windows, owners, spend amounts, products/services, and risk levels.
- Include at least one clean/control document, one high-priority issue, one medium-priority issue, and one missing or ambiguous data point.
- Make the facts internally consistent so the agent can rank, compare, or recommend action across the set.
- Include enough structured metadata and short clause language for the agent to cite evidence from each relevant file.

Design the documents so the agent can produce a demo-ready response with:
- A crisp status or verdict.
- A compact table of key findings.
- One clear business risk or opportunity.
- One short evidence quote from the agreement pack.
- One recommended next action with an owner.

Data outputs or findings the source documents should support:
{data_outputs}

Allowed values to support when applicable:
{allowed_values}

Decision logic to make possible through the source documents:
{context['decision_logic']}

PDF output requirements:
- Create exactly 5 downloadable PDF files that the SC can save and import into a demo environment.
- Use clean agreement-style formatting with title page, section headings, numbered clauses, tables where useful, and signature pages.
- Name files using the customer name, agreement type, and a short document role, for example `{context['customer_name']}_{context['contract_type']}_MSA.pdf`, `{context['customer_name']}_Order_Form.pdf`, or `{context['customer_name']}_Renewal_Notice.pdf`.
- Each PDF should include enough realistic agreement text and metadata for the AI Agent to cite.
- If Gemini cannot directly attach downloadable PDFs in this workspace, return PDF-ready document content separated by clear file names and page breaks, and state that the content should be exported to PDF.

Return format:
1. First create the 5 downloadable signed PDF files.
2. Then provide a one-paragraph summary of the demo scenario.
3. Then list the generated PDF file names and what each file contains.
4. End with a "Golden path expected findings" section listing the exact facts the agent should be able to extract.
"""


def _render_agent_studio_mode_steps() -> None:
    st.markdown(
        """
        <div class="ds-steps ds-steps-compact">
            Copy the brief, open Agent Studio, and paste it into the AI-generated agent prompt builder.
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_agreement_pack_steps() -> None:
    st.markdown(
        """
        <div class="ds-steps ds-steps-compact">
            Use this brief to create mock source agreements when the demo environment needs better data.
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_prompt_copy_action(
    prompt: str,
    disabled: bool,
    button_label: str = "Copy Prompt",
    key_suffix: str = "generated_prompt",
) -> None:
    safe_suffix = "".join(character if character.isalnum() else "_" for character in key_suffix)
    if disabled:
        st.button(button_label, disabled=True, key=f"disabled_copy_prompt_{safe_suffix}")
        return

    escaped_prompt = json.dumps(prompt)
    components.html(
        f"""
        <div class="gemini-actions">
            <button id="copy-prompt-{safe_suffix}" type="button">{escape(button_label)}</button>
            <span id="copy-prompt-status-{safe_suffix}" aria-live="polite"></span>
        </div>
        <script>
            const optimizedPrompt = {escaped_prompt};
            const copyPromptButton = document.getElementById("copy-prompt-{safe_suffix}");
            const copyPromptStatus = document.getElementById("copy-prompt-status-{safe_suffix}");
            copyPromptButton.addEventListener("click", async () => {{
                try {{
                    await navigator.clipboard.writeText(optimizedPrompt);
                    copyPromptStatus.textContent = "Paste this prompt into Agent Studio.";
                }} catch (error) {{
                    copyPromptStatus.textContent = "Copy failed. Select the prompt text and copy it manually.";
                }}
            }});
        </script>
        <style>
            .gemini-actions {{
                align-items: center;
                display: flex;
                flex-wrap: wrap;
                gap: 0.75rem;
                padding: 0.2rem 0 0.7rem;
            }}
            .gemini-actions button {{
                align-items: center;
                background: #4c00ff;
                border: 1px solid #4c00ff;
                border-radius: 8px;
                box-sizing: border-box;
                color: #ffffff;
                cursor: pointer;
                display: inline-flex;
                font-family: "Helvetica Neue", Helvetica, Arial, sans-serif;
                font-size: 0.95rem;
                font-weight: 700;
                justify-content: center;
                min-height: 2.55rem;
                padding: 0.55rem 0.95rem;
            }}
            .gemini-actions button:hover {{
                background: #26065d;
                border-color: #26065d;
                color: #ffffff;
            }}
            #copy-prompt-status-{safe_suffix} {{
                color: #5f5577;
                font-family: "Helvetica Neue", Helvetica, Arial, sans-serif;
                font-size: 0.9rem;
            }}
        </style>
        """,
        height=72,
    )


def _render_agent_studio_actions(brief: str, disabled: bool) -> None:
    if disabled:
        left, right = st.columns(2)
        left.button("Copy Agent Studio Brief", disabled=True, key="disabled_copy_agent_studio_brief")
        right.button("Paste in Agent Studio", disabled=True, key="disabled_paste_agent_studio_brief")
        return

    escaped_brief = json.dumps(brief)
    agent_studio_url = escape(AGENT_STUDIO_URL, quote=True)
    components.html(
        f"""
        <div class="gemini-actions">
            <button id="copy-brief" type="button">Copy Agent Studio Brief</button>
            <a id="open-agent-studio" href="{agent_studio_url}" target="_blank" rel="noopener noreferrer">Paste in Agent Studio</a>
            <span id="copy-status" aria-live="polite"></span>
        </div>
        <script>
            const brief = {escaped_brief};
            const copyButton = document.getElementById("copy-brief");
            const status = document.getElementById("copy-status");
            copyButton.addEventListener("click", async () => {{
                try {{
                    await navigator.clipboard.writeText(brief);
                    status.textContent = "Copied. Open Agent Studio and paste into the prompt builder.";
                }} catch (error) {{
                    status.textContent = "Copy failed. Select the brief text and copy it manually.";
                }}
            }});
        </script>
        <style>
            .gemini-actions {{
                align-items: center;
                display: flex;
                flex-wrap: wrap;
                gap: 0.75rem;
                padding: 0.2rem 0 0.7rem;
            }}
            .gemini-actions button,
            .gemini-actions a {{
                align-items: center;
                border-radius: 8px;
                box-sizing: border-box;
                cursor: pointer;
                display: inline-flex;
                font-family: "Helvetica Neue", Helvetica, Arial, sans-serif;
                font-size: 0.95rem;
                font-weight: 700;
                justify-content: center;
                min-height: 2.55rem;
                padding: 0.55rem 0.95rem;
                text-decoration: none;
            }}
            .gemini-actions button {{
                background: #4c00ff;
                border: 1px solid #4c00ff;
                color: #ffffff;
            }}
            .gemini-actions a {{
                background: #4c00ff;
                border: 1px solid #4c00ff;
                color: #ffffff;
            }}
            .gemini-actions button:hover {{
                background: #26065d;
                border-color: #26065d;
                color: #ffffff;
            }}
            .gemini-actions a:hover {{
                background: #26065d;
                border-color: #26065d;
            }}
            #copy-status {{
                color: #5f5577;
                font-family: "Helvetica Neue", Helvetica, Arial, sans-serif;
                font-size: 0.9rem;
            }}
        </style>
        """,
        height=72,
    )


def _render_agreement_pack_actions(brief: str, disabled: bool) -> None:
    if disabled:
        left, right = st.columns(2)
        left.button("Copy Agreement PDF Brief", disabled=True, key="disabled_copy_agreement_pdf_brief")
        right.button("Paste in Gemini", disabled=True, key="disabled_paste_agreement_pdf_brief")
        return

    escaped_brief = json.dumps(brief)
    gemini_url = escape(GEMINI_CHAT_URL, quote=True)
    components.html(
        f"""
        <div class="gemini-actions">
            <button id="copy-agreement-pack-brief" type="button">Copy Agreement PDF Brief</button>
            <a id="open-gemini-agreement-pack" href="{gemini_url}" target="_blank" rel="noopener noreferrer">Paste in Gemini</a>
            <span id="copy-agreement-pack-status" aria-live="polite"></span>
        </div>
        <script>
            const agreementPackBrief = {escaped_brief};
            const copyAgreementPackButton = document.getElementById("copy-agreement-pack-brief");
            const agreementPackStatus = document.getElementById("copy-agreement-pack-status");
            copyAgreementPackButton.addEventListener("click", async () => {{
                try {{
                    await navigator.clipboard.writeText(agreementPackBrief);
                    agreementPackStatus.textContent = "Copied. Open Gemini and paste into the new chat.";
                }} catch (error) {{
                    agreementPackStatus.textContent = "Copy failed. Select the brief text and copy it manually.";
                }}
            }});
        </script>
        <style>
            .gemini-actions {{
                align-items: center;
                display: flex;
                flex-wrap: wrap;
                gap: 0.75rem;
                padding: 0.2rem 0 0.7rem;
            }}
            .gemini-actions button,
            .gemini-actions a {{
                align-items: center;
                border-radius: 8px;
                box-sizing: border-box;
                cursor: pointer;
                display: inline-flex;
                font-family: "Helvetica Neue", Helvetica, Arial, sans-serif;
                font-size: 0.95rem;
                font-weight: 700;
                justify-content: center;
                min-height: 2.55rem;
                padding: 0.55rem 0.95rem;
                text-decoration: none;
            }}
            .gemini-actions button {{
                background: #4c00ff;
                border: 1px solid #4c00ff;
                color: #ffffff;
            }}
            .gemini-actions a {{
                background: #4c00ff;
                border: 1px solid #4c00ff;
                color: #ffffff;
            }}
            .gemini-actions button:hover {{
                background: #26065d;
                border-color: #26065d;
                color: #ffffff;
            }}
            .gemini-actions a:hover {{
                background: #26065d;
                border-color: #26065d;
            }}
            #copy-agreement-pack-status {{
                color: #5f5577;
                font-family: "Helvetica Neue", Helvetica, Arial, sans-serif;
                font-size: 0.9rem;
            }}
        </style>
        """,
        height=72,
    )


def _render_output_rules(rules: dict[str, Any], context: dict[str, Any]) -> None:
    st.markdown("The app uses these rules quietly when generating and testing the prompt.")
    profile = _response_structure_profile(context)
    limits = _presentation_limits(context, rules)
    st.markdown(f"**Response pattern:** {profile['name']}")
    st.markdown("- Pick the response shape based on the agent objective instead of forcing one universal format.")
    st.markdown("- Use bold markdown for headings only where the destination renders markdown; otherwise use plain-text headings.")
    st.markdown("- Derive findings from evidence first, then write the answer-first summary.")
    if _uses_agent_studio_destination(context) and rules.get("agent_studio_action_rules"):
        st.markdown("**Agent Studio action planning**")
        st.markdown(
            "\n".join(
                [
                    "- Classify the user request into the right Agent Studio action family before acting.",
                    "- Verify required inputs, configured schemas, IDs, templates, fields, routes, and records before create/update/send/retrieve actions.",
                    "- For read/analyze requests, proceed with available source data and state assumptions or missing data.",
                    "- Ask one concise clarifying question only for create/update/send/route actions when required inputs are missing.",
                ]
            )
        )
    word_limit = _response_word_limit(context)
    if word_limit:
        st.markdown(
            "\n".join(
                [
                    f"- Keep responses under {word_limit} words.",
                    f"- Use no more than {limits['max_table_rows']} priority table rows.",
                    "- Aim for short table cells, but allow enough room for useful contract details.",
                    "- Use readable labels, not underscore field names.",
                ]
            )
        )
    elif _uses_workflow_destination(context):
        st.markdown("- Use structured fields instead of prose word limits.")
    if rules.get("allowed_values"):
        st.markdown("**Allowed result values**")
        for field_name, values in rules["allowed_values"].items():
            st.markdown(f"- {_human_label(field_name)}: {', '.join(values)}")


def _contains_any(text: str, phrases: list[str]) -> bool:
    return any(phrase in text for phrase in phrases)


def _response_word_limit(context: dict[str, Any]) -> int | None:
    if _uses_workflow_destination(context) and not _uses_chat_destination(context) and not _uses_agent_studio_destination(context):
        return None

    combined_context = " ".join(
        [
            str(context.get("audience", "")),
            str(context.get("use_case", "")),
            str(context.get("agent_objective", "")),
            str(context.get("generation_mode", "")),
        ]
    ).lower()

    if any(term in combined_context for term in ["leadership", "executive", "leader", "cxo", "c-suite", "board"]):
        return 175
    if any(term in combined_context for term in ["internal analysis", "debug", "debugging", "diagnostic", "troubleshoot", "testing"]):
        return 300
    return 225


def _word_limit_instruction(context: dict[str, Any]) -> str:
    word_limit = _response_word_limit(context)
    if word_limit is None:
        return "Use structured fields instead of a prose word limit."
    return f"Aim for a brief response under roughly {word_limit} words; if you must cut, cut supporting detail, never the answer-first conclusion, source support, or next action."


def _presentation_limits(context: dict[str, Any], rules: dict[str, Any] | None = None) -> dict[str, int]:
    configured = dict((rules or {}).get("customer_output_limits", {}))
    combined_context = _combined_response_context(context)
    row_heavy_objective = _contains_any(
        combined_context,
        [
            "all ",
            "all contracts",
            "all agreements",
            "list",
            "identify",
            "portfolio",
            "multiple",
            "compare",
            "supplier",
            "vendor",
            "clauses",
            "obligations",
        ],
    )
    defaults = {
        "max_words": _response_word_limit(context) or configured.get("max_words", 10_000),
        "max_headings": 5,
        "max_table_rows": 8 if row_heavy_objective or _uses_agent_studio_destination(context) else 6,
        "max_bullets": 6,
    }
    defaults.update({key: value for key, value in configured.items() if value is not None})
    defaults["max_words"] = _response_word_limit(context) or defaults["max_words"]
    defaults["max_headings"] = max(defaults.get("max_headings", 0), 5)
    defaults["max_bullets"] = max(defaults.get("max_bullets", 0), 6)
    if row_heavy_objective or _uses_agent_studio_destination(context):
        defaults["max_table_rows"] = max(defaults.get("max_table_rows", 0), 8)
    return defaults


def _finding_limit_instruction(context: dict[str, Any], rules: dict[str, Any] | None = None) -> str:
    max_rows = _presentation_limits(context, rules)["max_table_rows"]
    return (
        f"Include up to {max_rows} priority findings rows when useful; "
        "if more items exist, summarize the remainder as additional items found but not shown."
    )


def _evidence_instruction() -> str:
    return "Internally derive findings from source evidence first, then compose the answer-first summary; include 1-3 short evidence quotes, metadata citations, or source references when needed to support multiple findings."


def _recommended_action_limit_instruction() -> str:
    return "Include one primary recommended action, plus one optional follow-up when it adds practical value."


def _table_cell_instruction() -> str:
    return "Use tables only when the destination renders them clearly; otherwise use labeled bullets. If using a table, aim for cells under 12 words and do not exceed 18 words unless clause language needs the extra context."


def _heading_format_instruction(context: dict[str, Any]) -> str:
    if _uses_workflow_destination(context) and not _uses_chat_destination(context) and not _uses_agent_studio_destination(context):
        return "Do not add display headings to strict structured output."
    return "Use bold markdown for visible titles and subheaders only when the destination renders markdown; if markdown may show as literal asterisks or the response is sent downstream, use plain-text headings ending in a colon instead."


def _missing_data_instruction(context: dict[str, Any], rules: dict[str, Any] | None = None, *, structured: bool = False) -> str:
    prose_rule = (
        "For customer-facing prose, use `Not found in agreement` when expected agreement text or terms are absent; "
        "use `Not available in the provided data` when metadata, CRM, connected records, or account context is unavailable."
    )
    structured_rule = (
        "For structured Data Outputs, workflow variables, JSON, or routing fields, do not put prose missing-data phrases into enum/status fields. "
        "Use schema-safe sentinels from the allowed values: `NOT_FOUND` when allowed for status/result fields, `Unknown` when allowed for yes/no/unknown fields, and `null` only when the schema explicitly permits it. "
        "For free-text string fields, use the short missing phrase that matches the source type."
    )
    if structured:
        return f"{structured_rule} {prose_rule}"
    return prose_rule


def _pattern_precedence_instruction() -> str:
    return "If multiple response patterns could apply, let the agent objective and selected prompt-library title decide the primary pattern; use other matched patterns only as supporting detail."


def _profile_exemplar_text(context: dict[str, Any]) -> str:
    profile = _response_structure_profile(context)
    first_heading = profile["sections"][0].split(" - ", 1)[0]
    evidence_heading = "**Evidence**" if any("**Evidence**" in section for section in profile["sections"]) else "**Source support**"
    plain_first_heading = first_heading.replace("**", "")
    plain_evidence_heading = evidence_heading.replace("**", "")
    return "\n".join(
        [
            "- Compact response exemplar for style only; do not copy facts:",
            f"  Markdown-rendered heading: {first_heading}: [source-supported conclusion or status].",
            f"  Plain-text fallback: {plain_first_heading}: [source-supported conclusion or status].",
            "  Key finding: [highest-value finding tied to the objective].",
            f"  {plain_evidence_heading}: [short agreement quote, metadata citation, or source reference].",
            "  Recommended action: [owner + concrete next step].",
        ]
    )


GENERIC_VALIDATION_RULES = {
    "template_id": "dynamic_prompt_validation",
    "required_fields": [],
    "customer_facing_required_fields": [],
    "customer_output_limits": {
        "max_words": 225,
        "max_headings": 5,
        "max_table_rows": 8,
        "max_bullets": 6,
    },
    "allowed_values": {},
    "array_fields": {},
    "array_item_required_fields": {},
    "object_required_fields": {},
    "evidence_array_fields": [],
    "recommended_action_required_fields": [],
    "missing_information_phrase": "Not found in agreement",
    "generic_action_phrases": [
        "manual review required",
        "review required",
        "needs review",
        "check with legal",
        "follow up",
        "reach out",
    ],
    "hallucination_watch_terms": [],
}


def _rules_for_validation(rules: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    validation_rules = dict(rules if _uses_template_validator(context) else GENERIC_VALIDATION_RULES)
    if rules.get("agent_studio_action_rules"):
        validation_rules["agent_studio_action_rules"] = rules["agent_studio_action_rules"]

    dynamic_fields = _validation_required_fields(context)
    if not _uses_template_validator(context):
        validation_rules["required_fields"] = dynamic_fields
        validation_rules["customer_facing_required_fields"] = dynamic_fields[:8]
    elif dynamic_fields and _uses_workflow_destination(context):
        validation_rules["required_fields"] = dynamic_fields

    word_limit = _response_word_limit(context)
    if word_limit is not None:
        validation_rules["customer_output_limits"] = _presentation_limits(context, validation_rules)

    return validation_rules


def _guardrail_tuning_tab(
    template_id: str,
    context: dict[str, Any],
    generated_prompt: str,
    rules: dict[str, Any],
    missing_fields: list[str],
) -> None:
    _render_anchor("fine-tune-guardrails")
    _render_section_intro(
        "Fine-Tune Guardrails",
        "Describe how you want the agent response to change, then generate a supplemental instruction to add to the prompt.",
    )
    st.caption(
        "Use this for response structure, tone, length, sections, table shape, ranking logic, and demo presentation. "
        "Safety, source-grounding, missing-data, and no-hallucination rules stay in place."
    )

    tuning_request = st.text_area(
        "What do you want to fine-tune?",
        value="",
        height=180,
        key="guardrail_tuning_request",
        placeholder=(
            "Example: Make the response executive-ready. Lead with the renewal risk, show a table with only the top 5 contracts, "
            "keep each table cell under 10 words, and end with one procurement next step."
        ),
    )

    supplement = ""
    if not missing_fields and tuning_request.strip():
        supplement = _build_guardrail_supplement(context, generated_prompt, tuning_request, rules)

    st.button(
        "Generate Guardrail Supplement",
        type="primary",
        disabled=bool(missing_fields) or not bool(tuning_request.strip()),
    )

    st.subheader("Generated Guardrail Supplement")
    if missing_fields:
        st.info(f"Complete {', '.join(missing_fields)} to generate a guardrail supplement.")
    elif not tuning_request.strip():
        st.caption("Describe the change you want, then copy the generated supplement into the prompt or Agent Studio instructions.")

    st.text_area(
        "Copy-ready guardrail supplement",
        value=supplement,
        height=340,
        label_visibility="collapsed",
    )
    _render_prompt_copy_action(
        supplement,
        disabled=bool(missing_fields) or not bool(supplement.strip()),
        button_label="Copy Guardrail Supplement",
        key_suffix="guardrail_supplement",
    )

    with st.expander("Good fine-tune examples", expanded=False):
        st.markdown("- Make it more executive-ready and limit the response to three sections.")
        st.markdown("- Return a ranked table with the top 5 items and keep table cells under 10 words.")
        st.markdown("- Replace narrative recommendations with one owner, one action, and one deadline.")
        st.markdown("- Include only evidence that supports the final recommendation.")
        st.markdown("- For Agent Studio, keep Data Outputs exact but make the user-facing response more concise.")


def _build_guardrail_supplement(
    context: dict[str, Any],
    generated_prompt: str,
    tuning_request: str,
    rules: dict[str, Any],
) -> str:
    destination = context.get("output_destination", "AI Chat")
    response_contract = _optimized_response_contract(context, _rules_for_validation(rules, context))
    destination_rule = (
        "This supplement is for Agent Studio. Preserve configured tools, schemas, Data Outputs, workflow routing, and Agent Studio action logic."
        if _uses_agent_studio_destination(context)
        else "This supplement is for AI Chat. Apply it to the visible chat response and do not introduce Agent Studio workflow configuration."
    )

    return f"""Guardrail Supplement: Response Fine-Tuning

Use this as an addendum to the current agent prompt. It should refine the user-facing response behavior without replacing the core role, data scope, evidence rules, or safety rules.

Prompt destination:
{destination}

SC-requested response change:
{tuning_request.strip()}

Supplemental instructions to add:
- Apply the SC-requested response change above whenever it fits the user's request and available source data.
- {destination_rule}
- Treat this supplement as a presentation and response-structure override only.
- Keep Docusign safety, privacy, compliance, source-grounding, missing-data, and no-hallucination rules in force.
- Do not invent agreement terms, dates, commercial values, risks, source evidence, owners, deadlines, or business facts.
- If this supplement conflicts with required structured fields, schema-safe values, Data Outputs, or workflow routing logic, preserve the structured requirements and apply the supplement only to the user-facing explanation.
- If this supplement conflicts with evidence or missing-data requirements, follow the evidence and missing-data requirements.
- Keep headings bold when markdown is supported.
- Keep the answer focused enough for an SC to explain live.

Current response contract to preserve unless the SC-requested change explicitly adjusts presentation:
{response_contract}
"""


def _qa_wizard(
    template_id: str,
    context: dict[str, Any],
    generated_prompt: str,
    rules: dict[str, Any],
    missing_fields: list[str],
) -> None:
    _render_anchor("prompt-validation")
    _render_section_intro(
        "Demo-Readiness Check",
        "Checks that a response is well-formed, complete, and demo-ready. It does not confirm the findings are substantively correct; that remains the SC's judgment.",
    )
    validation_rules = _rules_for_validation(rules, context)
    default_output_format = context["required_output_format"]
    validation_signature = "|".join(
        [
            context.get("library_title", ""),
            context.get("output_destination", ""),
            default_output_format,
        ]
    )
    if st.session_state.get("validation_context_signature") != validation_signature:
        st.session_state.validation_output_format = default_output_format
        st.session_state.validation_context_signature = validation_signature

    output_format = st.selectbox(
        "Sample output format",
        ALLOWED_OUTPUT_FORMATS,
        key="validation_output_format",
    )
    _render_validation_scope(context, validation_rules)
    sample_output = st.text_area("Paste sample AI Agent output", value="", height=340, key="sample_output")

    if st.button("Run Demo-Readiness Check", type="primary", key="validate_output"):
        structural = validate_output(sample_output, output_format, validation_rules)
        validation_destination = _validation_destination(context, output_format)
        quality = score_business_quality(
            structural.parsed_output,
            sample_output,
            context["audience"],
            validation_destination,
            validation_rules,
        )
        recommendations = recommend_prompt_improvements(
            structural.issues,
            quality.issues,
            context["audience"],
            validation_destination,
        )
        optimized_prompt = build_optimized_prompt(generated_prompt, recommendations, validation_destination)
        overall_status = determine_status(
            structural.passed,
            quality.average_score,
            issue_count=len(structural.issues) + len(quality.issues),
        )

        st.session_state.validation_report = {
            "overall_status": overall_status,
            "structural": structural,
            "quality": quality,
            "recommendations": recommendations,
            "optimized_prompt": optimized_prompt,
            "template_id": template_id,
            "library_title": context.get("library_title", ""),
            "output_format": output_format,
        }

    report = st.session_state.get("validation_report")
    if report and report.get("template_id") == template_id and report.get("library_title") == context.get("library_title", ""):
        _render_report(report)


def _uses_template_validator(context: dict[str, Any]) -> bool:
    title = context.get("library_title", "").lower()
    prompt_type = context.get("library_prompt_type", "")
    return prompt_type == "AI Agent" and "renewal" in title


def _validation_required_fields(context: dict[str, Any]) -> list[str]:
    raw_fields: list[str] = []
    for field in context.get("required_fields", []):
        raw_fields.extend(_split_validation_field_candidates(field))
    if not raw_fields:
        title_key = context.get("library_title", "").strip().lower()
        raw_fields.extend(PROMPT_FIELD_OVERRIDES.get(title_key, []))

    normalized_fields = []
    seen = set()
    for field in raw_fields:
        normalized = _normalize_validation_field(field)
        if not normalized or normalized in seen:
            continue
        normalized_fields.append(normalized)
        seen.add(normalized)
    return normalized_fields[:12]


def _split_validation_field_candidates(field: str) -> list[str]:
    value = str(field).strip()
    if not value:
        return []
    for separator in ["|", ",", ";"]:
        value = value.replace(separator, "\n")
    candidates = []
    for line in value.splitlines():
        cleaned = line.strip().strip("-").strip()
        if cleaned:
            candidates.append(cleaned)
    return candidates


def _normalize_validation_field(field: str) -> str:
    value = field.strip().strip(":")
    value = value.split(":", 1)[0].strip()
    value = value.split(" - ", 1)[0].strip()
    if not value or len(value.split()) > 8:
        return ""
    normalized = "".join(character if character.isalnum() else "_" for character in value.lower())
    normalized = "_".join(part for part in normalized.split("_") if part)
    return normalized


def _render_validation_scope(context: dict[str, Any], validation_rules: dict[str, Any]) -> None:
    required_fields = validation_rules.get("required_fields", [])
    if _uses_template_validator(context):
        st.caption("Using template-specific rules plus demo-readiness scoring.")
    elif required_fields:
        visible_fields = ", ".join(_human_label(field) for field in required_fields[:8])
        extra_count = max(0, len(required_fields) - 8)
        suffix = f", and {extra_count} more" if extra_count else ""
        st.caption(f"Using dynamic readiness checks from this prompt pattern's Data Outputs: {visible_fields}{suffix}.")
    else:
        st.caption("Using universal demo-readiness checks. Add Data Outputs to the prompt pattern for stricter field-level checks.")

    with st.expander("What the Demo-Readiness Check reviews", expanded=False):
        word_limit = _response_word_limit(context)
        st.markdown(f"- Destination fit: {context['output_destination']} output expectations.")
        st.markdown(f"- Format: selected sample output format must parse cleanly.")
        if required_fields:
            st.markdown(f"- Required fields: {', '.join(_human_label(field) for field in required_fields[:12])}.")
        else:
            st.markdown("- Required fields: no field-level schema is configured for this pattern yet.")
        if word_limit:
            st.markdown(f"- Live-demo length: response should stay under {word_limit} words.")
        else:
            st.markdown("- Structured output: no prose word limit for Workflow-only output.")
        st.markdown("- Quality: specificity, actionability, audience fit, evidence, risk clarity, formatting, and demo usefulness.")


def _render_report(report: dict[str, Any]) -> None:
    status = report["overall_status"]
    structural = report["structural"]
    quality = report["quality"]
    recommendations = report["recommendations"]
    optimized_prompt = report["optimized_prompt"]

    status_type = {"PASS": "success", "FAIL": "error"}[status]
    getattr(st, status_type)(f"Overall status: {status}")

    col1, col2, col3 = st.columns(3)
    col1.metric("Structural checks", "Pass" if structural.passed else "Fail")
    col2.metric("Business-quality score", f"{quality.average_score} / 5")
    col3.metric("Issue count", len(structural.issues) + len(quality.issues))

    st.subheader("Structure & Completeness Results")
    if structural.issues:
        for issue in structural.issues:
            icon = "Error" if issue.severity == "error" else "Issue"
            st.markdown(f"**{icon}: `{issue.field}`**  \n{issue.message}  \nFix: {issue.fix_hint}")
    else:
        st.write("All required structural checks passed.")

    st.subheader("Business-Quality Score")
    st.dataframe(
        [{"Category": category, "Score": score} for category, score in quality.category_scores.items()],
        hide_index=True,
        width="stretch",
    )
    if quality.issues:
        st.markdown("**Quality issues**")
        for issue in quality.issues:
            st.markdown(f"- {issue}")

    st.subheader("Recommended Prompt Improvements")
    for recommendation in recommendations:
        st.markdown(f"- {recommendation}")

    st.subheader("Optional Prompt Tune-Up")
    st.caption("Use this only if the tested AI Agent response needs refinement after running against demo data.")
    st.text_area("Prompt tune-up", value=optimized_prompt, height=420, label_visibility="collapsed")
    _render_prompt_copy_action(optimized_prompt, disabled=False, button_label="Copy Tune-Up Prompt", key_suffix="validation_tune_up")

    st.subheader("Retest Checklist")
    for item in _retest_checklist():
        st.checkbox(item, value=False)


def build_prompt(context: dict[str, Any], template: dict[str, Any], rules: dict[str, Any]) -> str:
    if context.get("library_title"):
        return _build_library_prompt(context, rules)

    if context["required_output_format"] != "JSON" and not _uses_workflow_destination(context):
        return _build_customer_demo_prompt(context, template, rules)

    required_fields = _required_fields_text(context, rules)
    allowed_values = _allowed_values_text(rules, context)
    definitions = _definitions_text(rules)
    analysis_instructions = _bullets(rules.get("analysis_instructions", []))
    extraction_instructions = _bullets(rules.get("extraction_instructions", []))
    routing_logic = _bullets(rules.get("routing_logic", []))
    recommendation_requirements = _bullets(rules.get("recommendation_requirements", []))
    output_style = _output_style_instructions(context)
    example_output = _example_output_text(context, template)
    checklist = _bullets(template.get("validation_checklist", []))
    recommendation_schema_instruction = _recommended_action_instruction(context)
    prose_missing = _missing_data_instruction(context, rules, structured=False)
    structured_missing = _missing_data_instruction(context, rules, structured=True)

    return f"""Agent role:
You specialize in {template['name']} use cases where completed agreement language must be converted into structured, actionable business intelligence.

Agent objective:
{context['agent_objective']}

Customer context:
- Customer: {context['customer_name']}
- Industry: {context['industry']}
- Customer Use Case: {context['use_case']}
- Contract type: {context['contract_type']}
- Risk tolerance: {context['risk_tolerance']}

LOB:
{context['audience']}

Documents and data reviewed:
{context['document_scope']}

Input assumptions:
- Analyze only the agreement language, metadata, and business context provided in the current request.
- Do not invent contract terms.
- Use extracted agreement language where available.
- Return all required fields even when some values are missing.
- For prose findings, {prose_missing}
- For structured output, {structured_missing}

Analysis instructions:
{analysis_instructions}

Allowed values:
{allowed_values}

Definitions:
{definitions}

Extraction instructions:
{extraction_instructions}

Decision logic:
{context['decision_logic']}

Required output format:
{context['required_output_format']} for {context['output_destination']}.
If the output is intended for Workflow, use strict field names and allowed values only.

Output instructions:
{output_style}

Required fields or variables:
{required_fields}

Guardrails against hallucination:
- Do not invent contract terms, clause names, renewal dates, notice windows, payment terms, uplift rights, renewal rights, termination rights, or commercial values.
- Do not infer missing terms unless the agreement language or supplied metadata supports the inference.
- Derive findings from extracted agreement language, metadata, or source references before writing the answer.
- If raw clause language is unavailable, cite metadata or source references; if neither is available, use the appropriate missing-data value.

Instructions for missing information:
- Keep the field present.
- {prose_missing}
- {structured_missing}
- Explain business impact only when the provided agreement language supports it.

Escalation/routing logic:
{routing_logic}

Recommendation requirements:
{recommendation_requirements}
- Make recommendations specific and actionable.
- Avoid generic statements like "manual review required" unless paired with rationale and next step.
{recommendation_schema_instruction}

Example good output:
{example_output}

Validation checklist:
{checklist}
"""


def _build_library_prompt(context: dict[str, Any], rules: dict[str, Any]) -> str:
    return _build_library_agent_prompt(context, rules)


def _build_library_agent_prompt(context: dict[str, Any], rules: dict[str, Any]) -> str:
    rules = _rules_for_validation(rules, context)
    return f"""AI Agent Prompt

Role:
You are the {context['library_title']} for Docusign IAM.

Library pattern to apply:
- Category: {context['library_category']}
- Agent title: {context['library_title']}
- What the agent does: {context['library_description'] or 'Not provided'}

Customer and evaluation context:
- Customer: {context['customer_name']}
- Industry: {context['industry']}
- LOB: {context['audience']}
- Customer Use Case: {context['use_case']}
- Agreement type: {context['contract_type']}
- Prompt destination: {context['output_destination']}

Documents and data reviewed:
{context['document_scope']}

Agent objective:
{context['agent_objective']}

{_prompt_behavior_contract(context, rules)}
"""


def _build_customer_demo_prompt(context: dict[str, Any], template: dict[str, Any], rules: dict[str, Any]) -> str:
    allowed_values = _allowed_values_text(rules, context)
    visible_fields = _required_fields_text(context, rules)
    output_style = _output_style_instructions(context)
    example_output = _example_output_text(context, template)
    prose_missing = _missing_data_instruction(context, rules, structured=False)

    return f"""Agent role:
You specialize in completed agreement renewal intelligence. Turn agreement evidence into a concise, customer-facing renewal brief.

Agent objective:
{context['agent_objective']}

LOB:
{context['audience']}

Documents and data reviewed:
{context['document_scope']}

AI Chat operating context:
- This prompt is intended for ad-hoc Iris AI Chat, not a published Agent Studio workflow.
- Treat the user's current message and visible agreement/document context as primary.
- Use dynamically available chat retrieval, summary, comparison, or analysis capabilities when available.
- Do not assume configured Agent Studio tools, background runs, workflow routing, or persistent Data Outputs.
- If needed agreement, metadata, or record context is not available in chat, state what is missing or ask a concise follow-up; do not invent.

Required response contract:
- Return a demo brief, not a full analysis report.
{output_style}

Visible fields:
{visible_fields}

Allowed values:
{allowed_values}

Evidence and hallucination rules:
- Do not invent contract terms, dates, notice windows, renewal rights, uplift terms, or commercial values.
- Internally derive findings from extracted agreement language, metadata, or source references before writing the answer-first summary.
- Quote only the shortest useful evidence excerpt when raw agreement language is available.
- If raw clause language is unavailable, cite metadata or source references; if neither is available, use the appropriate missing-data phrase.
- {prose_missing}

Example output:
{example_output}
"""


def _prompt_behavior_contract(context: dict[str, Any], rules: dict[str, Any]) -> str:
    response_contract = _optimized_response_contract(context, rules)
    if not _uses_agent_studio_destination(context):
        return f"""AI Chat operating context:
- This prompt is intended for ad-hoc Iris AI Chat, not a published Agent Studio workflow.
- Treat the user's current message and visible agreement/document context as primary.
- Use dynamically available chat retrieval, summary, comparison, or analysis capabilities when available, but do not assume configured Agent Studio tools, background runs, workflow routing, or persistent Data Outputs.
- Answer the user's request directly and conversationally while following the response contract below.
- If needed agreement, metadata, or record context is not available in chat, state what is missing or ask a concise follow-up; do not invent.
- Do not include Agent Studio configuration details, tool schemas, workflow variables, or routing metadata in the customer-facing response.

Response contract:
{response_contract}"""

    action_rules = _agent_studio_action_rules_text(context, rules)
    return f"""Agent Studio operating contract:
- This is a Prompt Builder-generated behavior contract for a Docusign IAM Agent Studio agent.
- This prompt is intended for a configured, repeatable Agent Studio workflow, not ad-hoc Iris chat.
- Follow Docusign safety, privacy, compliance, configured tools, and configured schemas first.
- Respect the agent's configured tools, data sources, filters, schemas, and orchestration; these may constrain or shape how the free-text prompt is interpreted.
- Treat this prompt's role, objective, customer context, data scope, destination, response contract, evidence rules, missing-data rules, and next-action rules as hard requirements.
- Live user instructions can refine the request, but must not broaden the job, weaken guardrails, override required output structure, or request unsupported facts.
- Stay within the described customer, data, and demo scope.
- Base material findings only on the agreements, clause language, metadata, or records in scope.
- Use configured tool outputs, workflow filters, Data Outputs, and decision logic as the source of truth for repeatable agent runs.
- For read, analyze, summarize, compare, or search requests, proceed with available in-scope data and clearly state any assumptions or missing data instead of derailing the demo.
- For create, update, send, route, approve, or other state-changing actions, ask one concise clarifying question when required action inputs are missing; never guess.
- If required information is missing, use the destination-specific missing-data rules in this prompt instead of guessing.

Agent Studio action planning:
{action_rules}

Response contract:
{response_contract}"""


def _agent_studio_action_rules_text(context: dict[str, Any], rules: dict[str, Any]) -> str:
    action_catalog = rules.get("agent_studio_action_rules", {})
    if not action_catalog:
        return "\n".join(
            [
                "- Before acting, choose the configured Agent Studio action family and verify required inputs.",
                "- For read, analyze, summarize, compare, or search requests, proceed with available in-scope data and state assumptions or missing information.",
                "- For create, update, send, route, approve, or other state-changing actions, ask one concise clarifying question when required inputs are missing.",
            ]
        )

    core_rules = action_catalog.get("core_rules", [])
    selected_families = _select_agent_studio_action_families(context, action_catalog.get("action_families", []))
    selected_mappings = _select_agent_studio_action_mappings(
        context,
        action_catalog.get("action_mappings", []),
        selected_families,
    )
    lines = ["Core rules:"]
    lines.extend(f"- {rule}" for rule in core_rules[:10])
    lines.append("- Demo-aware clarification: for read, analyze, summarize, compare, or search requests, proceed with available in-scope data and state assumptions or missing information.")
    lines.append("- Demo-aware clarification: reserve clarifying questions for create, update, send, route, approve, or other state-changing actions where guessing would be unsafe.")

    if selected_families:
        lines.append("")
        lines.append("Relevant action families for this prompt:")
        for family in selected_families:
            tools = ", ".join(family.get("tools", [])[:4])
            triggers = "; ".join(family.get("trigger_examples", [])[:2])
            required_inputs = ", ".join(family.get("required_inputs", [])[:4])
            avoid = "; ".join(family.get("avoid", [])[:3])
            lines.append(f"- {family.get('name', 'action_family')}: use tools [{tools}] when the user intent resembles {triggers}. Required inputs: {required_inputs or 'configured tool schema'}. Avoid: {avoid or 'guessing missing configuration or source facts'}.")

    if selected_mappings:
        lines.append("")
        lines.append("Exact prompt-to-action mappings to preserve:")
        for mapping in selected_mappings:
            lines.append(
                "- "
                f"{mapping.get('tool', 'configured_action')}: "
                f"intent example: {mapping.get('real_scenario', 'matching user intent')}; "
                f"required input: {mapping.get('input_needed', 'configured tool schema')}; "
                f"returns: {mapping.get('output_returned', 'configured action output')}; "
                f"avoid: {mapping.get('common_mistake_to_avoid', 'guessing missing inputs')}."
            )

    lines.append("")
    lines.append("- If a user request maps to another configured Agent Studio action family, apply the same pattern: identify the action, verify required inputs, use the configured schema/tool output, and avoid guessing.")
    lines.append("- If a read/analyze request has partial source data, do not stop the demo solely to ask for more context; answer from available evidence and mark missing items using the prompt's missing-data rules.")
    return "\n".join(lines)


def _select_agent_studio_action_families(context: dict[str, Any], families: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not families:
        return []

    combined = " ".join(
        str(context.get(key, ""))
        for key in [
            "industry",
            "audience",
            "use_case",
            "contract_type",
            "agent_objective",
            "document_scope",
            "library_title",
            "library_description",
            "library_prompt",
            "library_data_outputs",
        ]
    ).lower()

    selected_names: set[str] = set()
    keyword_map = {
        "agreement_desk_request_setup": ["agreement desk", "request", "intake", "approval", "route"],
        "agreement_desk_create_update_request": ["agreement desk", "request", "approval", "status", "assign", "counterparty"],
        "agreement_desk_documents_messages_approvals": ["approval", "message", "reviewer", "document", "attachment", "memo"],
        "esignature_envelopes": ["esign", "signature", "sign", "envelope", "recipient"],
        "agreement_management_retrieval": ["agreement", "contract", "supplier", "procurement", "renewal", "auto-renewal", "clause", "msa", "sow", "terms"],
        "knowledge_search": ["playbook", "standard", "policy", "position", "fallback", "clause"],
        "document_generation": ["generate document", "docx", "template", "memo", "brief"],
        "connected_data_lookup": ["salesforce", "crm", "account", "opportunity", "supplier", "vendor", "spend", "cost"],
        "reporting_and_analytics": ["report", "dashboard", "analytics", "all", "list", "identify", "portfolio", "spend", "cost", "renewal"],
        "human_in_the_loop_choices": ["choose", "select", "confirm", "ask me", "approval", "route"],
        "canvas_output": ["canvas", "workspace", "summary view"],
    }

    for family_name, keywords in keyword_map.items():
        if any(keyword in combined for keyword in keywords):
            selected_names.add(family_name)

    if _uses_agent_studio_destination(context):
        selected_names.update({"agreement_management_retrieval", "human_in_the_loop_choices"})

    family_by_name = {family.get("name", ""): family for family in families}
    ordered_names = [
        "agreement_management_retrieval",
        "reporting_and_analytics",
        "connected_data_lookup",
        "knowledge_search",
        "agreement_desk_request_setup",
        "agreement_desk_create_update_request",
        "agreement_desk_documents_messages_approvals",
        "esignature_envelopes",
        "document_generation",
        "human_in_the_loop_choices",
        "canvas_output",
    ]
    selected = [family_by_name[name] for name in ordered_names if name in selected_names and name in family_by_name]
    return selected[:6]


def _select_agent_studio_action_mappings(
    context: dict[str, Any],
    mappings: list[dict[str, Any]],
    selected_families: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not mappings:
        return []

    combined = " ".join(
        str(context.get(key, ""))
        for key in [
            "industry",
            "audience",
            "use_case",
            "contract_type",
            "agent_objective",
            "document_scope",
            "library_title",
            "library_description",
            "library_prompt",
            "library_data_outputs",
        ]
    ).lower()
    selected_family_names = {family.get("name", "") for family in selected_families}

    preferred_tools = _preferred_agent_studio_tools(combined)
    scored: list[tuple[int, int, dict[str, Any]]] = []
    for index, mapping in enumerate(mappings):
        tool = mapping.get("tool", "")
        family = mapping.get("family", "")
        searchable = " ".join(
            [
                tool,
                family,
                mapping.get("real_scenario", ""),
                mapping.get("input_needed", ""),
                mapping.get("output_returned", ""),
                mapping.get("common_mistake_to_avoid", ""),
            ]
        ).lower()
        score = 0
        if family in selected_family_names:
            score += 4
        if tool in preferred_tools:
            score += 5
        score += sum(1 for keyword in _agent_studio_mapping_keywords(combined) if keyword in searchable)
        if score:
            scored.append((score, -index, mapping))

    scored.sort(reverse=True)
    selected: list[dict[str, Any]] = []
    seen_tools: set[str] = set()
    family_counts: dict[str, int] = {}
    for _, _, mapping in scored:
        tool = mapping.get("tool", "")
        family = mapping.get("family", "")
        if tool in seen_tools:
            continue
        max_per_family = 4 if family == "agreement_management_retrieval" else 3
        if family_counts.get(family, 0) >= max_per_family:
            continue
        selected.append(mapping)
        seen_tools.add(tool)
        family_counts[family] = family_counts.get(family, 0) + 1
        if len(selected) >= 10:
            break

    return selected


def _preferred_agent_studio_tools(combined_context: str) -> set[str]:
    tools: set[str] = set()
    if any(term in combined_context for term in ["agreement", "contract", "supplier", "renewal", "auto-renewal", "msa", "sow"]):
        tools.update(
            {
                "system_list_agreements",
                "system_iam_search_agreements",
                "system_search_agreement_knowledge",
                "system_dms_get_documents",
                "system_get_agreement_hierarchy",
            }
        )
    if any(term in combined_context for term in ["cost", "spend", "supplier", "vendor", "crm", "salesforce", "account"]):
        tools.update(
            {
                "system_connected_data_list_connections",
                "system_connected_data_search_records",
                "system_reporting_get_parties",
                "system_get_party_snapshot",
            }
        )
    if any(term in combined_context for term in ["all", "list", "identify", "portfolio", "report", "dashboard", "owner"]):
        tools.add("system_iam_reporting_get_report_config")
    if any(term in combined_context for term in ["playbook", "fallback", "standard", "position", "unfavorable", "terms"]):
        tools.add("system_aiar_document_chat")
    if any(term in combined_context for term in ["choose", "select", "confirm", "route", "approval"]):
        tools.update({"system_hitl_ask_choice", "system_hitl_ask_multi_select", "system_hitl_ask_yes_no"})
    return tools


def _agent_studio_mapping_keywords(combined_context: str) -> list[str]:
    keywords = [
        "agreement",
        "contract",
        "supplier",
        "renewal",
        "auto-renewal",
        "cost",
        "spend",
        "terms",
        "clause",
        "search",
        "report",
        "record",
        "party",
        "snapshot",
        "filter",
        "document",
    ]
    return [keyword for keyword in keywords if keyword in combined_context]


def _response_structure_profile(context: dict[str, Any]) -> dict[str, Any]:
    combined = _combined_response_context(context)

    if _contains_any(
        combined,
        [
            "renewal",
            "auto-renew",
            "autorenew",
            "notice window",
            "notice deadline",
            "renegotiate",
            "upcoming",
        ],
    ):
        return {
            "name": "renewal risk and renegotiation brief",
            "sections": [
                "**Renewal signal** - lead with status, deadline pressure, and urgency.",
                "**Contracts needing attention** - show the highest-priority agreements.",
                "**Unfavorable terms** - call out renewal, cost, notice, or leverage issues.",
                "**Evidence** - cite the shortest useful agreement language or metadata.",
                "**Recommended action** - give one owner, action, and timing.",
            ],
            "table_rule": "Use a table when comparing multiple agreements; keep columns tied to renewal timing, risk, cost, and action.",
        }

    if _contains_any(
        combined,
        [
            "clause",
            "playbook",
            "redline",
            "fallback",
            "liability",
            "indemnity",
            "ip",
            "privacy",
            "non-standard",
            "deviation",
            "legal exception",
        ],
    ):
        return {
            "name": "clause risk and fallback brief",
            "sections": [
                "**Risk summary** - state the issue and materiality.",
                "**Clause findings** - identify clause, deviation, and risk.",
                "**Fallback position** - provide preferred or acceptable replacement language.",
                "**Evidence** - cite the shortest useful clause excerpt.",
                "**Recommended action** - route to the right owner with the next step.",
            ],
            "table_rule": "Use a table when comparing several clauses; keep each row focused on one issue and one fallback.",
        }

    if _contains_any(
        combined,
        [
            "spend",
            "cost",
            "supplier",
            "vendor",
            "procurement",
            "commercial",
            "price",
            "pricing",
            "leakage",
            "savings",
            "whitespace",
            "revenue",
            "upsell",
            "discount",
        ],
    ):
        return {
            "name": "commercial opportunity and leakage brief",
            "sections": [
                "**Opportunity signal** - lead with the commercial finding.",
                "**Commercial findings** - show spend, cost, pricing, or leakage signals.",
                "**Business impact** - explain why the finding matters now.",
                "**Evidence** - cite source agreement, CRM, or metadata support.",
                "**Recommended action** - give one commercial next step and owner.",
            ],
            "table_rule": "Use a table when comparing suppliers, accounts, spend, or opportunities; keep cells short enough to scan.",
        }

    if _contains_any(
        combined,
        [
            "triage",
            "approval",
            "route",
            "routing",
            "owner",
            "escalation",
            "assignment",
            "intake",
            "completeness",
            "missing",
        ],
    ):
        return {
            "name": "triage and routing brief",
            "sections": [
                "**Routing decision** - lead with status, owner, and destination.",
                "**Reason** - explain the inputs that drove the routing outcome.",
                "**Missing items** - list only blockers that affect the decision.",
                "**Evidence** - cite the agreement, request, or metadata source.",
                "**Next step** - make the handoff clear and actionable.",
            ],
            "table_rule": "Use a table when routing multiple items; keep statuses and owners easy to scan.",
        }

    if _contains_any(
        combined,
        [
            "extract",
            "extraction",
            "metadata",
            "obligation",
            "obligations",
            "field",
            "fields",
            "data output",
            "data outputs",
        ],
    ):
        return {
            "name": "extraction and obligation summary",
            "sections": [
                "**Extraction summary** - lead with what was found and what was missing.",
                "**Key data points** - return the highest-value extracted fields.",
                "**Obligations** - identify owner, due date, and requirement when present.",
                "**Evidence** - cite the source language for material fields.",
                "**Recommended action** - suggest the next review, task, or workflow step.",
            ],
            "table_rule": "Use a table for extracted fields or obligations; keep missing values explicit.",
        }

    return {
        "name": "business answer brief",
        "sections": [
            "**Answer** - lead with the most useful business conclusion.",
            "**Key findings** - include only findings that support the objective.",
            "**Evidence** - cite the source language or record supporting the finding.",
            "**Recommended action** - provide one owner and next step.",
        ],
        "table_rule": "Use a table only when it makes the response easier to scan.",
    }


def _combined_response_context(context: dict[str, Any]) -> str:
    return " ".join(
        str(context.get(key, ""))
        for key in [
            "library_category",
            "library_title",
            "library_description",
            "library_prompt",
            "library_data_outputs",
            "industry",
            "audience",
            "use_case",
            "contract_type",
            "document_scope",
            "agent_objective",
        ]
    ).lower()


def _dynamic_response_structure_text(context: dict[str, Any]) -> str:
    profile = _response_structure_profile(context)
    section_lines = "\n".join(f"  - {section}" for section in profile["sections"])
    return f"""- Choose the response pattern that best fits the agent objective instead of forcing one universal layout.
- Preferred pattern for this objective: {profile['name']}.
- {_pattern_precedence_instruction()}
- {_heading_format_instruction(context)}
- Internally identify the source-backed findings first; then write the answer-first summary.
- Start with the business answer, decision signal, or status before supporting details.
- Use these sections when relevant, and omit or rename sections when the user's request calls for a different concise shape:
{section_lines}
- {profile['table_rule']}
- Keep section names readable and customer-facing; do not expose underscores or machine field names.
{_profile_exemplar_text(context)}"""


def _profile_lead_instruction(context: dict[str, Any]) -> str:
    profile_name = _response_structure_profile(context)["name"]
    if "renewal" in profile_name:
        return "- Lead with the renewal status, deadline pressure, and action."
    if "clause" in profile_name:
        return "- Lead with the clause risk, materiality, and fallback position."
    if "commercial" in profile_name:
        return "- Lead with the commercial signal, business impact, and next action."
    if "triage" in profile_name:
        return "- Lead with the routing decision, owner, and next step."
    if "extraction" in profile_name:
        return "- Lead with what was found, what is missing, and what needs action."
    return "- Lead with the most useful business answer and next action."


def _optimized_response_contract(context: dict[str, Any], rules: dict[str, Any]) -> str:
    fields = _required_fields_text(context, rules)
    allowed_values = _allowed_values_text(rules, context)
    decision_logic = context.get("decision_logic", "").strip() or "No workflow decision logic configured."
    dynamic_structure = _dynamic_response_structure_text(context)
    finding_limit = _finding_limit_instruction(context, rules)
    evidence_limit = _evidence_instruction()
    action_limit = _recommended_action_limit_instruction()
    table_cell_limit = _table_cell_instruction()
    prose_missing = _missing_data_instruction(context, rules, structured=False)
    structured_missing = _missing_data_instruction(context, rules, structured=True)

    if _uses_agent_studio_destination(context):
        return f"""- Return a concise demo-ready answer, not a full analysis report.
{dynamic_structure}
- {_word_limit_instruction(context)}
- {finding_limit}
- {evidence_limit}
- {action_limit}
- {table_cell_limit}
- Use readable customer-facing labels; do not show underscores or machine field names.
- Do not return JSON in the user-facing answer unless Agent Studio explicitly requests structured output.
- Do not invent agreement terms, dates, commercial values, source evidence, or business facts.
- {prose_missing}
- {structured_missing}
- When Agent Studio requests structured fields or workflow routing, use the configured Data Outputs and decision logic below.

Agent Studio data outputs:
{fields}

Allowed values:
{allowed_values}

Agent Studio workflow logic:
{decision_logic}"""

    if _uses_workflow_destination(context) and not _uses_chat_destination(context):
        return f"""- Return strict JSON only.
- Use exact field names and allowed values.
- Do not wrap JSON in markdown fences.
- Return every configured Data Output, even when a value is missing.
- {structured_missing}
- Do not invent agreement terms, dates, commercial values, source evidence, or business facts.

Data Outputs:
{fields}

Allowed values:
{allowed_values}

Decision logic:
{decision_logic}"""

    if _uses_workflow_destination(context) and _uses_chat_destination(context):
        return f"""- Return a concise demo brief first, not a full analysis report.
{dynamic_structure}
- {_word_limit_instruction(context)}
- {finding_limit}
- {evidence_limit}
- {action_limit}
- {table_cell_limit}
- Use readable customer-facing labels in the chat brief; do not show underscores or machine field names there.
- Treat the configured Data Outputs as hidden workflow routing metadata, not chat-facing content.
- Do not include a "Workflow Data Outputs" section in the chat response.
- Do not display variable assignments such as `Risk_Level = High`, `renewal_status = NOTICE_WINDOW_OPEN`, or `auto_renewal = Yes` in the chat response.
- When the workflow runtime separately requests structured routing values, use the exact configured variable names and deterministic workflow-safe values.
- Do not invent agreement terms, dates, commercial values, source evidence, or business facts.
- {prose_missing}
- {structured_missing}

Internal workflow routing schema - do not display in chat:
{fields}

Allowed values:
{allowed_values}

Workflow routing logic:
{decision_logic}"""

    return f"""- Return a concise demo brief, not a full analysis report.
{dynamic_structure}
- {_word_limit_instruction(context)}
- {finding_limit}
- {evidence_limit}
- {action_limit}
- {table_cell_limit}
- Use readable customer-facing labels; do not show underscores or machine field names.
- Do not return JSON unless the user explicitly asks for JSON.
- Do not invent agreement terms, dates, commercial values, source evidence, or business facts.
- {prose_missing}
- Make every sentence easy for an SC to say out loud in a live demo.

Visible fields:
{fields}

Allowed values:
{allowed_values}"""


def build_expected_schema(rules: dict[str, Any]) -> dict[str, Any]:
    if rules.get("expected_schema"):
        return rules["expected_schema"]
    return {field_name: "string" for field_name in rules["required_fields"]}


def determine_status(structural_passed: bool, quality_score: float, issue_count: int = 0) -> str:
    if not structural_passed:
        return "FAIL"
    if issue_count:
        return "FAIL"
    if quality_score >= 4:
        return "PASS"
    return "FAIL"


def _load_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text())


def _load_rules(template_id: str, template: dict[str, Any]) -> dict[str, Any]:
    rules_path = RULES_DIR / f"{template_id}_rules.yaml"
    if rules_path.exists():
        return _attach_agent_studio_action_rules(_load_yaml(rules_path))
    return _attach_agent_studio_action_rules({
        "template_id": template_id,
        "required_fields": template.get("default_required_fields", []),
        "allowed_values": {},
        "missing_information_phrase": "Not found in agreement",
        "sample_files": {},
    })


def _attach_agent_studio_action_rules(rules: dict[str, Any]) -> dict[str, Any]:
    action_rules_path = RULES_DIR / "agent_studio_action_rules.yaml"
    if not action_rules_path.exists():
        return rules
    enriched_rules = dict(rules)
    enriched_rules["agent_studio_action_rules"] = _load_yaml(action_rules_path)
    return enriched_rules


def _load_default_prompt_library() -> list[dict[str, Any]]:
    if not PROMPT_LIBRARY_PATH.exists():
        return []
    return _load_prompt_library_from_bytes(PROMPT_LIBRARY_PATH.read_bytes())


@st.cache_data(show_spinner=False, ttl=300)
def _load_prompt_library_from_url(url: str) -> list[dict[str, Any]]:
    export_url = _prompt_library_export_url(url)
    request = Request(export_url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=12) as response:
        payload = response.read()
        content_type = response.headers.get("Content-Type", "")
    return _load_prompt_library_payload(payload, content_type, export_url)


def _prompt_library_export_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if "docs.google.com" in parsed.netloc and "/spreadsheets/d/" in parsed.path:
        sheet_id = parsed.path.split("/spreadsheets/d/", 1)[1].split("/", 1)[0]
        return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=xlsx"

    if "drive.google.com" in parsed.netloc:
        file_id = ""
        if "/file/d/" in parsed.path:
            file_id = parsed.path.split("/file/d/", 1)[1].split("/", 1)[0]
        else:
            file_id = parse_qs(parsed.query).get("id", [""])[0]
        if file_id:
            return f"https://drive.google.com/uc?export=download&id={file_id}"

    return url.strip()


def _load_prompt_library_payload(payload: bytes, content_type: str, source_url: str) -> list[dict[str, Any]]:
    if payload.startswith(b"PK"):
        return _load_prompt_library_from_bytes(payload)

    looks_like_csv = "csv" in content_type.lower() or source_url.lower().endswith(".csv")
    if looks_like_csv:
        text = payload.decode("utf-8-sig")
        rows = list(csv.reader(text.splitlines()))
        return _load_prompt_library_from_csv_rows(rows)

    raise ValueError("Expected an XLSX export link or published CSV link.")


def _load_prompt_library_from_csv_rows(rows: list[list[str]]) -> list[dict[str, Any]]:
    if not rows:
        return []
    return _ai_agent_entries(rows)


@st.cache_data(show_spinner=False)
def _load_prompt_library_from_bytes(workbook_bytes: bytes) -> list[dict[str, Any]]:
    sheets = _read_xlsx_sheets(workbook_bytes)
    entries: list[dict[str, Any]] = []
    entries.extend(_ai_agent_entries(sheets.get("AI Agents", [])))
    return entries


def _read_xlsx_sheets(workbook_bytes: bytes) -> dict[str, list[list[str]]]:
    namespaces = {
        "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
        "rel": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    }

    with zipfile.ZipFile(BytesIO(workbook_bytes)) as workbook:
        shared_strings = _xlsx_shared_strings(workbook, namespaces)
        sheet_paths = _xlsx_sheet_paths(workbook, namespaces)
        sheets: dict[str, list[list[str]]] = {}

        for sheet_name, sheet_path in sheet_paths.items():
            sheet_xml = ET.fromstring(workbook.read(sheet_path))
            rows: list[list[str]] = []
            for row in sheet_xml.findall(".//main:sheetData/main:row", namespaces):
                values_by_index: dict[int, str] = {}
                max_index = -1
                for cell in row.findall("main:c", namespaces):
                    index = _xlsx_column_index(cell.attrib.get("r", ""))
                    max_index = max(max_index, index)
                    values_by_index[index] = _xlsx_cell_value(cell, shared_strings, namespaces).strip()
                if max_index >= 0:
                    values = [values_by_index.get(index, "") for index in range(max_index + 1)]
                    if any(value for value in values):
                        rows.append(values)
            sheets[sheet_name] = rows

    return sheets


def _xlsx_shared_strings(workbook: zipfile.ZipFile, namespaces: dict[str, str]) -> list[str]:
    if "xl/sharedStrings.xml" not in workbook.namelist():
        return []
    shared_xml = ET.fromstring(workbook.read("xl/sharedStrings.xml"))
    return ["".join(item.itertext()) for item in shared_xml.findall("main:si", namespaces)]


def _xlsx_sheet_paths(workbook: zipfile.ZipFile, namespaces: dict[str, str]) -> dict[str, str]:
    workbook_xml = ET.fromstring(workbook.read("xl/workbook.xml"))
    rels_xml = ET.fromstring(workbook.read("xl/_rels/workbook.xml.rels"))
    rel_map = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels_xml}
    sheet_paths: dict[str, str] = {}

    for sheet in workbook_xml.findall(".//main:sheet", namespaces):
        rel_id = sheet.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
        target = rel_map.get(rel_id, "")
        if not target:
            continue
        sheet_path = "xl/" + target.lstrip("/") if not target.startswith("xl/") else target
        sheet_paths[sheet.attrib["name"]] = sheet_path

    return sheet_paths


def _xlsx_column_index(cell_ref: str) -> int:
    letters = "".join(character for character in cell_ref if character.isalpha())
    index = 0
    for character in letters:
        index = index * 26 + (ord(character.upper()) - 64)
    return max(index - 1, 0)


def _xlsx_cell_value(cell: ET.Element, shared_strings: list[str], namespaces: dict[str, str]) -> str:
    cell_type = cell.attrib.get("t")
    if cell_type == "s":
        value = cell.find("main:v", namespaces)
        if value is None or value.text is None:
            return ""
        shared_index = int(value.text)
        return shared_strings[shared_index] if shared_index < len(shared_strings) else ""
    if cell_type == "inlineStr":
        inline_string = cell.find("main:is", namespaces)
        return "".join(inline_string.itertext()) if inline_string is not None else ""
    value = cell.find("main:v", namespaces)
    return value.text if value is not None and value.text is not None else ""


def _ai_agent_entries(rows: list[list[str]]) -> list[dict[str, Any]]:
    if not rows:
        return []
    header = [_normalize_header(cell) for cell in rows[0]]
    category_index = _header_index(header, "category", "lob", default=0)
    title_index = _header_index(header, "agent_title", "title", default=1)
    description_index = _header_index(header, "what_the_agent_does", "description", default=2)
    business_value_index = _header_index(header, "business_value", default=-1)
    prompt_index = _header_index(header, "agent_prompt", "prompt", default=3)
    data_outputs_index = _header_index(header, "data_outputs_if_used_within_a_workflow", "data_outputs", default=4)
    link_index = _header_index(header, "link_to_assets", "assets", default=5)

    entries: list[dict[str, Any]] = []
    current_category = ""
    for row in rows[1:]:
        category = _clean_library_category(_cell(row, category_index) or current_category)
        current_category = category or current_category
        title = _cell(row, title_index)
        if not title:
            continue
        description = _library_description(_cell(row, business_value_index), _cell(row, description_index))
        entries.append(
            {
                "prompt_type": "AI Agent",
                "source_sheet": "AI Agents",
                "category": category or "General",
                "title": title,
                "description": description,
                "prompt": _cell(row, prompt_index),
                "data_outputs": _cell(row, data_outputs_index),
                "link_to_assets": _cell(row, link_index),
            }
        )
    return entries


def _cell(row: list[str], index: int) -> str:
    return row[index].strip() if 0 <= index < len(row) and isinstance(row[index], str) else ""


def _normalize_header(value: str) -> str:
    normalized = "".join(character.lower() if character.isalnum() else "_" for character in str(value))
    return "_".join(part for part in normalized.split("_") if part)


def _header_index(header: list[str], *names: str, default: int) -> int:
    for name in names:
        if name in header:
            return header.index(name)
    return default


def _library_description(business_value: str, description: str) -> str:
    if business_value and description:
        return f"Business value: {business_value}\n\nWhat the agent does: {description}"
    return description or business_value


def _ordered_unique(values: Any) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = _library_dedupe_key(value)
        if value and normalized not in seen:
            ordered.append(value)
            seen.add(normalized)
    return ordered


def _clean_library_category(value: str) -> str:
    return " ".join(value.split())


def _library_dedupe_key(value: str) -> str:
    return _clean_library_category(str(value)).lower()


def _library_category_index(categories: list[str], entries: list[dict[str, Any]], saved_category: str) -> int:
    saved_key = _library_dedupe_key(saved_category)
    for index, category in enumerate(categories):
        if _library_dedupe_key(category) == saved_key:
            return index
    for index, category in enumerate(categories):
        if any(
            _library_dedupe_key(entry.get("category", "")) == _library_dedupe_key(category)
            and DEFAULT_LIBRARY_TITLE_HINT.lower() in entry.get("title", "").lower()
            for entry in entries
        ):
            return index
    return 0


def _library_title_index(entries: list[dict[str, Any]], saved_label_or_title: str) -> int:
    if saved_label_or_title:
        for index, entry in enumerate(entries):
            if saved_label_or_title == entry.get("title", ""):
                return index
    for index, entry in enumerate(entries):
        if DEFAULT_LIBRARY_TITLE_HINT.lower() in entry.get("title", "").lower():
            return index
    return 0


def _sample_paths(rules: dict[str, Any], template_or_context: dict[str, Any]) -> tuple[Path, Path]:
    sample_files = rules.get("sample_files", {})
    defaults = template_or_context.get("context_defaults", template_or_context)
    output_format = defaults.get("required_output_format", "Plain Language Summary")
    if output_format == "JSON":
        passing_key, failing_key = "passing", "failing"
    else:
        passing_key, failing_key = "passing_plain", "failing_plain"
    passing = _resolve_repo_file(
        sample_files.get(passing_key, sample_files.get("passing", "samples/completed_agreements_renewal_passing_output.json"))
    )
    failing = _resolve_repo_file(
        sample_files.get(failing_key, sample_files.get("failing", "samples/completed_agreements_renewal_failing_output.json"))
    )
    return passing, failing


def _default_sample_text(template_id: str, passing_sample_path: Path) -> str:
    if st.session_state.get("sample_template_id") == template_id and st.session_state.get("sample_output"):
        return st.session_state.sample_output
    if passing_sample_path.exists():
        return passing_sample_path.read_text()
    return ""


def _first_active_template_id(templates: dict[str, Any]) -> str:
    for template_id, template in templates.items():
        if template.get("status") == "active":
            return template_id
    return next(iter(templates))


def _safe_index(options: list[str], selected: str) -> int:
    return options.index(selected) if selected in options else 0


def _lines(value: str) -> list[str]:
    return [line.strip().lstrip("- ").strip() for line in value.splitlines() if line.strip()]


def _bullets(items: list[str]) -> str:
    if not items:
        return "- Follow the required output schema exactly."
    return "\n".join(f"- {item}" for item in items)


def _allowed_values_text(rules: dict[str, Any], context: dict[str, Any]) -> str:
    if not rules.get("allowed_values"):
        return "- No template-specific allowed values configured."
    return "\n".join(f"- {_display_field_name(field_name, context)}: {', '.join(values)}" for field_name, values in rules["allowed_values"].items())


def _definitions_text(rules: dict[str, Any]) -> str:
    definitions = rules.get("status_definitions") or rules.get("triage_definitions") or {}
    if not definitions:
        return "- No template-specific definitions configured."
    return "\n".join(f"- {key}: {value}" for key, value in definitions.items())


def _definitions_as_lines(rules: dict[str, Any]) -> str:
    definitions = rules.get("status_definitions") or rules.get("triage_definitions") or {}
    return "\n".join(f"{key} = {value}" for key, value in definitions.items())


def _library_output_style_instructions(context: dict[str, Any]) -> str:
    dynamic_structure = _dynamic_response_structure_text(context)
    table_cell_limit = _table_cell_instruction()
    structured_missing = _missing_data_instruction(context, {}, structured=True)
    if _uses_agent_studio_destination(context):
        return "\n".join(
            [
                "- Create an Agent Studio-compatible prompt, not an ad-hoc Iris chat prompt.",
                "- Include an Agent Studio operating contract that respects platform safety, configured tools, and configured schemas first.",
                "- Keep the user-facing response concise and demo-ready.",
                dynamic_structure,
                "- Use readable labels in customer-facing output; do not show underscores there.",
                "- Treat Data Outputs and routing logic as Agent Studio configuration, not extra prose for the final user-facing answer.",
            ]
        )
    if _uses_workflow_destination(context) and not _uses_chat_destination(context):
        return "\n".join(
            [
                "- Return strict JSON only when this prompt is used for workflow automation.",
                "- Use exact field names and allowed values when workflow variables are configured.",
                "- Do not wrap JSON in markdown fences.",
                f"- {structured_missing}",
            ]
        )
    if _uses_workflow_destination(context) and _uses_chat_destination(context):
        return "\n".join(
            [
                "- Return a concise chat-ready summary first.",
                dynamic_structure,
                "- Treat workflow variables as hidden routing metadata, not chat-facing content.",
                "- Do not include a Workflow Data Outputs section in the chat response.",
                "- Do not display variable assignments such as `Risk_Level = High` or `renewal_status = NOTICE_WINDOW_OPEN` in the chat response.",
                "- If the workflow runtime separately requests structured routing values, use exact variable names and deterministic values.",
            ]
        )
    return "\n".join(
        [
            "- Create an ad-hoc Iris AI Chat prompt, not a configured Agent Studio workflow.",
            "- Follow the output format specified in the selected prompt-library pattern.",
            "- If the library pattern does not specify a format, use a concise business-ready summary or table.",
            dynamic_structure,
            "- Do not return raw JSON unless the user or Workflow destination requires it.",
            "- Use readable labels; do not show underscores in table titles, chart labels, headings, or field labels.",
            f"- {table_cell_limit}",
            "- Keep the response focused enough for an SC to explain in a live demo.",
        ]
    )


def _output_style_instructions(context: dict[str, Any]) -> str:
    output_format = context["required_output_format"]
    word_limit_instruction = _word_limit_instruction(context)
    dynamic_structure = _dynamic_response_structure_text(context)
    finding_limit = _finding_limit_instruction(context)
    evidence_limit = _evidence_instruction()
    action_limit = _recommended_action_limit_instruction()
    table_cell_limit = _table_cell_instruction()
    structured_missing = _missing_data_instruction(context, {}, structured=True)
    if _uses_agent_studio_destination(context):
        return "\n".join(
            [
                "- Produce a concise Agent Studio user-facing answer, not a full analysis report.",
                f"- {word_limit_instruction}",
                dynamic_structure,
                f"- {finding_limit}",
                f"- {evidence_limit}",
                f"- {action_limit}",
                f"- {table_cell_limit}",
                "- Use readable labels in customer-facing output; do not show underscores there.",
                "- Use configured Data Outputs only when Agent Studio requests structured fields or workflow routing.",
            ]
        )
    if output_format == "JSON" or (_uses_workflow_destination(context) and not _uses_chat_destination(context)):
        return "\n".join(
            [
                "- Return strict JSON only.",
                "- Do not wrap JSON in markdown fences.",
                "- Use exact field names and allowed values.",
                f"- {structured_missing}",
            ]
        )
    if _uses_workflow_destination(context) and _uses_chat_destination(context):
        return "\n".join(
            [
                "- Produce a demo brief first, not a full analysis report.",
                f"- {word_limit_instruction}",
                dynamic_structure,
                f"- {finding_limit}",
                f"- {evidence_limit}",
                f"- {action_limit}",
                f"- {table_cell_limit}",
                "- Do not show underscores in the customer-facing chat brief.",
                "- Treat workflow variables as hidden routing metadata, not chat-facing content.",
                "- Do not include a Workflow Data Outputs section in the chat response.",
                "- Do not display variable assignments such as `Risk_Level = High` or `renewal_status = NOTICE_WINDOW_OPEN` in the chat response.",
                "- If the workflow runtime separately requests structured routing values, keep workflow variable names exact.",
            ]
        )
    if output_format == "Markdown Table":
        return "\n".join(
            [
                "- Do not return raw JSON.",
                "- Produce a demo brief, not a full analysis report.",
                f"- {word_limit_instruction}",
                dynamic_structure,
                f"- {finding_limit}",
                f"- {evidence_limit}",
                f"- {action_limit}",
                f"- {table_cell_limit}",
                "- Use customer-facing labels such as `Agreement name`.",
                "- Do not show underscores in table titles, chart labels, headings, or field labels.",
                "- Make every sentence speakable in a live demo.",
            ]
        )
    if output_format == "Plain Language Summary" or _uses_chat_destination(context):
        return "\n".join(
            [
                "- Respond as an ad-hoc Iris AI Chat answer using the current user request and available chat context.",
                "- Do not assume Agent Studio background runs, configured workflow routing, or persistent Data Outputs.",
                "- Do not return raw JSON.",
                "- Produce a demo brief, not a full analysis report.",
                f"- {word_limit_instruction}",
                dynamic_structure,
                f"- {finding_limit}",
                f"- {evidence_limit}",
                f"- {action_limit}",
                f"- {table_cell_limit}",
                "- Use customer-facing labels such as `Agreement name`.",
                "- Do not show underscores in table titles, chart labels, headings, or field labels.",
                _profile_lead_instruction(context),
                "- Quote only the shortest evidence excerpt needed to support the finding.",
                "- Make every sentence speakable in a live demo.",
            ]
        )
    return "- Return a clear, structured response that covers every required field."


def _example_output_text(context: dict[str, Any], template: dict[str, Any]) -> str:
    if _uses_agent_studio_destination(context):
        chat_example = template.get("chat_output_example")
        if chat_example:
            return _bold_visible_example_headings(chat_example.rstrip())
    if context["required_output_format"] == "JSON" or (_uses_workflow_destination(context) and not _uses_chat_destination(context)):
        return "```json\n" + json.dumps(template["good_output_example"], indent=2) + "\n```"
    chat_example = template.get("chat_output_example")
    if chat_example:
        return _bold_visible_example_headings(chat_example.rstrip())
    return json.dumps(template.get("good_output_example", {}), indent=2)


def _bold_visible_example_headings(example: str) -> str:
    formatted_lines: list[str] = []
    for line in example.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
            indent = line[: len(line) - len(line.lstrip())]
            formatted_lines.append(f"{indent}**{heading}**" if heading else line)
        else:
            formatted_lines.append(line)
    return "\n".join(formatted_lines)


def _recommended_action_instruction(context: dict[str, Any]) -> str:
    if _uses_agent_studio_destination(context):
        return "- Include one recommended action with an owner, rationale, and next step."
    if context["required_output_format"] == "JSON" or _uses_workflow_destination(context):
        return "- Include action, owner, rationale, and next_step in recommended_action."
    return "- Include a recommended action with an action, owner, rationale, and next step."


def _required_fields_text(context: dict[str, Any], rules: dict[str, Any]) -> str:
    if context.get("library_title"):
        return "\n".join(f"- {_display_field_name(field, context)}" for field in context["required_fields"])
    if context["required_output_format"] == "JSON" or _uses_workflow_destination(context):
        return "\n".join(f"- {field}" for field in context["required_fields"])
    customer_fields = rules.get("customer_facing_required_fields", context["required_fields"])
    return "\n".join(f"- {_human_label(field)}" for field in customer_fields)


def _display_field_name(field_name: str, context: dict[str, Any]) -> str:
    if context["required_output_format"] == "JSON" or _uses_workflow_destination(context):
        return field_name
    return _human_label(field_name)


def _human_label(field_name: str) -> str:
    acronyms = {"arr": "ARR"}
    if "_" not in field_name:
        lower_name = field_name.lower()
        if lower_name in acronyms:
            return acronyms[lower_name]
        return field_name[:1].upper() + field_name[1:]
    words = field_name.split("_")
    formatted = [acronyms.get(word, word) for word in words]
    if not formatted:
        return field_name
    first = formatted[0].capitalize()
    return " ".join([first, *formatted[1:]])


def _retest_checklist() -> list[str]:
    return [
        "Rerun the AI Agent with the optimized prompt.",
        "Confirm every required field is returned.",
        "Confirm allowed values are exact and workflow-safe.",
        "Confirm renewal or risk items include extracted agreement language.",
        "Confirm missing terms are marked as Not found in agreement.",
        "Confirm the recommendation has an owner, rationale, and next step.",
    ]


def _inject_styles() -> None:
    css_path = _first_existing_path(
        REPO_DIR / "assets" / "app.css",
        REPO_DIR / "app.css",
        APP_DIR / "assets" / "app.css",
        APP_DIR / "app.css",
    )
    if css_path.exists():
        st.markdown(f"<style>{css_path.read_text()}</style>", unsafe_allow_html=True)


if __name__ == "__main__":
    main()
