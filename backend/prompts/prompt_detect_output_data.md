# Output Data Dependency Extraction Prompt

You are analyzing one script or workflow file from a larger data-processing workflow.

Your task is to identify data resources that the script creates, writes, exports, updates, overwrites, deletes, or otherwise produces as output.

Return your findings using the provided structured output schema.

## Response Language

Use the following language for all free-text fields such as `evidence`, `source_context`, and `unclear_items`:

```text
{output_language}
```

## Extraction Target

Extract only output data dependencies.

For each output data dependency found, create one dependency object with:

- `relationship`: `writes`
- `target.type`: one of `file`, `table`, `database`, `api`, or `unknown`
- `target.name`: the file path, table name, database name, API endpoint, connection name, or resource name
- `target.path`: the file path if the target is a file and a path is visible, otherwise leave empty
- `target.details.source_context`: useful surrounding context, such as function name, SQL statement type, Alteryx tool type, BAT command, or connection reference
- `evidence`: the exact line, expression, XML fragment, SQL clause, or command showing the output dependency
- `confidence`: `high`, `medium`, or `low`

## What Counts As Output Data

Include resources that are written or produced by the script.

Examples include:

- CSV, TSV, TXT, JSON, Excel, Parquet, XML, YXDB, or other local/network files created or updated
- Database tables or views created, inserted into, updated, deleted from, merged into, truncated, or overwritten
- API endpoints used to send or upload data
- Alteryx Output Data tools
- Files created or moved by BAT commands
- Export destinations
- SQL embedded inside Python or BAT scripts

## Python Output Patterns

Look for examples such as:

```python
df.to_csv("processed/customers.csv")
df.to_excel("reports/orders.xlsx")
open("logs/run.log", "w")
open("output.txt", "a")
Path("reports/summary.txt").write_text(summary)
df.to_sql("customer_summary", conn, if_exists="replace")
requests.post("https://api.example.com/customers", json=payload)
```

## SQL Output Patterns

Look for examples such as:

```sql
CREATE TABLE mart.customer_summary AS SELECT ...
CREATE VIEW reporting.customer_view AS SELECT ...
INSERT INTO mart.customer_summary SELECT ...
UPDATE mart.customer_summary SET ...
DELETE FROM mart.customer_summary WHERE ...
MERGE INTO mart.customer_summary USING staging.customer_summary
TRUNCATE TABLE staging.customer_summary
COPY mart.customer_summary TO 'output.csv'
```

For SQL:

- Tables or views in `CREATE`, `INSERT INTO`, `UPDATE`, `DELETE FROM`, `MERGE INTO`, and `TRUNCATE TABLE` are output data.
- Files in `COPY TO`, export, or unload commands are output files.
- Do not treat CTE names as output tables unless they are materialized into a table or view.

## Alteryx Output Patterns

Look for Alteryx workflow XML elements that indicate output data, such as:

- Output Data tools
- Database output configurations
- File paths inside output tool configuration
- Connection names or table names used by output tools

Examples may include references to:

```text
DbFileOutput
OutputData
Connection
Table
File
```

## BAT Output Patterns

Look for command-line outputs such as:

```bat
python generate_report.py > report.log
copy staging\customers.csv output\customers.csv
move temp\final.csv archive\final.csv
sqlcmd -i export.sql -o result.log
```

For BAT:

- In `copy source destination`, the destination is usually output data.
- In `move source destination`, the destination is usually output data.
- Redirected output using `>` or `>>` is output data.
- Log files written by commands may be output data if they are relevant to the workflow.

## Unclear Items

If something appears to be an output dependency but cannot be resolved clearly, add a short note to `unclear_items`.

Examples:

- Dynamic output paths built from variables
- Environment-variable-based destinations
- Database connection strings without visible target tables
- SQL strings assembled across multiple variables
- Alteryx connection aliases without visible file/table names

## Rules

- Do not extract input files or input tables.
- Do not extract imports or script calls.
- Do not summarize the script.
- Do not invent dependencies.
- Preserve exact paths, table names, connection names, API endpoints, and resource names when visible.
- Deduplicate repeated dependencies.
- Return only the structured output requested by the schema.

## Script Content

```text
{script_content}
```
