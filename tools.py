#%%
import os
import sys

script_directory = os.path.dirname(os.path.abspath(__file__))
os.chdir(script_directory)
sys.path.insert(0, script_directory)

from langchain_community.document_loaders import TextLoader
from langchain_ollama import ChatOllama
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.documents import Document


from pathlib import Path
from typing import Literal
from pydantic import BaseModel


#%%
"""
Tools for checking input directory validity and list all valid scripts
"""

def script_folder_exists(script_path: str | Path) -> bool:
    if isinstance(script_path, str):
        script_path = Path(script_path)
    return script_path.is_dir()


def list_all_scripts(script_path: str | Path) -> list[str]:
    script_path = Path(script_path)
    if script_folder_exists(script_path):
        print(f"Looking For Scripts In {script_path}...")

        valid_file_suffixes = [".py", ".sql", ".yxmd", ".yxwz", ".bat"] # Only allow for python, SQL, Alteryx and BAT files
        valid_file_list = []
        for file in script_path.iterdir():
            if file.is_file() and file.suffix in valid_file_suffixes:
                valid_file_list.append(file)

        for file in valid_file_list:
            print(file.name)
        return valid_file_list

    else:
        raise FileNotFoundError("The Specified Folder Doesn't Exist!")


#%%
"""
Tools to provide information on scripts found, including 
    - script name
    - script path
    - script type
    - relevant dependencies (imports, data connection, data output)

Tools listed in this section provides the first step of script interpretation, by understanding
    - processing logic of the script
    - list of modules imported
    - list of files / database table read
    - list of files / database table generated 

These information will be provided for second step analysis, in which Gen AI reasons on the inter-dependency between scripts and the order in which scripts should be executed
"""

import asyncio
from classes import ScriptDependencyProfile, DependencyRef, ResourceRef, ScriptSummary, DependencyExtraction

def set_up_LLM(model: Literal["OpenAI", "Ollama"] = "OpenAI"):
    if model == "OpenAI":
        return ChatOpenAI(temperature = 0)
    elif model == "Ollama":
        return ChatOllama(model = "qwen3.5:9b", 
                          temperature = 0)
    else:
        raise ValueError(f"Unsupported model provider: {model}")
    

def detect_script_type(file_path: Path):
    if file_path.suffix == ".py":
        return "python"
    elif file_path.suffix == ".sql":
        return "sql"
    elif file_path.suffix in [".yxmd", ".yxwz"]:
        return "alteryx"
    elif file_path.suffix == ".bat":
        return "bat"
    else:
        return "unknown"


def create_dependency_chains(model: Literal["OpenAI", "Ollama"]) -> DependencyExtraction:
    """
    Use LLM to extract the following information
        1. The list of imported modules (only include custom modules, and exclude built-in modules and packages installed from pip)
        2. The list of input data (e.g. csv files, tsv files, excel files, or database connection)
        3. The list of output data (e.g. csv files, tsv files, excel files or tables)
    The output will be a DependencyExtraction object, defined in dataclasses_module
    """

    LLM = set_up_LLM(model = model)

    import_prompt = ChatPromptTemplate.from_template(Path("./prompts/prompt_detect_imports.md").read_text(encoding = "utf-8"))
    input_data_prompt = ChatPromptTemplate.from_template(Path("./prompts/prompt_detect_input_data.md").read_text(encoding = "utf-8"))
    output_data_prompt = ChatPromptTemplate.from_template(Path("./prompts/prompt_detect_output_data.md").read_text(encoding = "utf-8"))

    structured_LLM = LLM.with_structured_output(DependencyExtraction)

    return {"imports": import_prompt | structured_LLM,
            "input data": input_data_prompt | structured_LLM,
            "output data": output_data_prompt | structured_LLM}


async def extract_dependencies_for_file(file: Path,
                                        chains: dict,
                                        semaphore: asyncio.Semaphore) -> ScriptDependencyProfile:
    """
    For each file passed in, pass the content of the file to LLM and buidl a ScriptDependencyProfile object that contains the following information:
        - script name
        - script directory
        - script type,
        - list of imported modules
        - list of input data
        - list of output data
    """
    print(f"Extracting dependency for {file.name}...")
    document = TextLoader(file_path = str(file), encoding = "utf-8", autodetect_encoding = True).load()[0]
    payload = {"script_content": document.page_content}
    import_result, input_data_result, output_data_result = await asyncio.gather(
        invoke_dependency_chain(chains["imports"], payload, semaphore, f"{file.name}: imports"),
        invoke_dependency_chain(chains["input data"], payload, semaphore, f"{file.name}: input data"),
        invoke_dependency_chain(chains["output data"], payload, semaphore, f"{file.name}: output data")
    )

    profile = ScriptDependencyProfile(file_name = file.name,
                                      file_path = str(file), 
                                      script_type = detect_script_type(file))

    profile.dependencies.extend(import_result.dependencies)
    profile.dependencies.extend(input_data_result.dependencies)
    profile.dependencies.extend(output_data_result.dependencies)

    profile.unclear_items.extend(import_result.unclear_items)
    profile.unclear_items.extend(input_data_result.unclear_items)
    profile.unclear_items.extend(output_data_result.unclear_items)

    return profile


