# Live Demo Runbook: Renewal Leverage Agent

## Demo Story

Northstar Health is preparing for a supplier renewal with Apex Analytics. The account team wants to know whether there is renewal leverage, whether Legal needs to review risky terms, and what workflow variables should drive routing.

## Prompt Builder Intake

- Prompt Mode: Use Prompt Library
- Business area: All categories
- Agent prompt: Renewal Leverage Agent
- Customer: Northstar Health
- LOB: Sales
- Agreement type: Supplier Subscription Agreement
- Industry: Healthcare
- Customer Use Case: Identify high-risk renewal terms and route the agreement to Legal when risk is high.
- Documents/data reviewed: Apex Analytics Master Subscription Agreement, Order Form, Renewal Amendment, vendor summary, and account metadata.
- Agent objective: Identify renewal timing, risky renewal terms, evidence, workflow-safe routing variables, and the next owner action.
- Agent Destination: Chat and Workflow

## Workflow Data Outputs

Use these in the app's Data Outputs field if you want to show workflow routing clearly:

```text
Risk_Level
Risk_Term_Count
Legal_Review_Required
Renewal_Notice_Status
Notice_Deadline
Recommended_Route
Recommended_Owner
Next_Action
```

## Workflow Routing Logic

```text
Risk_Level = High when Risk_Term_Count is 3 or more.
Legal_Review_Required = Yes when Risk_Level = High or when auto-renewal, liability, termination, or pricing language creates negotiation risk.
Recommended_Route = Legal Review when Legal_Review_Required = Yes.
Recommended_Route = Account Team Follow-up when Risk_Level is Medium and Legal_Review_Required = No.
Renewal_Notice_Status = Open when the notice deadline has not passed.
Renewal_Notice_Status = Missed when the notice deadline has passed.
```

## Recommended Demo Sequence

1. Select `Renewal Leverage Agent`.
2. Enter the intake details above.
3. Check both `Chat` and `Workflow`.
4. Paste the Workflow Data Outputs and Routing Logic above.
5. Generate the optimized prompt.
6. Show that the app generates both a customer-ready agent prompt and workflow-safe variables.
7. Use `expected_agent_response_app.md` to show the kind of response the app is designed to produce.
8. Contrast it with `free_form_llm_prompt.md`, which is intentionally shorter and less governed.

