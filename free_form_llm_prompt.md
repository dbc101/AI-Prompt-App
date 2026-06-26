# Free-Form LLM Prompt For Comparison

Paste this into Gemini or another LLM to contrast a normal chat-based prompt creation experience with the guided Prompt Builder app.

```text
Create a Docusign IAM agent prompt for a healthcare customer named Northstar Health. The agent should review a supplier subscription agreement, order form, renewal amendment, vendor summary, and account metadata for an upcoming renewal with Apex Analytics.

The customer use case is to identify high-risk renewal terms and route the agreement to Legal when risk is high. The agent should summarize renewal timing, risky terms, evidence from the agreement, and recommended next steps. It should also create workflow variables like Risk_Level, Risk_Term_Count, Legal_Review_Required, Renewal_Notice_Status, Notice_Deadline, Recommended_Route, Recommended_Owner, and Next_Action.

Make the prompt good for both chat and workflow automation.
```

## Why This Is A Useful Contrast

This free-form prompt is intentionally reasonable, but it leaves important details implicit:

- It does not force an exact output contract.
- It does not define routing thresholds such as `Risk_Level = High when Risk_Term_Count is 3 or more`.
- It does not enforce missing-information behavior.
- It does not clearly separate chat response from workflow variables.
- It relies on the LLM to infer quality guardrails instead of applying them consistently.

