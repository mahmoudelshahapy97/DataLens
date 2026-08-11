---
name: generate-mdl
description: Build a semantic layer -- models, calculated columns, metrics and joins -- so the agent writes SQL against business names. Use when someone wants metrics defined once, or is tired of explaining that amounts are in cents.
allowed-tools: Bash(vanna:*)
---

# Building a semantic layer

Turn a scanned schema into models the agent queries by business name:
`revenue`, not `SUM(amount_cents)/100.0`.

## Why bother

Without it, every question re-derives the same definitions, and the model gets
them subtly wrong in different ways each time. A calculated column is written
once and reviewed once.

## Steps

### 1. Draft from the scan

```bash
vanna project from-catalog
```

One model per table, join keys hidden, foreign keys as relationships, and the
column values the scan profiled carried across. It is exactly as smart as the
schema and no smarter — the value is that the next step is editing, not
authoring from a blank page.

### 2. Edit `models/<name>/metadata.yml`

**Rename to what people say.** If everyone says "customer" and the table says
`dim_cust_v2`, the model is `customers`.

**Add calculated columns for anything anyone recomputes.**

```yaml
- name: amount
  type: DOUBLE
  is_calculated: true
  expression: amount_cents / 100.0
  description: Order value in dollars.
```

Expressions are written over *this model's* column names. They may not contain
aggregates — a model row is one row. Aggregates are measures.

**Hide the noise.** `is_hidden: true` on surrogate keys keeps them usable in
joins and out of the prompt.

### 3. Check the join cardinality

```yaml
relationships:
  - name: orders_customers
    models: [orders, customers]
    join_type: MANY_TO_ONE      # many orders, one customer
    condition: orders.customer_id = customers.id
```

`join_type` is not documentation. `ONE_TO_MANY` is what tells the compiler that
`SUM(customers.orders.amount)` will repeat each customer once per order and
inflate any total over the customer side. Getting it backwards means the warning
fires on the wrong queries.

Order of models matters: cardinality is read left to right.

### 4. Define metrics as cube measures

```yaml
# cubes/order_metrics.yml
name: order_metrics
base_object: orders
measures:
  - name: revenue
    expression: SUM(amount)
    type: DOUBLE
dimensions:
  - name: region
    expression: customer.region
```

A measure is where aggregation belongs, and where the correct grain is decided
once by a human instead of per-question by a model.

### 5. Validate and build

```bash
vanna project validate --level warning
vanna project build
```

Validation catches, at author time, what would otherwise be a compiler error
several steps from its cause: dangling relationships, aggregates in model
columns, calculated-column cycles, rules using undeclared session properties.

`build` writes `target/mdl.json` and refuses to write a manifest with errors.

### 6. Point the deployment at it

`VANNA_PROJECT_DIR=/path/to/project`. The container reads the *built* manifest,
so what runs is what someone deliberately compiled.

## Checking your work

```bash
# What does this actually become?
curl -X POST .../api/vanna/v2/semantic/compile -d '{"sql":"SELECT revenue FROM ..."}'
```

Look for: the expression you expected, joins you expected, and no fan-out
warning on a query that aggregates.

## Traps

- **An aggregate in a calculated column.** Rejected at validation. Use a measure.
- **Two relationships to the same model.** Fine — they are aliased by path. But
  give the handles distinct names (`customer`, `approver`), not both `customer`.
- **MANY_TO_MANY.** The compiler refuses to traverse it, because there is no one
  correct join. Model the join table explicitly.
- **Editing `target/mdl.json`.** It is regenerated. Edit the YAML.
