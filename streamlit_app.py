from __future__ import annotations

import csv
import json
import os
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

from validators.optimize_prompt import build_optimized_prompt, recommend_prompt_improvements
from validators.validate_quality import score_business_quality
from validators.validate_structure import validate_output


ALLOWED_OUTPUT_DESTINATIONS = [
    "Chat",
    "Workflow",
    "Agreement Management",
    "Salesforce",
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

MODE_USE_LIBRARY = "Use Prompt Library"
MODE_CUSTOMIZE = "Customize Existing Prompt"
MODE_GEMINI = "Generate Gemini Brief"

GENERATION_MODES = [
    MODE_USE_LIBRARY,
    MODE_CUSTOMIZE,
    MODE_GEMINI,
]

LEGACY_GENERATION_MODE_ALIASES = {
    "Customize existing prompt": MODE_CUSTOMIZE,
    "Generate with Gemini Agent": MODE_GEMINI,
}

DEFAULT_LIBRARY_TITLE_HINT = "Renewals + Risk"


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
        st.info("Select a supported prompt-library pattern to generate and validate a complete MVP prompt.")
        return

    missing_fields = _missing_prompt_fields(context)
    generated_prompt = "" if missing_fields else _build_generated_artifact(context, selected_template, rules)

    with st.sidebar:
        _render_prompt_history_sidebar(context, generated_prompt, missing_fields)

    prompt_tab, agreement_pack_tab, qa_tab = st.tabs(["Prompt Builder", "Agreement Builder", "Prompt Validation"])

    with prompt_tab:
        _render_prompt_builder(context, template_id, generated_prompt, missing_fields, rules)

    with agreement_pack_tab:
        _render_agreement_pack_builder(context, template_id, missing_fields, rules)

    with qa_tab:
        _qa_wizard(template_id, context, generated_prompt, rules)


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
        "Generate the copy-ready agent prompt, customized prompt, or Gemini brief from the intake details above.",
    )
    generation_mode = _normalize_generation_mode(context["generation_mode"])
    action_label = "Generate Gemini Brief" if generation_mode == MODE_GEMINI else "Generate Prompt"
    st.button(action_label, type="primary", disabled=bool(missing_fields))

    if generation_mode == MODE_GEMINI:
        st.subheader("Generated Gemini Brief")
        _render_gemini_mode_steps()
        if missing_fields:
            st.info(f"Complete {', '.join(missing_fields)} to generate the Gemini Brief.")
        st.text_area("Copy-ready Gemini Brief", value=generated_prompt, height=520, label_visibility="collapsed")
        _render_gemini_actions(generated_prompt, disabled=bool(missing_fields))
        st.download_button(
            "Download Gemini Brief",
            generated_prompt,
            file_name=f"{template_id}_gemini_agent_brief.txt",
            disabled=bool(missing_fields),
        )
    else:
        st.subheader("Generated AI Agent Prompt")
        if missing_fields:
            st.info(f"Complete {', '.join(missing_fields)} to generate the prompt.")
        st.text_area("Copy-ready prompt", value=generated_prompt, height=560, label_visibility="collapsed")
        st.download_button("Download prompt", generated_prompt, file_name=f"{template_id}_prompt.txt", disabled=bool(missing_fields))

    _render_prompt_readiness(context, generated_prompt, missing_fields)

    with st.expander("Output rules", expanded=False):
        _render_output_rules(rules, context)


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
    st.text_area("Copy-ready Agreement PDF Brief", value=agreement_pack_brief, height=340, label_visibility="collapsed")
    _render_agreement_pack_actions(agreement_pack_brief, disabled=bool(missing_fields))


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
            <a class="ds-workflow-step" href="#prompt-mode">
                <span>1</span>
                <div>
                    <strong>Prompt Mode</strong>
                    <small>Start from existing or create new</small>
                </div>
            </a>
            <a class="ds-workflow-step" href="#customize-prompt">
                <span>2</span>
                <div>
                    <strong>Customize Prompt</strong>
                    <small>Enter demo details</small>
                </div>
            </a>
            <a class="ds-workflow-step" href="#prompt-builder">
                <span>3</span>
                <div>
                    <strong>Prompt Builder</strong>
                    <small>Generate custom prompt</small>
                </div>
            </a>
            <a class="ds-workflow-step" href="#agreement-builder">
                <span>4</span>
                <div>
                    <strong>Agreement Builder</strong>
                    <small>Generate mock agreements</small>
                </div>
            </a>
            <a class="ds-workflow-step" href="#prompt-validation">
                <span>5</span>
                <div>
                    <strong>Prompt Validation</strong>
                    <small>Test response accuracy</small>
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


def _render_form_section_label(title: str) -> None:
    st.markdown(f'<div class="ds-form-section-label">{escape(title)}</div>', unsafe_allow_html=True)


def _render_anchor(anchor_id: str) -> None:
    st.markdown(f'<div id="{escape(anchor_id)}" class="ds-anchor"></div>', unsafe_allow_html=True)


