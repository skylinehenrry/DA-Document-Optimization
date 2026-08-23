"""
Main workflow runner for the DA Document Generator.

This module is responsible for the actual analysis workflow:
- Receive script folder, output folder, model, and concurrency settings
- Find scripts that should be analyzed
- Ask the LLM to extract dependency profiles
- Ask the LLM to construct the workflow dependency network
- Ask the LLM to generate script summaries
- Build the flowchart spec and final HTML flowchart
- Save all generated outputs into an outputs folder inside the selected DA Document folder
"""

from pathlib import Path
from typing import Callable, Literal
import asyncio
import json

try:
    from .classes import ScriptDependencyProfile, WorkflowDependencyGraph, ScriptSummary
    from .tools import (
        construct_dependency_network,
        construct_flowchart_spec,
        create_dependency_chains,
        extract_dependencies_for_folder,
        generate_summary_for_folder,
        list_all_scripts,
        render_flowchart_html,
    )
except ImportError:
    from classes import ScriptDependencyProfile, WorkflowDependencyGraph, ScriptSummary
    from tools import (
        construct_dependency_network,
        construct_flowchart_spec,
        create_dependency_chains,
        extract_dependencies_for_folder,
        generate_summary_for_folder,
        list_all_scripts,
        render_flowchart_html,
    )


ModelProvider = Literal["OpenAI", "Ollama"]


def save_json_outputs(
    output_dir: Path,
    profiles: list[ScriptDependencyProfile],
    workflow_network: WorkflowDependencyGraph,
    summaries: list[ScriptSummary],
    logger: Callable[[str], None] | None = None,
) -> None:
    """
    Save the main workflow objects as JSON files.
    - profiles.json contains per-script dependency extraction
    - workflow_network.json contains script-to-script workflow reasoning
    - summaries.json contains per-script high-level and detailed summaries
    """
    def log(message: str) -> None:
        if logger is not None:
            logger(message)

    output_dir.mkdir(parents = True, exist_ok = True)

    (output_dir / "profiles.json").write_text(
        json.dumps(
            [profile.model_dump() for profile in profiles],
            indent = 2,
            ensure_ascii = False,
        ),
        encoding = "utf-8",
    )
    log("Saved profiles.json.")

    (output_dir / "workflow_network.json").write_text(
        workflow_network.model_dump_json(indent = 2),
        encoding = "utf-8",
    )
    log("Saved workflow_network.json.")

    (output_dir / "summaries.json").write_text(
        json.dumps(
            [summary.model_dump() for summary in summaries],
            indent = 2,
            ensure_ascii = False,
        ),
        encoding = "utf-8",
    )
    log("Saved summaries.json.")


def save_flowchart_outputs(
    output_dir: Path,
    workflow_network: WorkflowDependencyGraph,
    profiles: list[ScriptDependencyProfile],
    summaries: list[ScriptSummary],
    logger: Callable[[str], None] | None = None,
) -> None:
    """
    Build and save the visual workflow flowchart outputs.
    - flowchart_spec.json is the structured flowchart data
    - workflow_flowchart.html is the human-friendly visual report
    - Both files are saved into the selected DA Document folder
    """
    def log(message: str) -> None:
        if logger is not None:
            logger(message)

    log("Building flowchart specification.")
    flowchart_spec = construct_flowchart_spec(
        workflow_graph = workflow_network,
        profiles = profiles,
        summaries = summaries,
    )

    (output_dir / "flowchart_spec.json").write_text(
        flowchart_spec.model_dump_json(indent = 2),
        encoding = "utf-8",
    )
    log("Saved flowchart_spec.json.")

    log("Rendering workflow_flowchart.html.")
    render_flowchart_html(
        flowchart_spec = flowchart_spec,
        output_path = output_dir / "workflow_flowchart.html",
    )
    log("Saved workflow_flowchart.html.")


