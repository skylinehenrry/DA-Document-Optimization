# Workflow Dependency Reasoning Prompt

You are analyzing dependency profiles from multiple scripts in a data-processing workflow.

Your task is to infer the overall processing workflow: how scripts relate to each other, how data moves between them, and which scripts likely run before or after others.

Return the result using the provided structured output schema.

## Reasoning Rules

Use the dependency profiles as evidence.

Create a dependency edge when one of the following relationships is supported:

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

- stable ID
- script name
- script path
- script type
- short label
- likely role in the workflow if inferable from dependencies

## Unresolved Items

Add unresolved items when:

- a script reads data that no other script writes
- a script writes data that no other script reads
- an import cannot be mapped to a known script
- a dependency appears dynamic or ambiguous
- script execution order cannot be determined

## Rules

- Do not invent scripts that are not in the provided profiles.
- Do not invent files, tables, or resources that are not in the profiles.
- Prefer exact matches over inferred matches.
- Use low confidence for weak or ambiguous relationships.
- Distinguish data dependencies from code dependencies and execution dependencies.
- Return only the structured output requested by the schema.

## Script Dependency Profiles

```json
{script_dependency_profiles}
```