async def invoke_dependency_chain(chain,
                                  payload: dict,
                                  semaphore: asyncio.Semaphore,
                                  task_name: str,
                                  max_attempts: int = 3) -> DependencyExtraction:
    """
    Invoke one dependency extraction chain with retry handling.
    Local Ollama models may occasionally return malformed or empty structured output,
    especially under concurrent load.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            async with semaphore:
                return await chain.ainvoke(payload)
        except Exception as error:
            if attempt == max_attempts:
                return DependencyExtraction(
                    unclear_items = [
                        f"{task_name} failed after {max_attempts} attempts: {error}"
                    ]
                )
            await asyncio.sleep(attempt)


async def extract_dependencies_for_folder(valid_file_list: list[Path],
                                          chains: dict,
                                          max_concurrent_files: int = 1):
    """
    Batch execution for extract_dependencies_for_file, with asynchronus implementation for processing efficiency
    """
    semaphore = asyncio.Semaphore(max_concurrent_files)
    tasks = [extract_dependencies_for_file(file = file, 
                                        chains = chains,
                                        semaphore = semaphore) for file in valid_file_list]

    return await asyncio.gather(*tasks)


#%%
"""
Tools for letting AI reason dependency graph and generate summaries on each script
These tools will be used to generate the flow chart used in DA Document
"""

import asyncio
import json
from classes import ScriptDependencyProfile, WorkflowDependencyGraph

def profiles_to_json(profiles: list[ScriptDependencyProfile]) -> str:
    return json.dumps([profile.model_dump() for profile in profiles], indent = 2, ensure_ascii = False)


def construct_dependency_network(profiles: list[ScriptDependencyProfile], 
                                 model: Literal["OpenAI", "Ollama"] = "OpenAI") -> WorkflowDependencyGraph:

    print(f"Constructing dependency network for the program...")
    LLM = set_up_LLM(model = model)
    
    workflow_prompt = ChatPromptTemplate.from_template(Path("./prompts/prompt_construct_dependency_network.md").read_text(encoding = "utf-8"))
    workflow_chain = workflow_prompt | LLM.with_structured_output(schema = WorkflowDependencyGraph)
    workflow_graph = workflow_chain.invoke({"script_dependency_profiles": profiles_to_json(profiles)})
    
    return workflow_graph


#%%
"""
Tools for building script summaries
"""

def object_to_json(object):
    # Converts a child class of Pydantic Basemodel to JSON format
    return json.dumps(object.model_dump(), indent = 2, ensure_ascii = False)


def retrieve_attribute_from_workflow_graph(script: str, attribute: str, workflow_graph: WorkflowDependencyGraph):
    """
    Helper function to retrieve the specified attribute of the specified script from the workflow dependency graph
    e.g. retrieve the <stage_order_ID> attribute of preprocessing.py from the workflow dependency graph
    """
    for node in workflow_graph.nodes:
        if node.script_name == script:
            if not hasattr(node, attribute):
                raise AttributeError(f"WorkflowNode has no attribute: {attribute}")
            return getattr(node, attribute, None)
    return None


async def generate_summary_for_file(file: Path,
                                    script_profile: ScriptDependencyProfile, 
                                    workflow_graph: WorkflowDependencyGraph,
                                    semaphore: asyncio.Semaphore,
                                    summary_chain) -> ScriptSummary:

    print(f"Generating summary for {file.name}...")
    # Basic information of file, can be retrieved directly from path info
    script_name = file.name
    script_path = str(file)
    script_type = detect_script_type(file)

    # AI-generated information of file, including it's stage ID (to reflect the processing stage the file handles), and order ID within each stage (e.g. B01)
    script_stage_order_ID = retrieve_attribute_from_workflow_graph(file.name, "stage_order_ID", workflow_graph)
    
    document = TextLoader(file_path = str(file), 
                          encoding = "utf-8", 
                          autodetect_encoding = True).load()[0]

    # Async API call
    payload = {"script_name": script_name, 
               "script_path": script_path, 
               "script_type": script_type, 
               "script_stage_order_ID": script_stage_order_ID,
               "script_content": document.page_content, 
               "script_dependency_profile": object_to_json(script_profile), 
               "workflow_dependency_graph": object_to_json(workflow_graph)}

    async with semaphore:
        return await summary_chain.ainvoke(payload)


async def generate_summary_for_folder(valid_file_list: list[Path], 
                                      dependency_profiles: list[ScriptDependencyProfile],
                                      workflow_graph: WorkflowDependencyGraph,
                                      model: Literal["OpenAI", "Ollama"] = "OpenAI",
                                      max_concurrent_files: int = 10) -> list[ScriptSummary]:
    
    LLM = set_up_LLM(model = model)
    summary_prompt = ChatPromptTemplate.from_template(Path("./prompts/prompt_generate_script_summary.md").read_text(encoding = "utf-8"))
    summary_chain = summary_prompt | LLM.with_structured_output(schema = ScriptSummary) # Script Summary class can be found in classes.py

    semaphore = asyncio.Semaphore(max_concurrent_files)

    profile_by_path = {profile.file_path: profile for profile in dependency_profiles}
    tasks = []

    for file in valid_file_list:
        profile = profile_by_path.get(str(file))

        if profile is None:
            pass # to be defined later
        tasks.append(generate_summary_for_file(file, profile, workflow_graph, semaphore, summary_chain))

    return await asyncio.gather(*tasks)


#%%
"""
Tools for building DA Document based on the following objects
    - ScriptDependencyProfile # for providing script info and dependencies
    - WorkflowDependencyGraph # for providing dependency network on flow chart construction
    - ScriptSummary # for providing script summary, as well as input file / output file profiles
