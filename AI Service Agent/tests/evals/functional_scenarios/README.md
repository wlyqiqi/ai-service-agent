# Functional scenarios (v2)

- **Local file:** `master.jsonl` (exactly **60** cases, ids `F01`–`F60`)
- **LangSmith dataset:** `ai-cs-functional-scenarios-v2`
- **Legacy:** `ai-cs-functional-scenarios-v1` (S01–S56, pre–Form-Interrupt expectations) — keep for history only; do not use for new evals

## Suite distribution

| Suite | Count | Focus |
|---|---:|---|
| guard | 5 | jailbreak / spoof / bypass |
| read | 10 | order/shipment tools + FAQ |
| mutation | 14 | Form Interrupt / Resume / Rejected / Operation Clarify |
| high_risk | 10 | approve / reject / expire / clarify |
| fallback | 4 | unsupported + limit→handoff |
| handoff | 8 | create / human context / resolve |
| multiturn | 5 | cross-intent turns |
| context | 4 | padded history probes |

## Commands

```powershell
# rebuild local jsonl
uv run python scripts/build_functional_scenarios_dataset.py

# upload NEW LangSmith dataset (default name v2; does not touch v1)
uv run python scripts/upload_functional_scenarios_dataset.py

# run eval against v2
uv run python scripts/functional_langsmith_eval.py --split smoke
uv run python scripts/functional_langsmith_eval.py --split full
```
