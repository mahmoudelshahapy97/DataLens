---
name: access-control
description: Restrict which rows and columns a user can read, using row-level and column-level rules in the semantic layer. Use when different people must see different slices of the same tables.
allowed-tools: Bash(vanna:*)
---

# Row- and column-level access control

Rules live in the manifest and are enforced in the tool registry, which every
path — chat, MCP, dashboards, evaluations — passes through. There is no second
place to configure and no path that skips it.

## Row rules

Written on the model, as SQL over its own columns, with `@name` for a value
supplied per request:

```yaml
row_level_access_controls:
  - name: own_region
    condition: region = @session_region
    required_properties:
      - name: session_region
        required: true
```

The predicate is folded into the model's definition, so it ends up *inside* the
CTE the compiler builds. A user's subquery, `UNION`, or derived table sits above
that and cannot remove it. A predicate added to the outer `WHERE` would be
defeated by `SELECT * FROM (SELECT * FROM orders) x`.

## Column rules

```yaml
- name: salary
  type: INTEGER
  column_level_access_control:
    name: salary_guard
    operator: GREATER_THAN_OR_EQUALS
    threshold: {value: "5", dataType: NUMERIC}
    required_properties:
      - name: session_level
```

A blocked column is **removed from the model**, so referencing it is an
unknown-column error the agent can report and route around. It is deliberately
not nulled: a NULL silently corrupts `AVG` and `SUM`, and the reader is never
told the number is wrong.

Exactly one required property. Validation enforces this.

## Where the values come from

From the resolved user, never from the request. Map them in
`vanna_project.yml`:

```yaml
session_properties:
  session_region: metadata.region
  session_level: metadata.clearance
```

`tenant_id`, `user_email`, `user_id`, `username` and `groups` are available with
no configuration.

A session property a caller can set is not a control. If you find yourself
reading one from a tool argument, stop.

## Fail closed, always

A required property with no value **denies the query**. It does not skip the
rule. The tempting alternative means a misconfiguration silently returns *more*
data, which is the one direction this must never fail in.

You will see: `Access rule 'own_region' needs 'session_region', which is not set
for this user.` That is correct behaviour. Fix the user's attributes, not the
rule.

## Verify it

Do not infer from a small result set that a rule fired — that looks identical to
a rule that is broken in the other direction.

```bash
curl -X POST .../api/vanna/v2/admin/access/preview \
  -d '{"email":"ada@example.com","sql":"SELECT * FROM orders"}'
```

Returns the SQL that user would actually run, the rules applied, and the columns
dropped.

Then check the evasions. Each of these must still carry the predicate:

- `SELECT * FROM (SELECT * FROM orders) x`
- `SELECT id FROM orders UNION ALL SELECT id FROM orders`
- `WITH orders AS (SELECT * FROM orders) SELECT * FROM orders`
- a self-join
- `SELECT *`

## What this is not

It is not a substitute for database grants. A user who can reach the warehouse
directly is not constrained by anything here. This governs what the *agent* will
run on their behalf — which is the surface that exists because you deployed
Vanna, and therefore the one you are responsible for.
