# Real Model LangSmith Baseline: HighRisk Decoupling

## Result

- Experiment: `real-model-decoupled-20261010-133346-54cd71e0`
- Dataset: `ai-service-agent-real-model-eval-v1`
- Cases: 40
- Passed: 38
- Failed: 2
- Experiment URL: https://smith.langchain.com/o/7b7ef818-afd9-42a4-84cc-0f331bfd835f/datasets/4fc94b31-6919-420c-8d21-9cf53308b39b/compare?selectedSessions=37adde5b-4ba0-4d36-b4aa-ed704efe85ae

## Failures And Recheck

| Case | First Run | Recheck | Conclusion |
| --- | --- | --- | --- |
| `REAL-MUTATION-006` | Missing `clarification_required` | Local recheck passed | Non-stable real-model output; no code regression found |
| `REAL-ROUTE-004` | `faq` routed to `unknown` / fallback | Local recheck passed | Non-stable router fallback; no code regression found |

## Harness Isolation

- LangSmith runner now creates a clean SQLite snapshot after server startup.
- Every graph and handoff case restores that snapshot before execution.
- This prevents mutation and high-risk cases from leaking order state into later cases.

## Regression

- Full pytest: 624 passed, 2 skipped.
- Previously failing HighRisk/Mutation cases are now resolved.
