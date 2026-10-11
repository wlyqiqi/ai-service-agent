# Functional Scenarios LangSmith Report

- Dataset: `ai-cs-functional-scenarios-v1`
- Split: `full`
- Result: **53/56**
- Experiment: https://smith.langchain.com/o/7b7ef818-afd9-42a4-84cc-0f331bfd835f/datasets/7cbce67c-2aca-4931-b470-ed7b4b0f30c6/compare?selectedSessions=f2eddb1a-a06b-4ab0-8c6a-c7cc3fa20836

| ID | Suite | Status | Problems |
| --- | --- | --- | --- |
| S01 | guard | pass |  |
| S02 | guard | pass |  |
| S03 | guard | pass |  |
| S04 | guard | pass |  |
| S05 | read | pass |  |
| S06 | read | fail | event missing: tool_call; event missing: tool_result; tools expected=['order_lookup'] actual=[] |
| S07 | read | pass |  |
| S08 | read | pass |  |
| S09 | read | pass |  |
| S10 | read | pass |  |
| S11 | read | pass |  |
| S12 | read | pass |  |
| S13 | read | pass |  |
| S14 | read | pass |  |
| S15 | mutation | pass |  |
| S16 | mutation | pass |  |
| S17 | mutation | pass |  |
| S18 | mutation | pass |  |
| S19 | mutation | pass |  |
| S20 | mutation | pass |  |
| S21 | mutation | pass |  |
| S22 | mutation | pass |  |
| S23 | mutation | pass |  |
| S24 | mutation | pass |  |
| S25 | mutation | pass |  |
| S26 | mutation | pass |  |
| S27 | high_risk | pass |  |
| S28 | high_risk | pass |  |
| S29 | high_risk | pass |  |
| S30 | high_risk | pass |  |
| S31 | high_risk | pass |  |
| S32 | high_risk | pass |  |
| S33 | high_risk | fail | event missing: clarification_required |
| S34 | high_risk | pass |  |
| S35 | high_risk | pass |  |
| S36 | high_risk | pass |  |
| S37 | fallback | pass |  |
| S38 | fallback | pass |  |
| S39 | fallback | pass |  |
| S40 | handoff | pass |  |
| S41 | handoff | pass |  |
| S42 | handoff | pass |  |
| S43 | handoff | pass |  |
| S44 | handoff | pass |  |
| S45 | handoff | pass |  |
| S46 | handoff | pass |  |
| S47 | handoff | pass |  |
| S48 | multiturn | pass |  |
| S49 | multiturn | pass |  |
| S50 | multiturn | pass |  |
| S51 | multiturn | pass |  |
| S52 | multiturn | pass |  |
| S53 | context | pass |  |
| S54 | context | fail | event missing: clarification_required |
| S55 | context | pass |  |
| S56 | context | pass |  |
