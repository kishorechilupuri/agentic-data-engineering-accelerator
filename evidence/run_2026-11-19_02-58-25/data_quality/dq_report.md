# Data quality report

Rules authored: **6**

## `customer`

Rows: **12** | evaluated: **6** | passed: **4** | failed: **2** | status: **fail**

| Rule | Column | Check | Severity | Status | Detail |
| --- | --- | --- | --- | --- | --- |
| DQ-001 | `CustomerID` | not_null | error | **pass** | 0 null/blank values. |
| DQ-002 | `CustomerID` | unique | error | **fail** | 1 duplicate values. |
| DQ-003 | `CustomerID` | row_count_min | error | **pass** | row_count=12, minimum=1. |
| DQ-004 | `Email` | regex | warn | **fail** | 1 values not matching '^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$'. |
| DQ-005 | `CreatedDate` | date_format | warn | **pass** | 0 values not parseable as %Y-%m-%d %H:%M:%S. |
| DQ-006 | `ModifiedDate` | date_format | warn | **pass** | 0 values not parseable as %Y-%m-%d %H:%M:%S. |

