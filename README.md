# DA Document Generator

Analyze a project, correct its connections visually, and generate an interactive HTML flowchart from the saved result. Python, SQL, BAT and Alteryx files can be analyzed together. Analysis and visual editing work locally; AI suggestions and richer script descriptions are optional.

The chart shows **project dependencies and explicit workflow connections**. It is not a complete map of every statement, branch or possible runtime execution. A recorded connection may belong to a function or branch that never runs.

## Start the application

**If the previous version is still running, wait for its work to finish and stop its original command window once before opening the updated launcher.** An already running Python process does not load the updated backend automatically. Refresh any old browser tab after restarting.

Open this project folder and double-click:

- **macOS:** `Launch DA Document Generator.command`
- **Windows:** `Launch DA Document Generator.bat`

The launcher opens [the local application](http://127.0.0.1:8000/frontend/index.html) after checking that the correct backend is ready. It uses the project's `.venv` environment when available. You can close the launcher window after startup: the backend runs separately and continues accepted work when the browser closes.

If the required packages are not installed, complete this one-time setup from the project folder. Python 3.14 was used for verification; other versions have not been fully verified.

macOS:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python launch.py
```

Windows PowerShell:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe launch.py
```

Create the environment on the computer where you will run the app; a macOS `.venv` cannot be reused on Windows. Use valid Windows source and output paths. They may be on a network share, but the private saved-work store must be on a local disk.

The launcher does not install packages or download models automatically. No frontend build or Node installation is needed to run the app. Keep it on `127.0.0.1`; this is a local application for one trusted user.

## Interface appearance

The interface restores the original DA Document Generator style: a simple full-width header, centred numbered sections, thin blue-gray dividers, outlined controls, a horizontal progress tracker, a compact run log and clear output actions. The current Analyze → Review → Generate process remains separate underneath that familiar appearance, so a user still corrects and saves the graph before summaries or interactive output are attached.

Choose **App settings → Appearance → Accent theme** to switch between **Blue** (default), **Violet** and **Graphite**. The choice applies immediately, survives reloads in the same browser profile/address, and stays in sync across tabs. It changes the app interface only, not saved graphs or previously generated HTML files. If browser storage is unavailable, the chosen theme still works for the current page and the settings explain that it cannot be saved.

Open the saved-workflow library from the folder button in the top bar and close it with its close button or Escape. The same drawer is used on desktop and narrow screens so the working canvas keeps the full page width. Forms reflow on smaller screens, and the inspector moves below the diagram. See [frontend design notes](docs/frontend-design.md) for palette tokens, comment conventions and validation details.

## Analyze, review and generate

1. Choose **New analysis** and select the source folder and output folder. **Project name** is optional: leave it blank to use the source folder's name. The output folder is where generated files will be written; this workflow does not create a Word document.
2. Supply the optional runtime working folder, SQL dialect or shared database namespace only when you know them. These help distinguish resources with similar names. Unknown values should be left blank.
3. Choose **Analyze project**. The application reads supported source files without running them or calling a model. The job is saved before it begins; you can reopen its progress after a browser reload.
4. Use **Review → Findings** and **Source files** to check coverage. Findings are grouped by cause, with the original source locations available on expansion. A file that needs dependency review is different from a file that failed analysis.
5. Correct the draft under **Diagram**. Drag cards to reposition them; drag the background to pan. Use the wheel or zoom controls and **Fit** to navigate. Select a card or arrow to inspect it. Use **Add node** or **Add connection** for missing items, and change a connection's source/destination in the inspector to reconnect it. Remove incorrect items and confirm or reject proposed connections.
6. Choose **Save changes** to create a saved revision. **Undo**, **Redo** and **Discard** apply to your local edits. Generation uses a saved revision, so finish saving before proceeding.
7. Choose **Generate Flow Chart**. Local English descriptions are the default. Enable AI summaries only if you want the saved source text sent to the selected provider. When finished, choose **Open flowchart** to view it or **Download HTML** to save a standalone interactive file.

Use the saved-workflow library to reopen work. A new analysis creates a separate draft: it never overwrites a previously reviewed graph. Generating again adds a new result without changing the saved connections. Changes to source files, or improvements to the analyzer, require a fresh analysis if you want them reflected in the draft.

Script and file cards display the filename and extension, such as `load.sql` or `report.py`. Full paths remain available in the inspector/tooltip and saved graph. Files with the same name in different folders keep distinct identities; shortening a label does not merge them.

The default direct-dependency view groups repeated connections between the same file pair to reduce visual clutter, while retaining each underlying relationship and its evidence. Focus shows only immediate neighbors. `A → B → C` remains two direct relationships; it never becomes an invented `A → C` connection. If the source explicitly contains `A → C` as well, that real connection remains available.

### Editing and recovery

Unsaved diagram changes are kept in browser storage when storage is available. Returning with the same browser profile and site address can recover them. This is a convenience copy, not a substitute for **Save changes**: clearing site data, using another browser, or a storage quota failure can remove it. Export unsaved edits if the app reports a recovery-storage problem.

If another tab or operation has saved a newer revision, the app will not overwrite it with stale edits. Download your unsaved changes if needed, reload the current revision, and review what should be reapplied. Generation cannot quietly discard pending local edits.

The optional `.drawio` exchange remains available. Download the draft, edit it in an approved draw.io/diagrams.net editor, save the full file, then import it into the same draft revision. Keep one page and one layer. Original script cards retain their source associations; copied/new cards are unsourced process nodes. Positions, labels and connections are imported, but custom styles, card resizing and bend points are not. See the [backend guide](docs/backend-workflow.md#drawio-exchange) for the supported format.

## If a window closes or the backend stops

| Situation | What happens / what to do |
| --- | --- |
| Browser closes or reloads | Accepted jobs continue. Reopen the application to recover saved progress and results. |
| Launcher command window closes | A backend started by the new launcher continues separately. |
| Computer restarts or the backend is force-closed | Reopen the launcher. Saved drafts and completed results remain. Queued jobs that never started can proceed; unfinished running work is marked **Interrupted** unless its saved result can be recovered. |
| **Interrupted** or failed operation | Inspect the error and saved draft before choosing **Retry operation**. An interrupted model request may already have incurred a charge, so it is not automatically repeated. |
| Response is lost during submission or saving | The app checks the original request and saved transaction instead of blindly creating another operation. |
| A different backend occupies the port | The launcher explains the conflict rather than controlling that process. Stop the correct old instance or select another port. |

To stop the app normally, use **Settings → Stop server**, or run `python launch.py --stop` with the environment activated. Shutdown is refused while work is queued or running. `python launch.py --status` checks the current instance, `--port 8001` selects a different port, and `--no-browser` starts without another tab. Use the same port and `DA_WORKFLOW_STORE` setting for subsequent status/stop commands.

A manually started `python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000` server remains attached to its terminal. Stop that server from its own command window after work finishes; the managed shutdown button is unavailable for it. The detached-launcher behavior does not apply to direct CLI commands either.

## Understanding findings and missing downloads

- **Analyzed; some dependencies need review** means the file was analyzed, but some relationships could not be resolved statically. Dynamic filenames, database execution calls, inherited methods and complex batch commands are examples. It does not mean the source has invalid syntax.
- **Source analysis failed** means the source could not be analyzed, for example because of syntax, encoding or a parser failure. **Skipped** means it was not read safely or exceeded a limit. Generated/cache folders intentionally excluded from discovery are informational.
- **Proposed** connections need confirmation. They remain visually distinct if you explicitly choose to include them. Warnings do not automatically block generation; proposals and analysis errors do unless you review and explicitly accept them.
- **Local descriptions** mean no model summaries were requested. **Fallback** means a requested model summary was unavailable and a local English description was used instead. The reason is recorded with the summaries and displayed in the generated chart. Neither case rewrites connections.

Finished flowcharts now have addresses tied to their saved draft and generation. Open the saved workflow and use **Open flowchart** or **Download HTML**, rather than an old `/api/output/...` link. Those old links are retired. If a file was moved, deleted or changed on disk, the app explains the problem; restore it or generate again from the saved revision. A reviewed graph remains saved even if a generated file is missing.

## Optional AI setup

Install `requirements-llm.txt` in the same environment only if you want model suggestions or summaries:

```sh
python -m pip install -r requirements-llm.txt
```

Provider setup is isolated in `backend/model_provider.py`. The API selector `OpenAI` retains the existing **Azure OpenAI** deployment and interactive Microsoft sign-in; it does not use a newly introduced `OPENAI_API_KEY` setup. `Ollama` retains the existing local model configuration. Selecting a provider alone does not contact it.

AI suggestions add unconfirmed connections for review. AI summaries may only supply description text during generation. Source comments and attached text are treated as data, not as instructions to the model. Check the provider configuration and your data-sharing policy before opting in. Live provider authentication and paid model calls were not exercised for this redesign.

If Ollama summaries time out, check that the configured `qwen3.5:9b` model is available on the selected Ollama server. Under **Enhance summaries with AI → Model performance settings**, try **Concurrent requests: 1** and a longer **Timeout per request (seconds)**, up to 300. Alternatively, generate local descriptions while investigating. These settings can help distinguish load/time limits from a configuration problem, but do not guarantee a successful response or establish why a previous request timed out.

## Saved work, outputs and technical reference

The default private store is `backend/.workflow_store/workflows.sqlite3`. It retains drafts, revisions, source snapshots, jobs, operation receipts and summary-cache records. Startup logs are kept beside it in `server.log`. `DA_WORKFLOW_STORE` can select another **local private directory**; do not put it under `frontend/` or on a shared network filesystem. Keep the same setting when reopening the app. Back up the store and output folder together after stopping the app normally.

If the app itself is on a Windows network share, the launcher refuses the default store on that share. Set a local store and launch from the same PowerShell window:

```powershell
$env:DA_WORKFLOW_STORE = Join-Path $env:LOCALAPPDATA "DAFlowchartStudio\store"
.\.venv\Scripts\python.exe launch.py
```

For future double-click launches, make `DA_WORKFLOW_STORE` a Windows user environment variable with that same local path. The app does not automatically move old saved work. Before transferring an existing store from a share, stop **all** app instances and direct CLI operations, back it up, and copy the complete private store to the chosen local folder. Do not copy an active SQLite database in isolation.

Generated files are written beneath:

```text
<output folder>/outputs/workflows/<draft_id>/
  revisions/<revision>/
    draft.json, draft.drawio, draft.svg, review.json
  generations/<generation_id>/
    workflow_flowchart.html, workflow_graph.json,
    summaries.json, review.json, generation_manifest.json
```

The app serves only scoped, validated artifact links. There is no public output-folder mount. Downloaded HTML is self-contained and can be opened after the backend stops. Graphs, evidence and summaries can contain sensitive source excerpts, filenames and resource names; review them before sharing.

The [backend workflow and API guide](docs/backend-workflow.md) covers request formats, job recovery, graph semantics, command-line use, bounds and migration. The supported source extensions are `.py`, `.sql`, `.bat`, `.yxmd`, `.yxwz` and `.yxmc`. Dynamic runtime behavior, stored-procedure internals, embedded Alteryx code and full statement-level control flow still require manual review. **DOCX generation is not implemented.**

To run automated checks from the project folder with the Python environment activated:

```sh
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
node --test
```

Node is needed only for the frontend tests. Automated checks use temporary projects and mocked model responses. Browser checks verified analysis, filename-only cards, folder-name fallback, node dragging, adding/reconnecting connections, grouped removal/undo, saving, local generation and a project-named HTML download. Unsaved edits survived closing the browser page and restarting the backend. Windows path/launcher regressions and the CI matrix are included, but native Windows execution and live model responses remain unverified on this macOS host.
