# DocuSign IAM Agent Prompt Builder

A lightweight internal Streamlit app for DocuSign Solution Consultants who need to create, validate, and optimize DocuSign IAM-style AI Agent prompts for demos, POCs, workflow design, and customer-specific solution storytelling.

The MVP includes a bundled AI Agent prompt library from `data/agent_worksheet_prompts.xlsx`, plus demo examples. Prompt Validation now works across the library: it derives field-level checks from the selected prompt pattern, Data Outputs, sample output format, and destination, while renewal-intelligence prompts still use the deepest template-specific rule path.

## What The App Does

- Lets SCs search and filter the Prompt Library by business area before selecting an AI Agent pattern.
- Generates a customer-specific optimized prompt from the selected library pattern and SC-entered demo context.
- Applies deterministic optimization guardrails before the SC tests the prompt in the demo environment.
- Checks generated prompts with a built-in Optimized Prompt Readiness panel before QA testing.
- Applies the expected output rules and allowed values behind the scenes.
- Accepts pasted sample AI Agent output in JSON, markdown table, HTML table, workflow variables, or plain language.
- Validates prompt outputs across library patterns using dynamic required fields, selected output format, destination-specific formatting rules, built-in expected-output fallbacks, and demo-readiness checks.
- Preserves deeper renewal-intelligence validation for required fields, allowed values, renewal risk details, commercial summary fields, and recommended action details.
- Scores business quality from 1 to 5 across specificity, actionability, audience fit, workflow readiness, evidence quality, risk clarity, formatting clarity, and demo usefulness.
- Applies audience-aware length limits: 150 words for leadership/executive summaries, 180 words for chat demos, no prose word limit for Workflow-only structured output, and 300 words for internal analysis/debugging.
- Returns PASS or FAIL with issue details, recommended prompt improvements, an optional prompt tune-up, and a retest checklist.

## Install Dependencies

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run The Streamlit App

```bash
streamlit run streamlit_app.py
```

Then open the local URL Streamlit prints, usually `http://localhost:8501`.

## Run Or Publish In Replit

Upload the full project zip and run the project as-is. Do not ask Replit Agent to rebuild or recreate the app, because that can create a lookalike page that does not use the Streamlit app logic or branded banner.

The included Replit startup files run:

```bash
python main.py
```

If you set a custom Run command, use:

```bash
streamlit run streamlit_app.py --server.address 0.0.0.0 --server.port 8501
```

When the correct app is running, the page shows the Docusign IAM sidebar, the colorful branded banner, blank Prompt Intake Form fields, and an empty generated prompt area until required fields are completed.

## Publish On Streamlit Community Cloud

Use `streamlit_app.py` as the main file path. The root `streamlit_app.py` is the full app entry point, so Streamlit Cloud does not need to import an `app/` package.

Keep the full project structure together so the bundled workbook, YAML files, validators, samples, and branded stylesheet are available at runtime.

The app does not require secrets for the current template-based, prompt-library, Gemini-brief, QA, or agreement-pack workflows.

The app can resolve files from either the included folder structure or a flatter Streamlit repo. If you use a flat repo, keep `validators/` as a lowercase folder next to `streamlit_app.py`, and place the workbook, prompt template, rule YAML files, sample files, and CSS file beside the app file or in their original folders.

Admins can update the prompt library from the sidebar by opening **Admin: Prompt Library**, pasting an accessible Google Sheets or Drive-hosted XLSX link, and clicking **Refresh Library**. For a persistent hosted source, set `PROMPT_LIBRARY_URL` in Streamlit Community Cloud secrets. Private Google files require a published/exportable link or approved Google authentication.

## How SCs Should Use It

1. Choose a **Prompt Mode**.
2. If using **Use Prompt Library**, choose the AI Agent prompt pattern from the **Prompt Library**.
3. Complete the **Prompt Intake Form**: customer, LOB, agreement type, industry, customer use case, documents/data reviewed, objective, and agent destination.
4. Click **Generate Optimized Prompt** or **Generate Gemini Brief**.
5. Review the **Optimized Prompt Readiness** panel to confirm the prompt includes the core ingredients.
6. Copy the optimized prompt or Gemini Agent Brief into the right workspace.
7. Use **Agreement Builder** when it appears for agreement-heavy patterns that need a Gemini-ready brief for downloadable mock signed PDF agreements.
8. Upload the generated mock agreement PDFs into the demo environment, then run the AI Agent prompt.
9. Paste the AI Agent output into **Prompt Validation** and review the validation result, issue list, quality scores, prompt improvements, optional prompt tune-up, and retest checklist.

