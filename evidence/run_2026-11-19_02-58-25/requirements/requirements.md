# Customer Order Count and Total

Create a curated gold table with customer order count and total order amount, using the Sales.Customer table as the source.

## Source tables

- `Sales.Customer`

## Target entities

- **L3** `customer_orders` -- one row per customer account

## Functional requirements

### FR-001 (L3)

Create a gold table with customer order count and total order amount.

- [ ] The table contains all customers from the Sales.Customer table.
- [ ] The table contains the correct order count and total order amount for each customer.

### FR-002 (L3)

The pipeline must be re-runnable and reject a batch whose business keys are not unique.

- [ ] The pipeline can be re-run without affecting the existing gold table.
- [ ] The pipeline rejects a batch if the business keys (CustomerID) are not unique.

### FR-003 (L3)

The pipeline must not double count on a re-run.

- [ ] The pipeline does not double count orders on a re-run.
- [ ] The pipeline maintains the correct order count and total order amount for each customer.

## Non-functional requirements

- **NFR-001** The pipeline must be idempotent and reject a batch whose business keys are not unique.

## Assumptions

- The Sales.Customer table is the authoritative source of customer information.

## Open questions

- How often should the pipeline be run to ensure data freshness?
