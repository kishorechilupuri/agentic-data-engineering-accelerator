# Sales Order Count and Total

Build a curated gold table giving each customer their order count and total order amount, sourced from the SQL Server sales tables ADF lands in ADLS Gen2.

## Source tables

- `Sales.Customer`
- `Sales.Order`
- `Sales.OrderItem`

## Target entities

- **L3** `customer_orders` -- one row per customer

## Functional requirements

### FR-001 (L1)

Extract customer IDs and order IDs from the source tables.

- [ ] The pipeline extracts all customer IDs from the Sales.Customer table.
- [ ] The pipeline extracts all order IDs from the Sales.Order table.

### FR-002 (L2)

Count the number of orders for each customer.

- [ ] The pipeline counts the number of orders for each customer.
- [ ] The pipeline stores the count in the customer_orders table.

### FR-003 (L3)

Calculate the total order amount for each customer.

- [ ] The pipeline calculates the total order amount for each customer.
- [ ] The pipeline stores the total in the customer_orders table.

## Non-functional requirements

- **NFR-001** The pipeline must complete within 2 hours.

## Assumptions

- The Sales.Customer table has a column named 'CustomerID'.
- The Sales.Order table has a column named 'OrderID'.
- The Sales.OrderItem table has a column named 'OrderID'.
- The pipeline will use the current date as the effective date for the orders.

## Open questions

- How should orders with missing or invalid dates be handled?
- What is the expected format for the total order amount?