def _render_generation_mode_helper(generation_mode: str) -> None:
    mode_guidance = {
        MODE_USE_LIBRARY: "",
        MODE_CUSTOMIZE: "Adapt an existing prompt for a new customer or use case.",
        MODE_GEMINI: "Create a copy-ready brief for building a new prompt in Gemini.",
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
                Generate a prompt or Gemini brief, then save it here for quick reuse during the session.
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
            st.warning(error_message)
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
    st.session_state.output_destination = context.get("output_destination", "Chat")
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
        "use_case",
        "document_scope",
        "agent_objective",
        "workflow_data_outputs",
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
        return "Gemini Brief"
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
    st.subheader("Prompt Library")
    st.markdown(
        """
        <div class="ds-library-note">
            Choose the closest AI Agent prompt pattern from the library.
        </div>
        """,
        unsafe_allow_html=True,
    )
    library_entries = [entry for entry in default_library if entry.get("prompt_type") == "AI Agent"]

    if not library_entries:
        st.warning("No AI Agent prompt library entries were found. The app will use the built-in renewal prompt pattern.")
        return {}

    title_to_entry: dict[str, dict[str, Any]] = {}
    for entry in library_entries:
        title = entry.get("title", "Untitled")
        if title not in title_to_entry:
            title_to_entry[title] = entry

    titles = list(title_to_entry)
    saved_title = st.session_state.get("library_title", "")
    title_index = _library_title_index(list(title_to_entry.values()), saved_title)
    selected_label = st.selectbox("Agent prompt", titles, index=title_index, key="library_title")
    selected_entry = title_to_entry[selected_label]

    _render_library_entry_preview(selected_entry)
    return selected_entry


def _render_library_entry_preview(entry: dict[str, Any]) -> None:
    description = entry.get("description") or "No description provided in the library."
    data_outputs = entry.get("data_outputs") or "No data outputs listed."
    source_prompt = entry.get("prompt") or "No full source prompt listed yet. The app will use the description and data outputs as the starting point."

    st.markdown(
        f"""
        <div class="ds-library-preview">
            <div class="ds-library-kicker">AI Agent Prompt · {escape(entry.get('category', 'General'))}</div>
            <div class="ds-library-title">{escape(entry.get('title', 'Untitled'))}</div>
            <div class="ds-library-body">{escape(description)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    with st.expander("Library prompt details", expanded=False):
        st.markdown("**Starting prompt**")
        st.text_area("Starting prompt", value=source_prompt, height=180, label_visibility="collapsed", disabled=True)
        st.markdown("**Data outputs**")
        st.text_area("Data outputs", value=data_outputs, height=120, label_visibility="collapsed", disabled=True)
        if entry.get("link_to_assets"):
            st.markdown(f"**Link to assets:** {entry['link_to_assets']}")


def _context_form(template: dict[str, Any], rules: dict[str, Any], prompt_library: list[dict[str, Any]]) -> dict[str, Any]:
    defaults = template.get("context_defaults", {})

    st.subheader("Prompt Intake Form")
    _render_anchor("prompt-mode")
    _render_form_section_label("Prompt Mode")
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

    _render_anchor("customize-prompt")
    _render_form_section_label("Demo Context")
    left, right = st.columns(2)
    with left:
        customer_name = st.text_input("Customer", value="", key="customer_name")
        audience = st.text_input("LOB", value="", key="audience")
        contract_type = st.text_input("Agreement type", value="", key="contract_type")
        industry = st.text_input("Industry", value="", key="industry")
    with right:
        output_destination = st.selectbox(
            "Where will the agent be used?",
            ALLOWED_OUTPUT_DESTINATIONS,
            index=_safe_index(ALLOWED_OUTPUT_DESTINATIONS, defaults.get("output_destination", "Chat")),
            key="output_destination",
        )
        use_case = st.text_input(
            "Customer / evaluation use case",
            value="",
            key="use_case",
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

    library_data_outputs = selected_library_entry.get("data_outputs", "")
    required_fields_default = library_data_outputs or "\n".join(rules["required_fields"])
    decision_logic_default = defaults.get("decision_logic") or _definitions_as_lines(rules)
    if output_destination == "Workflow":
        required_fields = st.text_area(
            "Data Outputs",
            value=required_fields_default,
            height=135,
            key="workflow_data_outputs",
        )
        decision_logic = st.text_area(
            "Decision Logic",
            value=decision_logic_default,
            height=135,
            key="workflow_decision_logic",
        )
    else:
        required_fields = required_fields_default
        decision_logic = decision_logic_default

    required_output_format = _default_output_format(output_destination)

    return {
        "customer_name": customer_name,
        "industry": industry,
        "audience": audience,
        "use_case": use_case,
        "contract_type": contract_type,
        "agent_objective": agent_objective,
        "document_scope": document_scope,
        "output_destination": output_destination,
        "required_output_format": required_output_format,
        "required_fields": _lines(required_fields),
        "decision_logic": decision_logic,
        "risk_tolerance": defaults.get("risk_tolerance", "Balanced"),
        "business_outcome": agent_objective,
        "generation_mode": generation_mode,
        "existing_prompt": existing_prompt,
        "library_prompt_type": selected_library_entry.get("prompt_type", ""),
        "library_category": selected_library_entry.get("category", ""),
        "library_title": selected_library_entry.get("title", ""),
        "library_description": selected_library_entry.get("description", ""),
        "library_prompt": selected_library_entry.get("prompt", ""),
        "library_data_outputs": selected_library_entry.get("data_outputs", ""),
        "library_link_to_assets": selected_library_entry.get("link_to_assets", ""),
        "library_source_sheet": selected_library_entry.get("source_sheet", ""),
    }


def _missing_prompt_fields(context: dict[str, Any]) -> list[str]:
    required_labels = {
        "customer_name": "Customer",
        "audience": "LOB",
        "contract_type": "Agreement type",
        "industry": "Industry",
        "use_case": "Customer / evaluation use case",
        "document_scope": "What documents or data will the agent be reviewing?",
        "agent_objective": "What should the agent accomplish?",
    }
    if _normalize_generation_mode(context.get("generation_mode", "")) == MODE_CUSTOMIZE:
        required_labels["existing_prompt"] = "Paste a prompt that already works well"
    return [label for field_name, label in required_labels.items() if not str(context.get(field_name, "")).strip()]


def _default_output_format(output_destination: str) -> str:
    if output_destination in {"Workflow", "Salesforce"}:
        return "JSON"
    return "Plain Language Summary"


def _build_generated_artifact(context: dict[str, Any], template: dict[str, Any], rules: dict[str, Any]) -> str:
    generation_mode = _normalize_generation_mode(context.get("generation_mode", MODE_USE_LIBRARY))
    if generation_mode == MODE_GEMINI:
        return _build_gemini_agent_brief(context, template, rules)
    if generation_mode == MODE_CUSTOMIZE:
        return _build_customized_prompt(context, template, rules)
    return build_prompt(context, template, rules)


def _build_customized_prompt(context: dict[str, Any], template: dict[str, Any], rules: dict[str, Any]) -> str:
    base_prompt = context.get("existing_prompt", "").strip()
    generated_guardrails = build_prompt(context, template, rules)

    return f"""Adapted AI Agent Prompt:
Use the proven prompt pattern below, but tailor it to this customer demo context.

Customer context:
- Customer: {context['customer_name']}
- Industry: {context['industry']}
- LOB: {context['audience']}
- Customer / evaluation use case: {context['use_case']}
- Agreement type: {context['contract_type']}
- Agent destination: {context['output_destination']}
- Selected library pattern: {context.get('library_title', 'Not selected')} ({context.get('library_prompt_type', 'Prompt')})

Documents and data reviewed:
{context['document_scope']}

Agent objective:
{context['agent_objective']}

Instructions:
- Preserve the strongest structure, tone, and guardrails from the proven prompt.
- Replace any old customer, industry, LOB, use case, agreement type, objective, or output destination with the context above.
- Keep customer-facing responses concise, deterministic, and demo-ready.
- Do not expose machine field names with underscores unless the destination is Workflow or Salesforce.
- For Workflow or Salesforce, preserve strict field names, allowed values, and routing logic.
- Do not invent agreement terms, dates, commercial values, or source evidence.

Proven prompt to adapt:
{base_prompt}

Required quality bar:
{generated_guardrails}
"""


def _build_gemini_agent_brief(context: dict[str, Any], template: dict[str, Any], rules: dict[str, Any]) -> str:
    output_rules = _library_output_style_instructions(context)
    required_fields = _required_fields_text(context, rules)
    allowed_values = _allowed_values_text(rules, context)
    prompt_type = "AI Agent"
    library_title = context.get("library_title") or "Net-new prompt from intake context"
    library_category = context.get("library_category") or "Not selected"
    library_description = context.get("library_description") or "Use the intake form context as the starting point."
    library_prompt = context.get("library_prompt", "").strip() or "No source prompt selected; use the intake form context as the starting point."

    return f"""Gemini Agent Brief: Net-New {prompt_type} Prompt

Role:
You are a prompt architect helping a DocuSign Solution Consultant create a customer-ready Docusign IAM prompt for a demo or POC.

Goal:
Generate a net-new prompt for the selected prompt-library pattern and customer scenario below. The prompt should be copy-ready for a Docusign IAM AI chat, agent, workflow, or agreement management experience.

Selected prompt-library starting point:
- Type: {prompt_type}
- Category: {library_category}
- Title: {library_title}
- What it does: {library_description}

Source prompt:
{library_prompt}

Customer and demo context:
- Customer: {context['customer_name']}
- Industry: {context['industry']}
- LOB: {context['audience']}
- Customer / evaluation use case: {context['use_case']}
- Agreement type: {context['contract_type']}
- Agent destination: {context['output_destination']}

Documents and data the agent will review:
{context['document_scope']}

Agent objective:
{context['agent_objective']}

Required behavior:
- Write the final AI Agent prompt, not an explanation of the prompt.
- Make it concise, deterministic, and easy for an SC to use in a live customer demo.
- Include clear role, task, evidence, hallucination, missing-information, output, and recommendation instructions.
- Do not include internal implementation notes, schema jargon, or developer-facing language unless the agent destination is Workflow or Salesforce.
- Do not invent agreement terms, dates, renewal rights, commercial values, source evidence, or business facts.
- If source information is missing, instruct the agent to write "Not found in agreement."

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
    data_outputs = _required_fields_text(context, rules)
    allowed_values = _allowed_values_text(rules, context)

    return f"""Demo Agreement PDF Brief: Fictional Executed Agreement Set

Role:
You are creating demo-only source documents for a DocuSign Solution Consultant. The documents must be fictional, safe for customer-facing demos, formatted as signed PDFs, and designed to produce a clear, compelling AI Agent response when used with the related agent prompt.

Important safety rules:
- Do not use real customer names, real people, real addresses, real signatures, real emails, real phone numbers, or confidential data.
- Mark every document and PDF footer as "Fictional demo document - not legally binding."
- Use realistic contract language, but keep all parties, dates, amounts, products, and clauses fictional.
- Include mock executed signature blocks using fictional signer names, fictional titles, fictional signature timestamps, and typed `/s/ Fictional Name` signatures.
- Do not create or imitate any real person's handwritten signature.

Demo context:
- Customer/demo account: {context['customer_name']}
- Industry: {context['industry']}
- LOB: {context['audience']}
- Customer / evaluation use case: {context['use_case']}
- Agreement type: {context['contract_type']}
- Agent destination: {context['output_destination']}
- Selected prompt-library pattern: {context.get('library_title', 'Not selected')} ({context.get('library_prompt_type', 'Prompt')})

Documents or data the agent should review:
{context['document_scope']}

Agent objective:
{context['agent_objective']}

Create this demo agreement pack as downloadable PDF files:
1. A primary executed {context['contract_type']} PDF between two fictional companies.
2. One supporting executed order form, statement of work, purchase schedule, renewal notice, amendment, or supplier summary PDF that reinforces the use case.
3. A short metadata sheet PDF with account name, counterparty, effective date, term, owner, business unit, and key commercial values.
4. Optional: one conflicting or missing term that gives the agent a useful but manageable risk to flag.

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
- Create downloadable PDF files that the SC can save and import into a demo environment.
- Use clean agreement-style formatting with title page, section headings, numbered clauses, tables where useful, and signature pages.
- Name the files with short demo-safe names, for example `Fictional_Apex_Supplier_Agreement.pdf`.
- Each PDF should include enough realistic agreement text and metadata for the AI Agent to cite.
- If Gemini cannot directly attach downloadable PDFs in this workspace, return PDF-ready document content separated by clear file names and page breaks, and state that the content should be exported to PDF.

Return format:
1. First create the downloadable signed PDF files.
2. Then provide a one-paragraph summary of the demo scenario.
3. Then list the generated PDF file names and what each file contains.
4. End with a "Golden path expected findings" section listing the exact facts the agent should be able to extract.
"""


def _render_gemini_mode_steps() -> None:
    st.markdown(
        """
        <div class="ds-steps ds-steps-compact">
            Copy the brief into Gemini, then validate the AI Agent response in Prompt Validation.
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_agreement_pack_steps() -> None:
    st.markdown(
        """
        <div class="ds-steps ds-steps-compact">
            Use this brief to create fictional source agreements when the demo environment needs better data.
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_gemini_actions(brief: str, disabled: bool) -> None:
    if disabled:
        left, right = st.columns(2)
        left.button("Copy Gemini Brief", disabled=True)
        right.button("Paste in Gemini", disabled=True)
        return

    escaped_brief = json.dumps(brief)
    gemini_url = escape(GEMINI_CHAT_URL, quote=True)
    components.html(
        f"""
        <div class="gemini-actions">
            <button id="copy-brief" type="button">Copy Gemini Brief</button>
            <a id="open-gemini" href="{gemini_url}" target="_blank" rel="noopener noreferrer">Paste in Gemini</a>
            <span id="copy-status" aria-live="polite"></span>
        </div>
        <script>
            const brief = {escaped_brief};
            const copyButton = document.getElementById("copy-brief");
            const status = document.getElementById("copy-status");
            copyButton.addEventListener("click", async () => {{
                try {{
                    await navigator.clipboard.writeText(brief);
                    status.textContent = "Copied. Open Gemini and paste into the new chat.";
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
        left.button("Copy Agreement PDF Brief", disabled=True)
        right.button("Paste in Gemini", disabled=True)
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
    if context["required_output_format"] != "JSON" and context["output_destination"] != "Workflow":
        limits = rules.get("customer_output_limits", {})
        if limits:
            st.markdown(
                "\n".join(
                    [
                        f"- Keep customer-facing responses under {limits.get('max_words', 180)} words.",
                        f"- Use no more than {limits.get('max_table_rows', 8)} table rows.",
                        "- Use readable labels, not underscore field names.",
                    ]
                )
            )
    if rules.get("allowed_values"):
        st.markdown("**Allowed result values**")
        for field_name, values in rules["allowed_values"].items():
            st.markdown(f"- {_human_label(field_name)}: {', '.join(values)}")


def _render_prompt_readiness(context: dict[str, Any], generated_prompt: str, missing_fields: list[str]) -> None:
    report = _prompt_readiness_report(context, generated_prompt, missing_fields)
    status_class = report["status"].lower().replace(" ", "-")
    passed_items = "".join(
        f'<span class="ds-readiness-chip ds-readiness-pass">{escape(item)}</span>' for item in report["passed"]
    )
    issue_items = "".join(
        f'<span class="ds-readiness-chip ds-readiness-issue">{escape(item)}</span>' for item in report["issues"]
    )
    issue_block = (
        f"""
        <div class="ds-readiness-block">
            <div class="ds-readiness-label">Needs attention</div>
            <div class="ds-readiness-chip-row">{issue_items}</div>
        </div>
        """
        if report["issues"]
        else ""
    )
    title = "Gemini Brief Readiness" if _normalize_generation_mode(context.get("generation_mode", "")) == MODE_GEMINI else "Prompt Readiness"

    st.markdown(
        f"""
        <div class="ds-readiness-panel">
            <div class="ds-readiness-header">
                <div>
                    <div class="ds-readiness-title">{escape(title)}</div>
                    <div class="ds-readiness-subtitle">Checks structure before you test the agent response.</div>
                </div>
                <div class="ds-readiness-status ds-readiness-status-{status_class}">
                    {escape(report["status"])} · {report["passed_count"]}/{report["total_count"]}
                </div>
            </div>
            <div class="ds-readiness-block">
                <div class="ds-readiness-label">Included</div>
                <div class="ds-readiness-chip-row">{passed_items}</div>
            </div>
            {issue_block}
            <div class="ds-readiness-note">This does not replace Prompt Validation testing; it confirms the prompt has the core ingredients before you run it.</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _prompt_readiness_report(context: dict[str, Any], generated_prompt: str, missing_fields: list[str]) -> dict[str, Any]:
    if missing_fields:
        return {
            "status": "Complete Intake",
            "passed": ["Prompt mode selected"],
            "issues": [f"Complete {field}" for field in missing_fields],
            "passed_count": 1,
            "total_count": 1 + len(missing_fields),
        }

    text = generated_prompt.lower()
    generation_mode = _normalize_generation_mode(context.get("generation_mode", MODE_USE_LIBRARY))
    checks: list[tuple[str, bool, str]] = []

    if generation_mode == MODE_GEMINI:
        checks.extend(
            [
                ("Customer context", _contains_any(text, ["customer and demo context", "customer context"]), "Add customer and demo context."),
                ("Selected pattern", _contains_any(text, ["selected prompt-library starting point", "selected prompt-library pattern"]), "Include the selected prompt pattern."),
                ("Required behavior", "required behavior" in text, "Add required behavior instructions."),
                ("Output requirements", _contains_any(text, ["output requirements", "return format"]), "Add output requirements."),
                ("Safety guardrails", _contains_any(text, ["do not invent", "fictional", "not found"]), "Add safety and missing-information guardrails."),
                ("Gemini return format", "return format" in text, "Tell Gemini exactly what to return."),
            ]
        )
    else:
        checks.extend(
            [
                ("Role and task", _contains_any(text, ["agent role", "your job", "agent objective"]), "Add a clear role or task."),
                ("Objective", bool(context.get("agent_objective", "").strip()) and "agent objective" in text, "Add the agent objective."),
                ("Demo context", _contains_any(text, ["customer and evaluation context", "customer/demo account", "customer context"]), "Add customer, industry, LOB, and use case context."),
                ("Source data scope", _contains_any(text, ["documents and data", "documents or data", "data sources", "documents and data reviewed"]), "Describe what data the agent reviews."),
                ("Output requirements", _contains_any(text, ["output requirements", "required columns", "return format", "required output"]), "Define the expected output."),
                ("Evidence requirement", _contains_any(text, ["evidence", "source", "quote", "cite"]), "Require source evidence."),
                ("Missing-info rule", _contains_any(text, ["not found in agreement", "not available", "source information is missing"]), "Tell the agent what to do when data is missing."),
                ("Hallucination guardrail", _contains_any(text, ["do not invent", "only available", "do not hallucinate"]), "Add an instruction not to invent facts."),
                ("Demo-ready brevity", _contains_any(text, ["concise", "demo-ready", "live demo", "under 180 words"]), "Add concise demo-ready response guidance."),
            ]
        )

    if context.get("output_destination") == "Workflow":
        checks.extend(
            [
                ("Workflow data outputs", bool(context.get("required_fields")), "Define workflow data outputs."),
                ("Decision logic", bool(context.get("decision_logic", "").strip()), "Add workflow decision logic."),
            ]
        )

    passed = [label for label, passed_check, _issue in checks if passed_check]
    issues = [issue for _label, passed_check, issue in checks if not passed_check]
    passed_count = len(passed)
    total_count = len(checks)
    readiness_ratio = passed_count / total_count if total_count else 0

    if readiness_ratio >= 0.9:
        status = "Strong"
    elif readiness_ratio >= 0.72:
        status = "Ready With Notes"
    else:
        status = "Needs Attention"

    return {
        "status": status,
        "passed": passed,
        "issues": issues,
        "passed_count": passed_count,
        "total_count": total_count,
    }


def _contains_any(text: str, phrases: list[str]) -> bool:
    return any(phrase in text for phrase in phrases)


def _qa_wizard(template_id: str, context: dict[str, Any], generated_prompt: str, rules: dict[str, Any]) -> None:
    if not _uses_renewal_validator(context):
        _generic_qa_wizard(context, generated_prompt)
        return

    _render_anchor("prompt-validation")
    _render_section_intro(
        "Prompt Validation",
        "Paste the AI Agent output from your demo environment and the app will flag formatting, quality, and demo-readiness issues.",
    )
    st.subheader("Validate Sample AI Agent Output")
    st.caption("Use this for sample agent output, not leadership briefs, planning notes, or prompt drafts.")
    passing_sample_path, failing_sample_path = _sample_paths(rules, {"context_defaults": context})
    sample_left, sample_right = st.columns(2)
    if passing_sample_path.exists() and sample_left.button("Use passing sample"):
        st.session_state.sample_output = passing_sample_path.read_text()
        st.session_state.sample_template_id = template_id
        st.session_state.selected_format = context["required_output_format"]
    if failing_sample_path.exists() and sample_right.button("Use failing sample"):
        st.session_state.sample_output = failing_sample_path.read_text()
        st.session_state.sample_template_id = template_id
        st.session_state.selected_format = context["required_output_format"]

    sample_output = _default_sample_text(template_id, passing_sample_path)

    output_format = st.selectbox(
        "Sample output format",
        ALLOWED_OUTPUT_FORMATS,
        index=_safe_index(ALLOWED_OUTPUT_FORMATS, st.session_state.get("selected_format", context["required_output_format"])),
        key="selected_format",
    )
    sample_output = st.text_area("Paste sample AI Agent output", value=sample_output, height=340)

    if st.button("Validate Output", type="primary"):
        structural = validate_output(sample_output, output_format, rules)
        quality = score_business_quality(
            structural.parsed_output,
            sample_output,
            context["audience"],
            context["output_destination"],
            rules,
        )
        recommendations = recommend_prompt_improvements(
            structural.issues,
            quality.issues,
            context["audience"],
            context["output_destination"],
        )
        optimized_prompt = build_optimized_prompt(generated_prompt, recommendations, context["output_destination"])
        overall_status = determine_status(structural.passed, quality.average_score)

        st.session_state.validation_report = {
            "overall_status": overall_status,
            "structural": structural,
            "quality": quality,
            "recommendations": recommendations,
            "optimized_prompt": optimized_prompt,
            "template_id": template_id,
        }

    report = st.session_state.get("validation_report")
    if report and report.get("template_id") == template_id:
        _render_report(report)


def _uses_renewal_validator(context: dict[str, Any]) -> bool:
    title = context.get("library_title", "").lower()
    prompt_type = context.get("library_prompt_type", "")
    return prompt_type == "AI Agent" and "renewal" in title


def _generic_qa_wizard(context: dict[str, Any], generated_prompt: str) -> None:
    _render_anchor("prompt-validation")
    _render_section_intro(
        "Prompt Validation",
        "Paste the AI Agent output from your demo environment and the app will flag formatting, quality, and demo-readiness issues.",
    )
    st.subheader("Review Sample AI Agent Output")
    st.info(
        "This selected library pattern uses the general demo-readiness QA check. Renewal intelligence prompts still use the deeper field-by-field validator."
    )
    st.caption("Use this for sample agent output, not leadership briefs, planning notes, or prompt drafts.")
    sample_output = st.text_area("Paste sample AI Agent output", value="", height=340)

    if st.button("Validate Output", type="primary", key="generic_validate_output"):
        st.session_state.generic_validation_report = _generic_quality_report(sample_output, context, generated_prompt)

    report = st.session_state.get("generic_validation_report")
    if report:
        _render_generic_report(report)


def _generic_quality_report(sample_output: str, context: dict[str, Any], generated_prompt: str) -> dict[str, Any]:
    text = sample_output.strip()
    issues: list[str] = []
    strengths: list[str] = []
    recommendations: list[str] = []
    word_count = len(text.split())

    if not text:
        issues.append("No sample output was provided.")
    if text and word_count < 35:
        issues.append("The sample output is too thin for a customer-facing demo.")
    if word_count > 260 and context["output_destination"] == "Chat":
        issues.append("The sample output may be too long to speak to comfortably in a live demo.")
    if context["output_destination"] == "Chat" and text.startswith("{"):
        issues.append("The sample output appears to be raw JSON, which is not ideal for a chat demo.")
    if context["output_destination"] not in {"Workflow", "Salesforce"} and "_" in text:
        issues.append("The sample output includes underscore-style field names; use readable labels for customer-facing demos.")
    if text and not any(keyword in text.lower() for keyword in ["action", "next step", "recommend", "route", "owner"]):
        issues.append("The sample output should include a clear next action, owner, or recommendation.")
    if text and not any(keyword in text.lower() for keyword in ["agreement", "contract", "source", "evidence", "clause", "data"]):
        issues.append("The sample output should make the source data or agreement evidence clearer.")

    data_outputs = context.get("required_fields", [])
    if text and data_outputs:
        visible_output_count = sum(1 for field in data_outputs[:8] if field.lower().replace("_", " ") in text.lower())
        if visible_output_count == 0:
            issues.append("The sample output does not clearly reflect the selected Data Outputs.")
        else:
            strengths.append("The output reflects at least one selected Data Output.")

    if text and not issues:
        strengths.append("The output is concise, structured, and demo-ready.")
    if text and word_count <= 260:
        strengths.append("The output length is reasonable for a live demo.")

    if issues:
        recommendations = [
            "Tighten the prompt so the response is concise, structured, and easy to explain live.",
            "Explicitly ask for readable labels and no underscore-style field names for customer-facing output.",
            "Require one next action, owner, or recommendation.",
            "Ask the agent to cite the agreement, record, clause, or data source it used.",
        ]
    else:
        recommendations = ["No major changes needed. Retest once with the final demo source data."]

    if not text:
        status = "FAIL"
    elif len(issues) <= 1:
        status = "PASS"
    elif len(issues) <= 3:
        status = "NEEDS OPTIMIZATION"
    else:
        status = "FAIL"

    optimized_prompt = build_optimized_prompt(generated_prompt, recommendations, context["output_destination"])
    return {
        "status": status,
        "issues": issues,
        "strengths": strengths,
        "recommendations": recommendations,
        "optimized_prompt": optimized_prompt,
    }


def _render_generic_report(report: dict[str, Any]) -> None:
    status = report["status"]
    status_type = {"PASS": "success", "NEEDS OPTIMIZATION": "warning", "FAIL": "error"}[status]
    getattr(st, status_type)(f"Overall status: {status}")

    col1, col2 = st.columns(2)
    col1.metric("Demo-readiness checks", "Pass" if status == "PASS" else "Review")
    col2.metric("Issue count", len(report["issues"]))

    if report["strengths"]:
        st.subheader("What Looks Good")
        for strength in report["strengths"]:
            st.markdown(f"- {strength}")

    if report["issues"]:
        st.subheader("Issues To Fix")
        for issue in report["issues"]:
            st.markdown(f"- {issue}")

    st.subheader("Recommended Prompt Improvements")
    for recommendation in report["recommendations"]:
        st.markdown(f"- {recommendation}")

    st.subheader("Optimized Prompt Version")
    st.text_area("Optimized prompt", value=report["optimized_prompt"], height=420, label_visibility="collapsed")


def _render_report(report: dict[str, Any]) -> None:
    status = report["overall_status"]
    structural = report["structural"]
    quality = report["quality"]
    recommendations = report["recommendations"]
    optimized_prompt = report["optimized_prompt"]

    status_type = {"PASS": "success", "NEEDS OPTIMIZATION": "warning", "FAIL": "error"}[status]
    getattr(st, status_type)(f"Overall status: {status}")

    col1, col2, col3 = st.columns(3)
    col1.metric("Structural checks", "Pass" if structural.passed else "Fail")
    col2.metric("Business-quality score", f"{quality.average_score} / 5")
    col3.metric("Issue count", len(structural.issues) + len(quality.issues))

    st.subheader("Structural Validation Results")
    if structural.issues:
        for issue in structural.issues:
            icon = "Error" if issue.severity == "error" else "Warning"
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

    st.subheader("Optimized Prompt Version")
    st.text_area("Optimized prompt", value=optimized_prompt, height=420, label_visibility="collapsed")

    st.subheader("Retest Checklist")
    for item in _retest_checklist():
        st.checkbox(item, value=False)


def build_prompt(context: dict[str, Any], template: dict[str, Any], rules: dict[str, Any]) -> str:
    if context.get("library_title"):
        return _build_library_prompt(context, rules)

    if context["required_output_format"] != "JSON" and context["output_destination"] != "Workflow":
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

    return f"""Agent role:
You specialize in {template['name']} use cases where completed agreement language must be converted into structured, actionable business intelligence.

Agent objective:
{context['agent_objective']}

Customer context:
- Customer: {context['customer_name']}
- Industry: {context['industry']}
- Customer / evaluation use case: {context['use_case']}
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
- If a required term is not found, return "Not found in agreement."
- Return all required fields even when some values are missing.

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
- Use extracted agreement language where available.
- If source language is missing, return "Not found in agreement."

Instructions for missing information:
- Keep the field present.
- Use "Not found in agreement" for missing agreement language.
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
    source_prompt = context.get("library_prompt", "").strip()
    source_prompt_section = source_prompt or "No full source prompt is listed in the library. Use the agent title, description, objective, and data outputs below as the operating pattern."

    return f"""Agent role:
You are the {context['library_title']} for Docusign IAM.

Selected library pattern:
- Category: {context['library_category']}
- Agent title: {context['library_title']}
- What the agent does: {context['library_description'] or 'Not provided'}

Customer and evaluation context:
- Customer: {context['customer_name']}
- Industry: {context['industry']}
- LOB: {context['audience']}
- Customer / evaluation use case: {context['use_case']}
- Agreement type: {context['contract_type']}
- Agent destination: {context['output_destination']}

Documents and data reviewed:
{context['document_scope']}

Agent objective:
{context['agent_objective']}

Operating pattern from the prompt library:
{source_prompt_section}

Required agent behavior:
- Use the operating pattern above, but tailor the response to the customer and evaluation context.
- Produce a concise, deterministic, well-formatted response that is easy to speak to in a live demo.
- Prioritize the user-requested business outcome over exhaustive analysis.
- Use customer-facing language unless the destination is Workflow or Salesforce.
- Do not invent agreement terms, account data, commercial values, source evidence, or business facts.
- If source information is missing, write "Not found in agreement" or "Not available in the provided data."

Output requirements:
{_library_output_style_instructions(context)}

Data outputs or visible fields:
{_required_fields_text(context, rules)}

Decision logic:
{context['decision_logic']}
"""


def _build_customer_demo_prompt(context: dict[str, Any], template: dict[str, Any], rules: dict[str, Any]) -> str:
    allowed_values = _allowed_values_text(rules, context)
    visible_fields = _required_fields_text(context, rules)
    example_output = _example_output_text(context, template)

    return f"""Agent role:
You specialize in completed agreement renewal intelligence. Turn agreement evidence into a concise, customer-facing renewal brief.

Agent objective:
{context['agent_objective']}

LOB:
{context['audience']}

Documents and data reviewed:
{context['document_scope']}

Required response contract:
- Return a demo brief, not a full analysis report.
- Use exactly this structure: title, one-line verdict, compact table, why it matters, evidence, recommended action.
- Keep the entire response under 180 words.
- Include at most 8 table rows, 1 risk, 1 evidence quote, and 1 recommended action.
- Use customer-facing labels such as `Agreement name`; do not show underscores.
- Do not return JSON unless the user explicitly asks for JSON.
- Make every sentence speakable in a live demo.

Visible fields:
{visible_fields}

Allowed values:
{allowed_values}

Evidence and hallucination rules:
- Do not invent contract terms, dates, notice windows, renewal rights, uplift terms, or commercial values.
- Use extracted agreement language where available.
- Quote only the shortest useful evidence excerpt.
- If a required term is missing, write "Not found in agreement."

Example output:
{example_output}
"""


def build_expected_schema(rules: dict[str, Any]) -> dict[str, Any]:
    if rules.get("expected_schema"):
        return rules["expected_schema"]
    return {field_name: "string" for field_name in rules["required_fields"]}


def determine_status(structural_passed: bool, quality_score: float) -> str:
    if not structural_passed:
        return "FAIL"
    if quality_score >= 4:
        return "PASS"
    return "NEEDS OPTIMIZATION"


def _load_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text())


def _load_rules(template_id: str, template: dict[str, Any]) -> dict[str, Any]:
    rules_path = RULES_DIR / f"{template_id}_rules.yaml"
    if rules_path.exists():
        return _load_yaml(rules_path)
    return {
        "template_id": template_id,
        "required_fields": template.get("default_required_fields", []),
        "allowed_values": {},
        "missing_information_phrase": "Not found in agreement",
        "sample_files": {},
    }


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
    entries: list[dict[str, Any]] = []
    current_category = ""
    for row in rows[1:]:
        category = _clean_library_category(_cell(row, 0) or current_category)
        current_category = category or current_category
        title = _cell(row, 1)
        if not title:
            continue
        entries.append(
            {
                "prompt_type": "AI Agent",
                "source_sheet": "AI Agents",
                "category": category or "General",
                "title": title,
                "description": _cell(row, 2),
                "prompt": _cell(row, 3),
                "data_outputs": _cell(row, 4),
                "link_to_assets": _cell(row, 5),
            }
        )
    return entries


def _cell(row: list[str], index: int) -> str:
    return row[index].strip() if index < len(row) and isinstance(row[index], str) else ""


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
    if context["required_output_format"] == "JSON" or context["output_destination"] == "Workflow":
        return "\n".join(
            [
                "- Return strict JSON only when this prompt is used for workflow automation.",
                "- Use exact field names and allowed values when workflow variables are configured.",
                "- Do not wrap JSON in markdown fences.",
            ]
        )
    return "\n".join(
        [
            "- Follow the output format specified in the selected prompt-library pattern.",
            "- If the library pattern does not specify a format, use a concise business-ready summary or table.",
            "- Do not return raw JSON unless the user, Workflow, or Salesforce destination requires it.",
            "- Use readable labels; do not show underscores in table titles, chart labels, headings, or field labels.",
            "- Keep the response focused enough for an SC to explain in a live demo.",
        ]
    )


def _output_style_instructions(context: dict[str, Any]) -> str:
    output_format = context["required_output_format"]
    destination = context["output_destination"]
    if output_format == "JSON" or destination == "Workflow":
        return "\n".join(
            [
                "- Return strict JSON only.",
                "- Do not wrap JSON in markdown fences.",
                "- Use exact field names and allowed values.",
            ]
        )
    if output_format == "Markdown Table":
        return "\n".join(
            [
                "- Do not return raw JSON.",
                "- Produce a demo brief, not a full analysis report.",
                "- Keep the response under 180 words.",
                "- Use exactly this structure: title, one-line verdict, compact table, why it matters, evidence, recommended action.",
                "- Include at most 8 table rows, 1 risk, 1 evidence quote, and 1 recommended action.",
                "- Use customer-facing labels such as `Agreement name`.",
                "- Do not show underscores in table titles, chart labels, headings, or field labels.",
                "- Make every sentence speakable in a live demo.",
            ]
        )
    if output_format == "Plain Language Summary" or destination == "Chat":
        return "\n".join(
            [
                "- Do not return raw JSON.",
                "- Produce a demo brief, not a full analysis report.",
                "- Keep the response under 180 words.",
                "- Use exactly this structure: title, one-line verdict, compact table, why it matters, evidence, recommended action.",
                "- Include at most 8 table rows, 1 risk, 1 evidence quote, and 1 recommended action.",
                "- Use customer-facing labels such as `Agreement name`.",
                "- Do not show underscores in table titles, chart labels, headings, or field labels.",
                "- Lead with the renewal status, date pressure, and action.",
                "- Quote only the shortest evidence excerpt needed to support the finding.",
                "- Make every sentence speakable in a live demo.",
            ]
        )
    return "- Return a clear, structured response that covers every required field."


def _example_output_text(context: dict[str, Any], template: dict[str, Any]) -> str:
    if context["required_output_format"] == "JSON" or context["output_destination"] == "Workflow":
        return "```json\n" + json.dumps(template["good_output_example"], indent=2) + "\n```"
    chat_example = template.get("chat_output_example")
    if chat_example:
        return chat_example.rstrip()
    return json.dumps(template["good_output_example"], indent=2)


def _recommended_action_instruction(context: dict[str, Any]) -> str:
    if context["required_output_format"] == "JSON" or context["output_destination"] == "Workflow":
        return "- Include action, owner, rationale, and next_step in recommended_action."
    return "- Include a recommended action with an action, owner, rationale, and next step."


def _required_fields_text(context: dict[str, Any], rules: dict[str, Any]) -> str:
    if context.get("library_data_outputs"):
        return "\n".join(f"- {_display_field_name(field, context)}" for field in context["required_fields"])
    if context["required_output_format"] == "JSON" or context["output_destination"] == "Workflow":
        return "\n".join(f"- {field}" for field in context["required_fields"])
    customer_fields = rules.get("customer_facing_required_fields", context["required_fields"])
    return "\n".join(f"- {_human_label(field)}" for field in customer_fields)


def _display_field_name(field_name: str, context: dict[str, Any]) -> str:
    if context["required_output_format"] == "JSON" or context["output_destination"] == "Workflow":
        return field_name
    return _human_label(field_name)


def _human_label(field_name: str) -> str:
    if "_" not in field_name:
        return field_name
    acronyms = {"arr": "ARR"}
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