"""
import html
import re
from classes import FlowchartSpec, FlowchartNode, FlowchartEdge, FlowchartEdgeKind, FlowchartNodeKind

def convert_object_to_json(object: list[ScriptDependencyProfile] | WorkflowDependencyGraph | list[ScriptSummary], 
                           output_path: str | Path) -> None:
    """
    Export the input object to JSON
    """
    if isinstance(output_path, str):
        output_path = Path(output_path)
        output_path.mkdir(exist_ok = True)

    if isinstance(object, list):
        # Export the list as a nested graph
        if all(isinstance(element, ScriptDependencyProfile) for element in object):
            (output_path / "profiles.json").write_text(data = json.dumps([profile.model_dump() for profile in object], 
                                                     indent = 2, 
                                                     ensure_ascii = False))

        elif all(isinstance(element, ScriptSummary) for element in object):
            (output_path / "summaries.json").write_text(data = json.dumps([profile.model_dump() for profile in object], 
                                                                 indent = 2, 
                                                                 ensure_ascii = False))

    elif isinstance(object, WorkflowDependencyGraph):
        (output_path / "dependency_network.json").write_text(data = object.model_dump_json(indent = 2), 
                               encoding = "utf-8")


def make_safe_id(value: str) -> str:
    """
    Convert a display value into a compact identifier for HTML/spec usage.

    The flowchart has two different needs: a readable label for people and a
    stable id for code. Labels may contain spaces, slashes, punctuation, or
    other characters that are awkward to use as object ids. This helper creates
    a simple lowercase ASCII id by replacing non-alphanumeric runs with
    underscores. It is mainly used for generated data nodes, where the original
    resource name may come from an LLM extraction. The original name is still
    preserved on the node label, so this function only affects internal ids.
    """
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "_", value)
    value = value.strip("_")
    return value or "unknown"


def make_data_node_id(resource_type: str, resource_name: str) -> str:
    """
    Build a namespaced id for a data resource node.

    Data nodes are created from dependency extraction results rather than from
    the workflow graph itself. Prefixing them with ``data_`` keeps them separate
    from script ids and reduces the chance of accidentally colliding with a
    script named the same thing as an input or output file.
    """
    return f"data_{make_safe_id(resource_type)}_{make_safe_id(resource_name)}"


def script_type_to_node_kind(script_type: str) -> FlowchartNodeKind:
    """
    Map a script type from the dependency graph to a visual node kind.

    The workflow graph stores script types in simple business terms such as
    ``python`` or ``sql``. The flowchart renderer uses more specific visual
    kinds such as ``python_script`` so it can assign consistent colors, labels,
    and icon text. Unknown script types are intentionally allowed and rendered
    with the fallback style instead of breaking the chart.
    """
    mapping: dict[str, FlowchartNodeKind] = {
        "python": "python_script",
        "sql": "sql_script",
        "alteryx": "alteryx_workflow",
        "bat": "bat_script",
    }

    return mapping.get(script_type.lower(), "unknown")


def resource_type_to_node_kind(resource_type: str) -> FlowchartNodeKind:
    """
    Map an extracted resource type to a visual node kind.

    Input and output resources can be files, tables, databases, APIs, or
    unknown resources. This helper translates the extracted resource type into
    the controlled vocabulary used by ``FlowchartNode.kind``. Keeping the
    mapping here means the rest of the renderer can work from one standard set
    of node kinds.
    """
    mapping: dict[str, FlowchartNodeKind] = {
        "file": "file",
        "table": "table",
        "database": "database",
        "api": "api",
        "unknown": "unknown",
    }

    return mapping.get(resource_type.lower(), "unknown")


def construct_flowchart_spec(
    workflow_graph: WorkflowDependencyGraph,
    profiles: list[ScriptDependencyProfile],
) -> FlowchartSpec:
    """
    Combine script workflow reasoning and file-level dependency profiles.

    This function converts the analysis objects into one renderer-friendly
    ``FlowchartSpec``. The workflow graph contributes script nodes and
    script-to-script relationships. The dependency profiles contribute input and
    output data nodes, plus read/write edges between those data resources and
    the scripts that use them. The output is intentionally a separate object
    from the final HTML, because it can be reviewed, edited, exported, or
    re-rendered with a different visual style later.

    The function also deduplicates nodes and edges as it builds the spec. That
    matters because several scripts may reference the same file, and repeated
    extraction results should not produce repeated cards in the chart. The
    resulting object is the clean handoff point between AI-assisted reasoning
    and deterministic visual rendering.
    """
    spec = FlowchartSpec(
        title="Workflow Flowchart",
        summary=workflow_graph.workflow_summary,
    )

    created_node_ids: set[str] = set()
    created_edge_keys: set[tuple[str, str, str, str | None]] = set()

    def add_node(node: FlowchartNode) -> None:
        # Keep one card per logical node, even if multiple dependencies mention it.
        if node.id not in created_node_ids:
            spec.nodes.append(node)
            created_node_ids.add(node.id)

    def add_edge(edge: FlowchartEdge) -> None:
        # Avoid visually duplicated arrows with the same source, target, kind, and label.
        key = (edge.source, edge.target, edge.kind, edge.label)
        if key not in created_edge_keys:
            spec.edges.append(edge)
            created_edge_keys.add(key)

    # Add script nodes from workflow graph
    for node in workflow_graph.nodes:
        add_node(
            FlowchartNode(
                id = node.id,
                label = node.script_name,
                kind = script_type_to_node_kind(node.script_type),
                stage_ID = node.stage_ID,
                stage_order_ID = node.stage_order_ID,
                subtitle = node.role,
                details = {
                    "script_path": node.script_path,
                    "script_type": node.script_type,
                    "graph_label": node.label,
                    "order_confidence": node.order_confidence,
                    "order_evidence": node.order_evidence,
                },
            )
        )

    script_node_by_path = {
        node.script_path: node.id
        for node in workflow_graph.nodes
    }

    script_node_by_name = {
        node.script_name: node.id
        for node in workflow_graph.nodes
    }

    # Add data nodes and read/write edges from dependency profiles
    for profile in profiles:
        script_node_id = script_node_by_path.get(profile.file_path)

        if script_node_id is None:
            script_node_id = script_node_by_name.get(profile.file_name)

        if script_node_id is None:
            continue

        for dependency in profile.dependencies:
            if dependency.relationship not in {"reads", "writes"}:
                continue

            resource_type = dependency.target.type
            resource_name = dependency.target.name

            data_node_id = make_data_node_id(resource_type, resource_name)

            add_node(
                FlowchartNode(
                    id = data_node_id,
                    label = resource_name,
                    kind = resource_type_to_node_kind(resource_type),
                    subtitle = resource_type,
                    details = {
                        "path": dependency.target.path,
                        "source_script": profile.file_name,
                        "dependency_details": dependency.details,
                    },
                )
            )

            if dependency.relationship == "reads":
                source = data_node_id
                target = script_node_id
                label = "reads"
            else:
                source = script_node_id
                target = data_node_id
                label = "writes"

            add_edge(
                FlowchartEdge(
                    source = source,
                    target = target,
                    kind = dependency.relationship,
                    label = label,
                    confidence = dependency.confidence,
                    evidence = dependency.evidence,
                )
            )

    # Add script-to-script edges from workflow graph
    for edge in workflow_graph.edges:
        add_edge(
            FlowchartEdge(
                source = edge.source,
                target = edge.target,
                kind = edge.relationship,
                label = edge.shared_resource or edge.relationship,
                confidence = edge.confidence,
                evidence = edge.evidence,
            )
        )

    return spec


def render_flowchart_html(flowchart_spec: FlowchartSpec, output_path: str | Path) -> None:
    """
    Render a FlowchartSpec as a standalone HTML file.

    This is the final deterministic rendering step in the workflow.
    It takes the already-structured ``FlowchartSpec`` and writes one complete
    HTML document that can be opened directly in a browser.
    The LLM should ideally decide what the nodes and edges mean before this
    function runs; this function decides how that information is displayed.

    The renderer keeps the visual style standardized across projects.
    It defines the page shell, color palette, legend, canvas, script cards,
    data cards, SVG arrows, and hover behavior for relationship labels.
    Because the HTML is generated from a schema, the output can be reproduced
    consistently after a human edits ``flowchart_spec.json``.

    The layout is intentionally simple and predictable.
    Nodes are placed into left-to-right layers based on graph dependencies.
    The canvas then grows horizontally and vertically to fit those nodes.
    Edges are rendered as orthogonal SVG paths so arrows turn at right angles.
    Relationship labels are hidden until hover to avoid clutter in dense charts.

    In practice, this function is the main place to tune the look and feel.
    Change colors, fonts, node dimensions, legend styling, or hover effects
    here when you want a different department-wide visual standard.
    """
    if isinstance(output_path, str):
        output_path = Path(output_path)

    output_path.parent.mkdir(exist_ok = True)

    # Build coordinates first, then size the canvas around the resulting layout.
    layer_by_node = calculate_flowchart_layers(flowchart_spec)
    layer_count = max(layer_by_node.values(), default = 0) + 1
    canvas_width = max(1500, layer_count * 520 + 160)
    positions = calculate_flowchart_positions(flowchart_spec, canvas_width = canvas_width)
    node_lookup = {node.id: node for node in flowchart_spec.nodes}

    max_x = max((position["x"] for position in positions.values()), default = 0)
    max_y = max((position["y"] for position in positions.values()), default = 0)
    canvas_width = max(canvas_width, max_x + 360)
    canvas_height = max(720, max_y + 180)

    # Render cards and arrows separately so cards can sit visually above edges.
    node_html = "\n".join(
        build_flowchart_node_html(node, positions[node.id])
        for node in flowchart_spec.nodes
        if node.id in positions
    )

    edge_svg = "\n".join(
        build_flowchart_edge_svg(edge, positions, node_lookup)
        for edge in flowchart_spec.edges
        if edge.source in positions and edge.target in positions
    )

    title = escape_html(flowchart_spec.title)
    summary = escape_html(flowchart_spec.summary or "")

    # The HTML is standalone: CSS, SVG markers, nodes, and edges all live here.
    html_output = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    :root {{
      --bg: #f6f8fb;
      --surface: #ffffff;
      --ink: #172033;
      --muted: #667085;
      --border: #d0d7e2;
      --line: #8390a3;
      --file: #108765;
      --table: #7b4fd6;
      --database: #0f766e;
      --api: #0478a8;
      --python: #2563c7;
      --sql: #6d45c5;
      --alteryx: #c95f1b;
      --bat: #4b5563;
      --unknown: #778194;
    }}

    * {{ box-sizing: border-box; }}

    body {{
      margin: 0;
      min-height: 100vh;
      font-family: "Cambria Math", Cambria, Georgia, serif;
      font-weight: 700;
      color: var(--ink);
      background:
        linear-gradient(180deg, rgba(255,255,255,0.92), rgba(246,248,251,0.96)),
        var(--bg);
    }}

    header {{
      padding: 24px 32px 18px;
      border-bottom: 1px solid var(--border);
      background: rgba(255,255,255,0.92);
      position: sticky;
      top: 0;
      z-index: 10;
    }}

    h1 {{
      margin: 0;
      font-size: 24px;
      line-height: 1.2;
      letter-spacing: 0;
    }}

    .summary {{
      margin-top: 7px;
      max-width: 980px;
      color: var(--muted);
      font-size: 13px;
      font-weight: 700;
      line-height: 1.45;
    }}

    .legend {{
      display: flex;
      flex-wrap: wrap;
      gap: 9px;
      margin-top: 15px;
    }}

    .legend span {{
      display: inline-flex;
      align-items: center;
      gap: 7px;
      padding: 6px 9px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--surface);
      color: #475467;
      font-size: 12px;
      font-weight: 700;
    }}

    .swatch {{
      width: 11px;
      height: 11px;
      border-radius: 3px;
      display: inline-block;
    }}

    main {{
      padding: 28px;
      overflow: auto;
    }}

    .canvas {{
      position: relative;
      width: {canvas_width}px;
      height: {canvas_height}px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: #ffffff;
      box-shadow: 0 16px 38px rgba(16,24,40,0.08);
    }}

    svg.edges {{
      position: absolute;
      inset: 0;
      width: {canvas_width}px;
      height: {canvas_height}px;
      z-index: 1;
      pointer-events: auto;
      overflow: visible;
    }}

    .node {{
      position: absolute;
      z-index: 3;
      width: 230px;
      min-height: 92px;
      display: grid;
      grid-template-columns: 42px 1fr;
      gap: 11px;
      align-items: start;
      padding: 13px;
      border: 1px solid var(--border);
      border-left: 6px solid var(--unknown);
      border-radius: 8px;
      background: rgba(255,255,255,0.98);
      box-shadow: 0 10px 22px rgba(16,24,40,0.10);
    }}

    .kind-file {{ border-left-color: var(--file); }}
    .kind-table {{ border-left-color: var(--table); }}
    .kind-database {{ border-left-color: var(--database); }}
    .kind-api {{ border-left-color: var(--api); }}
    .kind-python_script {{ border-left-color: var(--python); }}
    .kind-sql_script {{ border-left-color: var(--sql); }}
    .kind-alteryx_workflow {{ border-left-color: var(--alteryx); }}
    .kind-bat_script {{ border-left-color: var(--bat); }}

    .icon {{
      width: 36px;
      height: 36px;
      display: grid;
      place-items: center;
      border-radius: 8px;
      background: var(--unknown);
      color: #fff;
      font-size: 11px;
      font-weight: 800;
      letter-spacing: 0.02em;
    }}

    .kind-file .icon {{ background: var(--file); }}
    .kind-table .icon {{ background: var(--table); }}
    .kind-database .icon {{ background: var(--database); }}
    .kind-api .icon {{ background: var(--api); }}
    .kind-python_script .icon {{ background: var(--python); }}
    .kind-sql_script .icon {{ background: var(--sql); }}
    .kind-alteryx_workflow .icon {{ background: var(--alteryx); }}
    .kind-bat_script .icon {{ background: var(--bat); }}

    .node-kind {{
      margin-bottom: 4px;
      color: var(--muted);
      font-size: 10px;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }}

    .node-label {{
      font-size: 14px;
      line-height: 1.25;
      font-weight: 760;
      overflow-wrap: anywhere;
    }}

    .node-subtitle {{
      margin-top: 6px;
      color: var(--muted);
      font-size: 11px;
      font-weight: 700;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }}

    .stage-id {{
      display: inline-block;
      margin-top: 9px;
      padding: 4px 7px;
      border-radius: 7px;
      background: #eef2f7;
      color: #344054;
      font-size: 11px;
      font-weight: 700;
    }}

    .edge-path {{
      fill: none;
      stroke: var(--line);
      stroke-width: 2.2;
      marker-end: url(#arrow);
      pointer-events: none;
    }}

    .edge-low {{
      stroke-dasharray: 6 5;
      stroke-width: 1.8;
    }}

    .edge-hit {{
      fill: none;
      stroke: transparent;
      stroke-width: 18;
      pointer-events: stroke;
    }}

    .edge-tooltip-text {{
      opacity: 0;
      transition: opacity 120ms ease;
      pointer-events: none;
    }}

    .edge:hover .edge-tooltip-text {{
      opacity: 1;
    }}

    .edge:hover .edge-path {{
      stroke: #344054;
      stroke-width: 2.8;
    }}

    .edge-tooltip-text {{
      font: 14px "Cambria Math", Cambria, Georgia, serif;
      font-weight: 800;
      fill: #344054;
    }}
  </style>
</head>
<body>
  <header>
    <h1>{title}</h1>
    <div class="summary">{summary}</div>
    <div class="legend" aria-label="Flowchart legend">
      <span><i class="swatch" style="background: var(--file)"></i>File</span>
      <span><i class="swatch" style="background: var(--table)"></i>Table</span>
      <span><i class="swatch" style="background: var(--database)"></i>Database</span>
      <span><i class="swatch" style="background: var(--python)"></i>Python</span>
      <span><i class="swatch" style="background: var(--sql)"></i>SQL</span>
      <span><i class="swatch" style="background: var(--alteryx)"></i>Alteryx</span>
      <span><i class="swatch" style="background: var(--bat)"></i>BAT</span>
    </div>
  </header>
  <main>
    <section class="canvas" aria-label="Workflow flowchart">
      <svg class="edges" viewBox="0 0 {canvas_width} {canvas_height}" aria-hidden="true">
        <defs>
          <marker id="arrow" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto" markerUnits="strokeWidth">
            <path d="M0,0 L0,6 L9,3 z" fill="#8390a3"></path>
          </marker>
        </defs>
{edge_svg}
      </svg>
{node_html}
    </section>
  </main>
</body>
</html>
"""

    output_path.write_text(html_output, encoding = "utf-8")


