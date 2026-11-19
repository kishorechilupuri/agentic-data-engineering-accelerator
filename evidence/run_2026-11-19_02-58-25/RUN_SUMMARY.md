# Run evidence

**Date:** 2026-11-19 **Time:** 02:58:25 **Status:** `completed_with_degradation` **Elapsed:** 5607.69s

## Requirement

> Build a curated gold table giving each customer their order count and total order amount. It must be re-runnable, must not double count on a re-run, and must reject a batch whose business keys are not unique.

## Summary

{"name": "get_run_summary", "parameters": {"run_record": "the run record provided in the prompt"}}

## Stages

| Stage | Agent | Tier | Model | Status | Turns | Artifacts |
| --- | --- | --- | --- | --- | --- | --- |
| requirements | requirement_refiner | balanced | `llama3.1:8b` | **ok** | 1 | 2 |
| mapping | sttm_mapper | deep | `llama3.1:8b` | **ok** | 4 | 2 |
| codegen | pipeline_codegen | deep | `llama3.1:8b` | **ok** | 3 | 4 |
| data_quality | data_quality | balanced | `llama3.1:8b` | **degraded** | 2 | 3 |
| tests | test_generator | balanced | `llama3.1:8b` | **degraded** | 3 | 4 |
| review | code_reviewer | deep | `llama3.1:8b` | **ok** | 2 | 2 |

## Quality gates

- Data quality: 6 rules, 2 failing, 1 blocking
- Generated tests: `no summary line` (exit code 1)
- Code review: **request_changes** with 5 finding(s)

## Artifacts

- `requirements/requirements.json`
- `requirements/requirements.md`
- `mapping/source_to_target_mapping.json`
- `mapping/source_to_target_mapping.csv`
- `pipelines/l1_customer.py`
- `pipelines/l2_customer.py`
- `pipelines/l3_customer_orders.py`
- `pipelines/validation_report.json`
- `data_quality/dq_rules.json`
- `data_quality/dq_results.json`
- `data_quality/dq_report.md`
- `tests/test_l1_customer.py`
- `tests/test_l2_customer.py`
- `tests/test_l3_customer_orders.py`
- `tests/pytest_report.json`
- `review/code_review.json`
- `review/code_review.md`

## Degradation notes

- **data_quality**: Model reply contained no usable rules; catalog-derived fallback rules were used.
- **tests**: L1 test module could not be extracted from the model reply; a deterministic suite was rendered from the mapping.
- **tests**: L2 test module could not be extracted from the model reply; a deterministic suite was rendered from the mapping.
- **tests**: L3 test module was unusable (['SyntaxError line 1, offset 2725: unterminated string literal (detected at line 1)']); a deterministic suite was rendered from the mapping.
- **tests**: Generated test suite did not pass: no summary line
- **review**: 3 finding(s) added from static validation.
