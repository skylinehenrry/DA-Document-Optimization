# Input Data Dependency Extraction Prompt

You are analyzing one script or workflow file from a larger data-processing workflow.

Your task is to identify data resources that the script reads, loads, imports, queries, or otherwise consumes as input.

Return your findings using the provided structured output schema.

## Response Language

Use the following language for all free-text fields such as `evidence`, `source_context`, and `unclear_items`:

```text
{output_language}
```

## Extraction Target

Extract only input data dependencies.

For each input data dependency found, create one dependency object with:

- `relationship`: `reads`
- `target.type`: one of `file`, `table`, `database`, `api`, or `unknown`
- `target.name`: the file path, table name, database name, API endpoint, connection name, or resource name
- `target.path`: the file path if the target is a file and a path is visible, otherwise leave empty
- `target.details.source_context`: useful surrounding context, such as function name, SQL statement type, Alteryx tool type, BAT command, or connection reference
- `evidence`: the exact line, expression, XML fragment, SQL clause, or command showing the input dependency
- `confidence`: `high`, `medium`, or `low`

## What Counts As Input Data

Include resources that are read or consumed by the script.

Examples include:

- CSV, TSV, TXT, JSON, Excel, Parquet, XML, YXDB, or other local/network files
- Database tables or views queried by SQL
- Database connection names when the table is not explicit
- API endpoints or URLs used to retrieve data
- Alteryx Input Data tools
- Alteryx Dynamic Input tools
- BAT command arguments that appear to be input files
- Files passed into Python, SQL, Alteryx, or other scripts
- SQL embedded inside Python or BAT scripts

## Python Input Patterns

Look for examples such as:

```python
pd.read_csv("raw/customers.csv")
pd.read_excel("inputs/orders.xlsx")
open("config/settings.json", "r")
Path("inputs/data.txt").read_text()
pd.read_sql("SELECT * FROM raw.customers", conn)
requests.get("https://api.example.com/customers")
```

## SQL Input Patterns

Look for examples such as:

```sql
SELECT * FROM raw.customers
SELECT * FROM sales.orders o JOIN crm.customers c ON o.customer_id = c.customer_id
MERGE INTO target_table USING source_table
COPY table_name FROM 'input.csv'
LOAD DATA INFILE 'input.tsv'
```

For SQL:

- Tables or views in `FROM`, `JOIN`, and `USING` clauses are usually input data.
- Files in `COPY FROM` or `LOAD DATA` are input files.
- Do not treat CTE names as external input tables unless they reference an external source.

## Alteryx Input Patterns

Look for Alteryx workflow XML elements that indicate input data, such as:

- Input Data tools
- Dynamic Input tools
- Database input configurations
- File paths inside input tool configuration
- Connection names or query text used by input tools

Examples may include references to:

```text
DbFileInput
DynamicInput
InputData
Connection
Query
File
```

## BAT Input Patterns

Look for command-line inputs such as:

```bat
python clean_customers.py raw/customers.csv
sqlcmd -i query.sql
type input.txt
copy raw\customers.csv staging\customers.csv
AlteryxEngineCmd.exe workflow.yxmd
```

For BAT:

- In `copy source destination`, the source is usually input data.
- In `move source destination`, the source is usually input data.
- Files passed as arguments may be input data if they appear to be consumed by the called command.
- Scripts called by BAT should only be included if they are being read as data. Otherwise, they belong in a separate script-call extraction task.

## Unclear Items

If something appears to be an input dependency but cannot be resolved clearly, add a short note to `unclear_items`.

Examples:

- Dynamic paths built from variables
- Environment-variable-based paths
- Database connection strings without visible tables
- SQL strings assembled across multiple variables
- Alteryx connection aliases without visible file/table names

## Rules

- Do not extract output files or output tables.
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