def calculate_flowchart_positions(flowchart_spec: FlowchartSpec,
                                  canvas_width: int = 1500) -> dict[str, dict[str, int]]:
    """
    Calculate absolute pixel positions for every node in the flowchart.

    This function is the small layout engine behind the HTML chart.
    It first asks ``calculate_flowchart_layers`` which horizontal layer each
    node belongs to, then stacks nodes vertically within each layer.
    Layering gives the chart its left-to-right workflow direction.
    Row stacking keeps nodes in the same stage from overlapping.

    The returned dictionary maps each node id to an ``x`` and ``y`` coordinate.
    Those coordinates are later used by both ``build_flowchart_node_html`` and
    ``build_flowchart_edge_svg``. This shared coordinate system is important:
    if cards and arrows use different positioning logic, arrows drift away from
    their intended start and end points.

    The horizontal spacing is dynamic. The function uses the requested canvas
    width, left/right margins, and number of layers to decide how far apart
    columns should be. It still enforces a minimum spacing so dense charts do
    not collapse into each other. Within each layer, nodes are sorted by stage
    order, kind, and label to make the visual output stable across runs.

    If the chart ever looks too compressed or too spread out, this is the first
    function to inspect. The main tuning values are margins, node width, minimum
    layer spacing, top offset, and row spacing.
    """
    layer_by_node = calculate_flowchart_layers(flowchart_spec)
    nodes_by_layer: dict[int, list[FlowchartNode]] = {}

    # Group nodes by dependency layer before assigning actual x/y coordinates.
    for node in flowchart_spec.nodes:
        layer = layer_by_node.get(node.id, 0)
        nodes_by_layer.setdefault(layer, []).append(node)

    positions: dict[str, dict[str, int]] = {}
    layer_count = max(nodes_by_layer.keys(), default = 0) + 1
    left_margin = 48
    right_margin = 48
    node_width = 230

    if layer_count <= 1:
        layer_spacing = 0
    else:
        # Spread layers across the available canvas while keeping a readable gap.
        usable_width = max(canvas_width - left_margin - right_margin - node_width, 360)
        layer_spacing = max(360, usable_width // (layer_count - 1))

    for layer, nodes in nodes_by_layer.items():
        nodes.sort(key = lambda node: (node.stage_order_ID or "", node.kind, node.label))
        for row, node in enumerate(nodes):
            positions[node.id] = {
                "x": left_margin + layer * layer_spacing,
                "y": 70 + row * 138
            }

    return positions


def calculate_flowchart_layers(flowchart_spec: FlowchartSpec) -> dict[str, int]:
    """
    Assign each flowchart node to a left-to-right dependency layer.

    The goal of this function is to create a readable workflow direction.
    Nodes with no incoming edges are placed at the beginning of the chart.
    Their downstream targets are placed one layer to the right, and so on.
    This makes inputs and earlier scripts appear toward the left, while
    outputs and later scripts move toward the right.

    The implementation uses a simple topological-style pass.
    It counts incoming edges for each node, starts with nodes that have no
    incoming edges, and repeatedly pushes target nodes to later layers.
    If a node can be reached through multiple paths, it keeps the furthest
    layer required by those paths. That helps avoid placing a downstream node
    too early when it depends on several upstream resources.

    Graphs produced by LLM reasoning can sometimes have cycles or ambiguous
    relationships. Rather than failing, this function gives any unresolved node
    a fallback layer of zero. That keeps the renderer robust while still making
    imperfect workflow reasoning visible for human review.

    This function does not decide exact pixel positions. It only decides the
    conceptual column for each node. ``calculate_flowchart_positions`` converts
    those columns into actual screen coordinates.
    """
    node_ids = {node.id for node in flowchart_spec.nodes}
    incoming_count = {node_id: 0 for node_id in node_ids}
    outgoing: dict[str, list[str]] = {node_id: [] for node_id in node_ids}

    # Build a compact adjacency view from the edge list.
    for edge in flowchart_spec.edges:
        if edge.source not in node_ids or edge.target not in node_ids:
            continue
        outgoing[edge.source].append(edge.target)
        incoming_count[edge.target] += 1

    ready = sorted([node_id for node_id, count in incoming_count.items() if count == 0])
    layer_by_node = {node_id: 0 for node_id in ready}

    while ready:
        current = ready.pop(0)
        current_layer = layer_by_node.get(current, 0)

        # Push downstream nodes at least one layer to the right of this node.
        for target in outgoing[current]:
            layer_by_node[target] = max(layer_by_node.get(target, 0), current_layer + 1)
            incoming_count[target] -= 1
            if incoming_count[target] == 0:
                ready.append(target)
                ready.sort()

    for node_id in sorted(node_ids):
        layer_by_node.setdefault(node_id, 0)

    return layer_by_node


def build_flowchart_node_html(node: FlowchartNode, position: dict[str, int]) -> str:
    """
    Render one flowchart node card as positioned HTML.

    Each node in the ``FlowchartSpec`` becomes one card on the canvas.
    The card displays a compact icon, a kind label, the main node label, an
    optional subtitle, and an optional stage/order badge. The ``position``
    argument supplies the exact top-left pixel coordinate calculated by the
    layout function.

    This function deliberately emits normal HTML instead of SVG for cards.
    HTML text wrapping is easier to control for long file names, paths, and
    script names. It also lets the browser handle accessible text selection and
    layout details more naturally. The SVG layer is reserved for arrows.

    The node kind is converted into a CSS class such as ``kind-python_script``
    or ``kind-file``. The stylesheet then applies the correct color stripe and
    icon background. This keeps the schema clean: the data says what type of
    node it is, while CSS decides how that type should look.

    If you want to change the card appearance, this function controls the HTML
    structure, while ``render_flowchart_html`` controls the surrounding CSS.
    """
    kind_class = f"kind-{node.kind}"
    icon = node_kind_to_icon_text(node.kind)
    stage = node.stage_order_ID or node.stage_ID
    stage_html = f'<div class="stage-id">{escape_html(stage)}</div>' if stage else ""

    return f"""      <article class="node {kind_class}" style="left: {position["x"]}px; top: {position["y"]}px;">
        <div class="icon">{escape_html(icon)}</div>
        <div>
          <div class="node-kind">{escape_html(node.kind.replace("_", " "))}</div>
          <div class="node-label">{escape_html(node.label)}</div>
          <div class="node-subtitle">{escape_html(node.subtitle or "")}</div>
          {stage_html}
        </div>
      </article>"""


def build_flowchart_edge_svg(edge: FlowchartEdge,
                             positions: dict[str, dict[str, int]],
                             node_lookup: dict[str, FlowchartNode]) -> str:
    """
    Render one relationship edge as an SVG group.

    This function draws the arrow connecting a source node to a target node.
    It uses the same pixel positions as the node cards, then calculates anchor
    points on the right side of the source card and the left side of the target
    card. For normal left-to-right relationships, it draws an orthogonal path:
    horizontal out of the source, vertical if needed, then horizontal into the
    target. For backward or same-layer relationships, it routes from the bottom
    of the source toward the top of the target.

    The edge is returned as an SVG ``g`` group containing three pieces.
    The visible path is the actual arrow.
    The transparent ``edge-hit`` path creates a wider hover target so the user
    does not need to place the mouse exactly on a thin line.
    The text element contains the relationship label, hidden until hover.

    Keeping labels hidden by default avoids clutter when many edges merge.
    On hover, CSS reveals the label and emphasizes the arrow, making it clear
    which dependency is being inspected. The label text is display-formatted by
    ``format_edge_label`` so internal values like ``code_dependency`` become
    human-friendly labels such as ``Code Dependency``.

    This function is the main place to tune arrow geometry, routing style,
    hover label position, and low-confidence edge styling.
    """
    source = positions[edge.source]
    target = positions[edge.target]
    source_x = source["x"] + 230
    source_y = source["y"] + 46
    target_x = target["x"]
    target_y = target["y"] + 46

    if target_x <= source_x:
        # Same-layer or backward edges need a different route to avoid card overlap.
        source_x = source["x"] + 115
        source_y = source["y"] + 92
        target_x = target["x"] + 115
        target_y = target["y"]
        mid_y = source_y + max(36, (target_y - source_y) // 2)
        path = f"M{source_x} {source_y} L{source_x} {mid_y} L{target_x} {mid_y} L{target_x} {target_y}"
        label_x = int((source_x + target_x) / 2)
        label_y = int(mid_y) - 12
    else:
        # Standard forward edge: right side of source to left side of target.
        mid_x = int((source_x + target_x) / 2)
        path = f"M{source_x} {source_y} L{mid_x} {source_y} L{mid_x} {target_y} L{target_x} {target_y}"

        if abs(source_y - target_y) > 24:
            label_x = int((source_x + mid_x) / 2)
            label_y = int(source_y) - 12
        else:
            label_x = int((source_x + target_x) / 2)
            label_y = int(source_y) - 12

    label = escape_html(format_edge_label(edge.label or edge.kind))
    edge_class = "edge-path edge-low" if edge.confidence == "low" else "edge-path"

    return f"""        <g class="edge">
          <path class="{edge_class}" d="{path}"></path>
          <path class="edge-hit" d="{path}"></path>
          <text class="edge-tooltip-text" x="{label_x}" y="{label_y}" text-anchor="middle">{label}</text>
        </g>"""


def format_edge_label(value: str) -> str:
    """
    Convert schema-style relationship names into display labels.

    The flowchart data stores relationship kinds in machine-friendly values
    such as ``code_dependency``. The chart should show human-friendly labels
    such as ``Code Dependency``. This helper keeps that transformation in one
    place so the underlying schema remains stable while the visual text can be
    polished for readers.
    """
    return value.replace("_", " ").title()


def node_kind_to_icon_text(kind: FlowchartNodeKind) -> str:
    """
    Return the short text shown inside a node icon.

    The current renderer uses compact text badges instead of image icons.
    This helper keeps that mapping centralized: Python scripts show ``PY``,
    SQL scripts show ``SQL``, files show ``FILE``, and so on. The result is
    deliberately short because it has to fit inside a small square icon.
    """
    mapping = {
        "file": "FILE",
        "table": "TBL",
        "database": "DB",
        "api": "API",
        "python_script": "PY",
        "sql_script": "SQL",
        "alteryx_workflow": "AX",
        "bat_script": "BAT",
        "unknown": "?"
    }
    return mapping.get(kind, "?")


def escape_html(value: object) -> str:
    """
    Escape a value before inserting it into generated HTML.

    Script names, file paths, and LLM-extracted labels may contain characters
    that have special meaning in HTML. Escaping them prevents accidental markup
    breakage and makes the renderer safer when displaying arbitrary project
    content.
    """
    return html.escape(str(value), quote = True)