async def run_da_document_workflow(
    script_folder: str | Path,
    da_document_folder: str | Path,
    model: ModelProvider = "OpenAI",
    language: str = "English",
    max_concurrency: int = 3,
    logger: Callable[[str], None] | None = None,
) -> dict[str, Path]:
    """
    Run the full DA Document analysis workflow.
    - script_folder is selected in the frontend as Script Folder
    - da_document_folder is selected in the frontend as DA Document Folder
    - Generated files are saved into <da_document_folder>/outputs
    - model controls whether OpenAI or Ollama is used
    - language controls generated explanation language
    - max_concurrency controls how many LLM calls can run at once
    """
    def log(message: str) -> None:
        # The logger is provided by either the frontend API or the CLI entry point.
        # Keeping this as the single progress channel makes browser logs and
        # console output show the same workflow status.
        if logger is not None:
            logger(message)

    log("Preparing analysis parameters.")

    script_folder = Path(script_folder).expanduser()
    output_root = Path(da_document_folder).expanduser()
    output_dir = output_root / "outputs"

    log(f"Script folder received: {script_folder}")
    log(f"DA Document folder received: {output_root}")
    log(f"Model selected: {model}")
    log(f"Output language selected: {language}")
    log(f"Max concurrency selected: {max_concurrency}")

    log("Validating script folder.")
    if not script_folder.is_dir():
        raise FileNotFoundError(f"Script folder does not exist: {script_folder}")

    output_dir.mkdir(parents = True, exist_ok = True)
    log(f"Output folder ready: {output_dir}")

    valid_file_list = list_all_scripts(script_path = script_folder, logger = log)
    log(f"Found {len(valid_file_list)} supported script file(s).")

    if not valid_file_list:
        raise ValueError(f"No supported scripts were found in: {script_folder}")

    log(f"Setting up {model} dependency extraction chains.")
    dependency_chains = create_dependency_chains(model = model)

    log("Extracting imports, input data, and output data from scripts.")
    profiles = await extract_dependencies_for_folder(
        valid_file_list = valid_file_list,
        chains = dependency_chains,
        max_concurrent_files = max_concurrency,
        output_language = language,
        logger = log,
    )
    log("Dependency profile extraction complete.")

    # The network construction step is synchronous, so run it outside the event loop.
    log("Constructing workflow dependency network.")
    workflow_network = await asyncio.to_thread(
        construct_dependency_network,
        profiles,
        model,
        language,
        log,
    )
    log("Workflow dependency network complete.")

    log("Generating script summaries.")
    summaries = await generate_summary_for_folder(
        valid_file_list = valid_file_list,
        dependency_profiles = profiles,
        workflow_graph = workflow_network,
        model = model,
        max_concurrent_files = max_concurrency,
        output_language = language,
        logger = log,
    )
    log("Script summary generation complete.")

    log("Saving JSON outputs.")
    save_json_outputs(
        output_dir = output_dir,
        profiles = profiles,
        workflow_network = workflow_network,
        summaries = summaries,
        logger = log,
    )

    log("Rendering workflow flowchart.")
    save_flowchart_outputs(
        output_dir = output_dir,
        workflow_network = workflow_network,
        profiles = profiles,
        summaries = summaries,
        logger = log,
    )
    log("Analysis complete. Outputs are ready.")

    return {
        "profiles": output_dir / "profiles.json",
        "network": output_dir / "workflow_network.json",
        "summaries": output_dir / "summaries.json",
        "flowchart_spec": output_dir / "flowchart_spec.json",
        "flowchart": output_dir / "workflow_flowchart.html",
    }


async def main() -> None:
    """
    Optional command-line entry point for manual testing.
    - The frontend normally calls run_da_document_workflow through backend/app.py
    - This block remains useful when testing the workflow without the UI
    """
    script_folder = input("Please enter the script folder: ")
    output_folder = input("Please enter the DA Document output folder: ")

    outputs = await run_da_document_workflow(
        script_folder = script_folder,
        da_document_folder = output_folder,
        model = "Ollama",
        language = "English",
        max_concurrency = 3,
        logger = print,
    )

    print("Saved outputs:")
    for name, path in outputs.items():
        print(f"- {name}: {path}")


if __name__ == "__main__":
    asyncio.run(main())