Use **Prompt History** in the sidebar to start a new prompt, save the current generated prompt or Gemini brief, reload recent prompts from the current session, or clear the session history. History is local to the active app session and is not a long-term storage system.

## Generation Modes

**Use Prompt Library** adapts the selected AI Agent pattern from the bundled workbook and applies the app's deterministic optimization guardrails up front.

**Customize Existing Prompt** lets SCs paste a prompt that already works well, then adapts and optimizes it for the selected customer, industry, LOB, customer use case, destination, documents/data, and objective.

**Generate Gemini Brief** creates a copy-ready Gemini Brief from the selected prompt-library pattern and intake context. The app does not call Gemini directly or store Gemini credentials. SCs copy the brief, open Gemini, paste it into their approved enterprise Gemini workspace, then run the generated prompt and validate the sample output in Prompt Validation.

For Workflow agents, the form shows generated **Data Outputs** and **Workflow Routing Logic** so SCs can define workflow-safe variables and routing behavior. These fields stay hidden when only Chat is selected.

In **Agent Destination**, select **Chat**, **Workflow**, or both. Use **Chat** for Iris or another chat assistant. Use **Workflow** when the output needs strict variables or routing logic. Select both when the same agent needs a speakable chat response plus workflow-safe Data Outputs such as `Risk_Level = High`.

## Prompt Library

The **Prompt Library** reads AI Agent prompt patterns from `data/agent_worksheet_prompts.xlsx`. Users can filter by business area and search by title, category, description, prompt text, or Data Outputs.

Admins can also point the app to a living Google Sheet or Drive-hosted workbook from the sidebar. The bundled Excel file remains the fallback so demos still work if the external source is unavailable.

When **Use Prompt Library** is selected, SCs can select:

- Agent prompt

The selected library pattern becomes the starting point for the generated prompt. The **Customer Use Case** field is intentionally separate; it captures the broader customer scenario, while the selected library pattern captures the specific agent behavior.

The library is bundled with the app so SCs do not need to manage or upload a separate spreadsheet.

## Agreement Builder

The **Agreement Builder** creates a copy-ready brief for Gemini or another approved document-generation workspace. It asks Gemini to create downloadable mock, demo-only signed PDFs and supporting metadata that align to the selected prompt-library pattern, industry, LOB, customer use case, agreement type, source documents, and agent objective.

Use this when the demo environment needs stronger source data before running the AI Agent prompt. The generated source documents should be marked demo-only, should use mock typed signatures only, and should never contain real customer data.

Use the included passing and failing samples to see the validation behavior quickly.

## How To Add New AI Agent Templates

Add a new entry in `prompts/prompt_templates.yaml` with:

- `name`
- `status`
- `description`
- required fields
- example good output
- validation checklist

Set `status: active` only after rules and validation behavior are ready. Stubbed templates can stay in the file with `status: stub`.

## How To Add New Validation Rules

Prompt Validation can run without a custom rules file by deriving required fields from the selected prompt pattern's Data Outputs. Create or extend a YAML file in `rules/` when a prompt pattern needs deeper domain-specific validation. The renewal intelligence MVP uses `rules/completed_agreements_renewal_intelligence_rules.yaml`, which defines:

- required fields
- allowed values
- renewal status definitions
- array requirements
- required child fields
- missing-information phrase
- generic action phrases
- hallucination watch terms
- business-quality categories

For a new template, add a rules file and update the Streamlit app to load that rules file when the template is active.

## How To Interpret Results

- **PASS**: All required structural checks pass, no validation issues are found, and the business-quality score is 4 or higher.
- **FAIL**: Required fields are missing, values are unsupported, formatting is invalid, evidence is missing, hallucination risk is detected, the response is too long for a live demo, or the output is not compatible with the selected destination.

## Demo-Safe Data Only

Use demo-safe or sanitized data only. Do not paste confidential customer agreements, credentials, or sensitive data unless operating in an approved company environment.

## Project Structure

```text
streamlit_app.py
app/streamlit_app.py
assets/app.css
main.py
.replit
.streamlit/config.toml
prompts/prompt_templates.yaml
data/agent_worksheet_prompts.xlsx
rules/completed_agreements_renewal_intelligence_rules.yaml
samples/completed_agreements_renewal_passing_output.json
samples/completed_agreements_renewal_failing_output.json
samples/completed_agreements_renewal_passing_output.md
samples/completed_agreements_renewal_failing_output.md
validators/validate_structure.py
validators/validate_quality.py
validators/optimize_prompt.py
reports/sample_validation_report.html
README.md
```
