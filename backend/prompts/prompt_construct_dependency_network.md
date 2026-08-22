# Workflow Dependency Reasoning Prompt

You are analyzing dependency profiles from multiple scripts in a data-processing workflow.

Your task is to infer the overall processing workflow: how scripts relate to each other, how data moves between them, which scripts likely run before or after others, and what stage/order identifier should be assigned to each script.

Return the result using the provided structured output schema.

## Reasoning Rules

Use the dependency profiles as evidence.

Create a dependency edge when one of the following relationships is supported.

## Data Dependency

If script A writes a file, table, database object, or API resource that script B reads, create an edge:

```text
A -> B
```

Set:

- `relationship`: `data_dependency`
- `shared_resource`: the shared file/table/database/API resource
- `confidence`: `high` if the resource names match exactly
- `confidence`: `medium` if the names appear equivalent but are not exact
- `evidence`: explain the matching read/write relationship

## Code Dependency

If script A imports a custom module that corresponds to script B, create an edge:

```text
B -> A
```

This means script A depends on code defined in script B.

Set:

- `relationship`: `code_dependency`
- `shared_resource`: the imported module or script
- `confidence`: based on how clearly the import maps to a script
- `evidence`: explain the import relationship

## Execution Dependency

If script A calls, runs, invokes, or triggers script B, create an edge:

```text
A -> B
```

This means script A executes script B.

Set:

- `relationship`: `execution_dependency`
- `shared_resource`: the called script or workflow
- `evidence`: explain the call relationship

## Inferred Order

If filenames, numbering, comments, or roles strongly imply order, but there is no direct data/code/execution dependency, create an edge only when useful.

Set:

- `relationship`: `inferred_order`
- `confidence`: usually `low` or `medium`
- `evidence`: explain why the order is inferred

## Nodes

Create one node for each script profile.

Each node should include:

- stable unique graph ID
- script name
- script path
- script type
- short label
- likely role in the workflow if inferable from dependencies
- stage/order information if supported by the schema

For `WorkflowNode.id`:

- Create a stable unique graph identifier only.
- Do not use `WorkflowNode.id` as the execution order.
- Every node ID must be unique.
- Edge `source` and `target` must exactly match an existing node `id`.
- Prefer IDs derived from the script name using lowercase snake_case.

## Stage And Order Assignment

For each script node, assign:

- `stage_ID`
- `order_ID`
- `stage_order_ID`

The `stage_order_ID` is the combination of `stage_ID` and `order_ID`.

Examples:

```text
A01
A02
B01
B02
C01
```

Only the combined `stage_order_ID` must be unique across all scripts.

## Stage ID Rules

Use stage IDs to group scripts by their role in the workflow.

Suggested stage IDs:

- `A`: Raw data ingestion, extraction, source loading
- `B`: Pre-processing, cleaning, standardization, validation
- `C`: Transformation, joining, enrichment, calculation
- `D`: Data loading, publishing, database/table creation
- `E`: Reporting, export, dashboards, final outputs
- `Z`: Orchestration, utility, helper, or unclear role

Choose the stage that best reflects the script's main role.

## Order ID Rules

Within each stage, assign a zero-padded two-digit order ID:

```text
01
02
03
```

Order scripts based on the best available evidence:

1. Explicit execution order from BAT files, main scripts, or workflow runners
2. Data dependencies where one script writes data read by another
3. Code dependencies where one script imports or calls another
4. Filename prefixes such as `01_`, `02_`, `03_`
5. Inferred logical workflow order

If multiple scripts appear to run in parallel within the same stage, still assign unique order IDs within that stage. Use filename order as a stable tie-breaker and mention the parallelism in `order_evidence`.

## Uniqueness Rules

- Every script node must have exactly one `stage_ID`
- Every script node must have exactly one `order_ID`
- Every script node must have exactly one `stage_order_ID`
- `stage_order_ID` must equal `stage_ID + order_ID`
- No two script nodes may have the same `stage_order_ID`
- Do not skip order numbers within a stage unless there is strong evidence that a slot is intentionally reserved

## Confidence Rules

Set `order_confidence`:

- `high`: explicit execution order or direct data dependency
- `medium`: filename/order convention or strong role-based inference
- `low`: weak inference or unclear order

Set `order_evidence` to briefly explain why the stage/order was assigned.

## Unresolved Items

Add unresolved items when:

- a script reads data that no other script writes
- a script writes data that no other script reads
- an import cannot be mapped to a known script
- a dependency appears dynamic or ambiguous
- script execution order cannot be determined confidently
- stage/order assignment is uncertain

## Rules

- Do not invent scripts that are not in the provided profiles.
- Do not invent files, tables, or resources that are not in the profiles.
- Prefer exact matches over inferred matches.
- Use low confidence for weak or ambiguous relationships.
- Distinguish data dependencies from code dependencies and execution dependencies.
- Ensure every edge references valid node IDs.
- Return only the structured output requested by the schema.

## Script Dependency Profiles

```json
{script_dependency_profiles}
```
