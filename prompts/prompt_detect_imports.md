# Custom Import Dependency Extraction Prompt

You are analyzing one script from a larger codebase.

Your task is to identify imports that appear to refer to project-specific modules, local packages, or custom code.

Return your findings using the provided structured output schema.

## Extraction Target

Extract only custom module imports.

For each custom import found, create one dependency object with:

- `relationship`: `imports`
- `target.type`: `module`
- `target.name`: the imported module path
- `target.path`: leave empty unless the script explicitly contains a local file path for the module
- `target.details.imported_names`: imported functions, classes, constants, or symbols when visible
- `target.details.alias`: import alias when visible
- `target.details.is_custom`: `true`
- `evidence`: the exact import statement from the script
- `confidence`: `high`, `medium`, or `low`

## What Counts As Custom

Treat an import as custom when it appears to refer to:

- A local project module
- A local package
- A nearby script in the same codebase
- A project-specific namespace such as `src`, `utils`, `config`, `pipeline`, `modules`, `helpers`, or a business/domain-specific package

Examples of custom imports:

```python
import config
import data_loader
from utils.cleaning import clean_names
from src.features.orders import build_order_features
from pipeline.extract.customer_extract import run_extract
```

## What To Ignore

Do not create dependency objects for:

- Python standard library imports
- Third-party packages commonly installed from `pip` or `conda`
- Common packages such as `pandas`, `numpy`, `sqlalchemy`, `pydantic`, `langchain`, `openai`, `requests`, `matplotlib`, `seaborn`, `sklearn`, `scipy`, `pathlib`, `os`, `sys`, `re`, `json`, `datetime`, `typing`, `collections`, `itertools`, `functools`, `subprocess`, `logging`, `xml`, `ast`, and similar external libraries

Examples to ignore:

```python
import os
import pandas as pd
from pathlib import Path
from pydantic import BaseModel
from langchain_community.document_loaders import TextLoader
```

## Unclear Imports

If an import may be custom but you are not sure, do not create a dependency object for it.

Instead, add a short note to `unclear_items` explaining the uncertainty.

## Rules

- Only inspect real import statements.
- Do not infer imports from comments, strings, docstrings, or examples.
- Preserve the module path rather than only the imported symbol.
- Deduplicate repeated imports.
- Do not summarize the script.
- Do not include non-import dependencies.
- Return only the structured output requested by the schema.

## Script Content

```python
{script_content}
```
