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
from typing import Callable, Literal
from pydantic import BaseModel

BACKEND_DIR = Path(__file__).parent
PROMPTS_DIR = BACKEND_DIR / "prompts"
FLOWCHART_ICON_DIR = BACKEND_DIR / "assets" / "flowchart_icons"

#%%
"""
Tools for checking input directory validity and list all valid scripts
"""

def script_folder_exists(script_path: str | Path) -> bool:
    if isinstance(script_path, str):
        script_path = Path(script_path)
    return script_path.is_dir()


def list_all_scripts(script_path: str | Path,
                     logger: Callable[[str], None] | None = None) -> list[Path]:
    """
    Find supported scripts in the selected folder.
    - Searches nested folders with rglob
    - Ignores common generated / environment folders
    - Reports discovered files through logger when provided
    """
    def log(message: str) -> None:
        if logger is not None:
            logger(message)

    script_path = Path(script_path)
    if script_folder_exists(script_path):
        log(f"Scanning scripts recursively in: {script_path}")

        valid_file_suffixes = {".py", ".sql", ".yxmd", ".yxwz", ".bat"} # Only allow for python, SQL, Alteryx and BAT files
        ignored_folder_names = {"__pycache__", ".git", ".venv", "venv", "env", "node_modules"}
        valid_file_list = [
            file
            for file in script_path.rglob("*")
            if file.is_file()
            and file.suffix.lower() in valid_file_suffixes
            and not any(parent.name in ignored_folder_names for parent in file.parents)
        ]

        for file in valid_file_list:
            log(f"Found script: {file.relative_to(script_path)}")
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

    import_prompt = ChatPromptTemplate.from_template((PROMPTS_DIR / "prompt_detect_imports.md").read_text(encoding = "utf-8"))
    input_data_prompt = ChatPromptTemplate.from_template((PROMPTS_DIR / "prompt_detect_input_data.md").read_text(encoding = "utf-8"))
    output_data_prompt = ChatPromptTemplate.from_template((PROMPTS_DIR / "prompt_detect_output_data.md").read_text(encoding = "utf-8"))

    structured_LLM = LLM.with_structured_output(DependencyExtraction)

    return {"imports": import_prompt | structured_LLM,
            "input data": input_data_prompt | structured_LLM,
            "output data": output_data_prompt | structured_LLM}


async def extract_dependencies_for_file(file: Path,
                                        chains: dict,
                                        semaphore: asyncio.Semaphore,
                                        output_language: str = "English",
                                        logger: Callable[[str], None] | None = None) -> ScriptDependencyProfile:
    """
    For each file passed in, pass the content of the file to LLM and buidl a ScriptDependencyProfile object that contains the following information:
        - script name
        - script directory
        - script type,
        - list of imported modules
        - list of input data
        - list of output data
    """
    def log(message: str) -> None:
        if logger is not None:
            logger(message)

    log(f"Extracting dependency profile: {file.name}")
    document = TextLoader(file_path = str(file), encoding = "utf-8", autodetect_encoding = True).load()[0]
    payload = {
        "script_content": document.page_content,
        "output_language": output_language,
    }
    import_result, input_data_result, output_data_result = await asyncio.gather(
        invoke_dependency_chain(chains["imports"], payload, semaphore, f"{file.name}: imports", logger = logger),
        invoke_dependency_chain(chains["input data"], payload, semaphore, f"{file.name}: input data", logger = logger),
        invoke_dependency_chain(chains["output data"], payload, semaphore, f"{file.name}: output data", logger = logger)
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

    log(f"Finished dependency profile: {file.name}")
    return profile


async def invoke_dependency_chain(chain,
                                  payload: dict,
                                  semaphore: asyncio.Semaphore,
                                  task_name: str,
                                  logger: Callable[[str], None] | None = None,
                                  max_attempts: int = 3) -> DependencyExtraction:
    """
    Invoke one dependency extraction chain with retry handling.
    Local Ollama models may occasionally return malformed or empty structured output,
    especially under concurrent load.
    """
    def log(message: str) -> None:
        if logger is not None:
            logger(message)

    for attempt in range(1, max_attempts + 1):
        try:
            async with semaphore:
                log(f"Running extraction task: {task_name}")
                result = await chain.ainvoke(payload)
                log(f"Finished extraction task: {task_name}")
                return result
        except Exception as error:
            if attempt == max_attempts:
                log(f"Extraction task failed: {task_name}")
                return DependencyExtraction(
                    unclear_items = [
                        f"{task_name} failed after {max_attempts} attempts: {error}"
                    ]
                )
            log(f"Retrying extraction task: {task_name} (attempt {attempt + 1} of {max_attempts})")
            await asyncio.sleep(attempt)


async def extract_dependencies_for_folder(valid_file_list: list[Path],
                                          chains: dict,
                                          max_concurrent_files: int = 1,
                                          output_language: str = "English",
                                          logger: Callable[[str], None] | None = None):
    """
    Batch execution for extract_dependencies_for_file, with asynchronus implementation for processing efficiency
    """
    def log(message: str) -> None:
        if logger is not None:
            logger(message)

    log(f"Starting dependency extraction for {len(valid_file_list)} file(s).")
    semaphore = asyncio.Semaphore(max_concurrent_files)
    tasks = [extract_dependencies_for_file(file = file, 
                                        chains = chains,
                                        semaphore = semaphore,
                                        output_language = output_language,
                                        logger = logger) for file in valid_file_list]

    profiles = await asyncio.gather(*tasks)
    log(f"Completed dependency extraction for {len(profiles)} file(s).")
    return profiles


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
                                 model: Literal["OpenAI", "Ollama"] = "OpenAI",
                                 output_language: str = "English",
                                 logger: Callable[[str], None] | None = None) -> WorkflowDependencyGraph:

    def log(message: str) -> None:
        if logger is not None:
            logger(message)

    log("Calling LLM to construct workflow dependency network.")
    LLM = set_up_LLM(model = model)
    
    workflow_prompt = ChatPromptTemplate.from_template((PROMPTS_DIR / "prompt_construct_dependency_network.md").read_text(encoding = "utf-8"))
    workflow_chain = workflow_prompt | LLM.with_structured_output(schema = WorkflowDependencyGraph)
    workflow_graph = workflow_chain.invoke({
        "script_dependency_profiles": profiles_to_json(profiles),
        "output_language": output_language,
    })
    
    log(f"Workflow dependency network contains {len(workflow_graph.nodes)} node(s) and {len(workflow_graph.edges)} edge(s).")
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
                                    summary_chain,
                                    output_language: str = "English",
                                    logger: Callable[[str], None] | None = None) -> ScriptSummary:

    def log(message: str) -> None:
        if logger is not None:
            logger(message)

    log(f"Generating script summary: {file.name}")
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
               "workflow_dependency_graph": object_to_json(workflow_graph),
               "output_language": output_language}

    async with semaphore:
        summary = await summary_chain.ainvoke(payload)

    log(f"Finished script summary: {file.name}")
    return summary


async def generate_summary_for_folder(valid_file_list: list[Path], 
                                      dependency_profiles: list[ScriptDependencyProfile],
                                      workflow_graph: WorkflowDependencyGraph,
                                      model: Literal["OpenAI", "Ollama"] = "OpenAI",
                                      max_concurrent_files: int = 10,
                                      output_language: str = "English",
                                      logger: Callable[[str], None] | None = None) -> list[ScriptSummary]:
    """
    Generate ScriptSummary objects for all supported scripts.
    - Uses the workflow graph to fill stage / order information
    - Uses dependency profiles to provide context for each script
    - Reports per-file progress through the shared logger
    """
    def log(message: str) -> None:
        if logger is not None:
            logger(message)
    
    log(f"Setting up summary chain for {len(valid_file_list)} file(s).")
    LLM = set_up_LLM(model = model)
    summary_prompt = ChatPromptTemplate.from_template((PROMPTS_DIR / "prompt_generate_script_summary.md").read_text(encoding = "utf-8"))
    summary_chain = summary_prompt | LLM.with_structured_output(schema = ScriptSummary) # Script Summary class can be found in classes.py

    semaphore = asyncio.Semaphore(max_concurrent_files)

    profile_by_path = {profile.file_path: profile for profile in dependency_profiles}
    tasks = []

    for file in valid_file_list:
        profile = profile_by_path.get(str(file))

        if profile is None:
            log(f"Skipping summary because dependency profile is missing: {file.name}")
            continue
        tasks.append(generate_summary_for_file(file, profile, workflow_graph, semaphore, summary_chain, output_language = output_language, logger = logger))

    summaries = await asyncio.gather(*tasks)
    log(f"Completed script summaries for {len(summaries)} file(s).")
    return summaries


#%%
"""
Tools for building objects used for DA Document based on the following objects
    - ScriptDependencyProfile # for providing script info and dependencies
    - WorkflowDependencyGraph # for providing dependency network on flow chart construction
    - ScriptSummary # for providing script summary, as well as input file / output file profiles
"""
import html
import re
from classes import FlowchartSpec, FlowchartNode, FlowchartEdge, FlowchartEdgeKind, FlowchartNodeKind

FLOWCHART_NODE_WIDTH = 300
FLOWCHART_NODE_HEIGHT = 82

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
    Helper for building internal flowchart IDs.
    - Converts display text into a lowercase identifier
    - Replaces spaces / punctuation with underscores
    - Keeps the readable name separately on the node label
    """
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "_", value)
    value = value.strip("_")
    return value or "unknown"


def make_data_node_id(resource_type: str, resource_name: str) -> str:
    """
    Helper for building IDs for data resource nodes.
    - Prefixes the ID with data_
    - Includes resource type and resource name
    - Keeps data node IDs separate from script node IDs
    """
    return f"data_{make_safe_id(resource_type)}_{make_safe_id(resource_name)}"


def script_type_to_node_kind(script_type: str) -> FlowchartNodeKind:
    """
    Helper for mapping script type to visual node type.
    - python -> python_script
    - sql -> sql_script
    - alteryx -> alteryx_workflow
    - unknown values use the fallback node style
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
    Helper for mapping data resource type to visual node type.
    - file -> file node
    - table -> table node
    - database -> database node
    - unknown values use the fallback node style
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
    summaries: list[ScriptSummary] | None = None,
) -> FlowchartSpec:
    """
    Tool for building FlowchartSpec based on the following objects:
    - WorkflowDependencyGraph # script nodes and script-to-script relationships
    - ScriptDependencyProfile # input data, output data, and dependency refs
    - ScriptSummary # high-level and detailed summaries for script hover/click UI

    For each script in the workflow graph:
    - Create one script node
    - Preserve script type, role, stage ID, and order ID
    - Attach script summary details when a matching ScriptSummary is available
    - Add script-to-script dependency edges

    For each dependency profile:
    - Create data nodes for files / tables / databases used by scripts
    - Add read edges from data nodes to scripts
    - Add write edges from scripts to data nodes

    Return a FlowchartSpec object that can be reviewed, edited, and passed to
    render_flowchart_html for standardized HTML output.
    """
    # This object is the bridge between analysis output and visual output.
    # The HTML renderer should not need to know about ScriptDependencyProfile
    # or WorkflowDependencyGraph directly.
    spec = FlowchartSpec(
        title="Workflow Flowchart",
        summary=workflow_graph.workflow_summary,
    )

    # These sets keep the visual graph tidy when the same file or relationship
    # is discovered more than once by different extraction steps.
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

    # Build lookup tables so summaries can be attached even when one object
    # identifies scripts by filename and another identifies them by full path.
    summary_by_name = {
        summary.script_name: summary
        for summary in summaries or []
    }

    summary_by_path = {
        summary.script_location: summary
        for summary in summaries or []
    }

    # Add script nodes from workflow graph.
    # These nodes are the main processing steps shown in the flowchart.
    for node in workflow_graph.nodes:
        script_summary = summary_by_path.get(node.script_path)

        if script_summary is None:
            script_summary = summary_by_name.get(node.script_name)

        # Details are not all printed directly on the card.
        # Some are used by hover/click interactions in the generated HTML.
        details = {
            "script_path": node.script_path,
            "script_type": node.script_type,
            "graph_label": node.label,
            "order_confidence": node.order_confidence,
            "order_evidence": node.order_evidence,
        }

        if script_summary is not None:
            details.update(
                {
                    "script_high_level_summary": script_summary.script_high_level_summary,
                    "script_detailed_summary": script_summary.script_detailed_summary,
                    "script_input_data": script_summary.script_input_data,
                    "script_output_data": script_summary.script_output_data,
                    "script_summary_stage_order_ID": script_summary.script_stage_order_ID,
                }
            )

        add_node(
            FlowchartNode(
                id = node.id,
                label = node.script_name,
                kind = script_type_to_node_kind(node.script_type),
                stage_ID = node.stage_ID,
                stage_order_ID = node.stage_order_ID,
                subtitle = node.role,
                details = details,
            )
        )

    # Profiles usually describe a script by file path / file name.
    # These maps let us connect each profile back to its script node.
    script_node_by_path = {
        node.script_path: node.id
        for node in workflow_graph.nodes
    }

    script_node_by_name = {
        node.script_name: node.id
        for node in workflow_graph.nodes
    }

    # Add data nodes and read/write edges from dependency profiles.
    # This is how files, tables, databases, and similar resources appear
    # together with scripts in the final flowchart.
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

            # One data resource becomes one card, even if multiple scripts use it.
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

            # Reads point from data resource -> script.
            # Writes point from script -> data resource.
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

    # Add script-to-script edges from workflow graph.
    # These show code dependency / execution dependency between scripts.
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
    Tool for building HTML flowchart output based on FlowchartSpec.

    The FlowchartSpec should already contain:
    - nodes # scripts, files, tables, databases, etc.
    - edges # read, write, code dependency, execution dependency, etc.
    - title / summary # text to show at the top of the HTML report

    This function is responsible for:
    - calculating node positions
    - creating the HTML page shell
    - defining CSS styles for cards, icons, colors, and fonts
    - drawing orthogonal SVG arrows between nodes
    - showing relationship labels when the user hovers over an edge

    The output is a standalone HTML file. This keeps visual generation
    deterministic and consistent across projects.
    """
    if isinstance(output_path, str):
        output_path = Path(output_path)

    output_path.parent.mkdir(exist_ok = True)

    # Build coordinates first, then size the canvas around the resulting layout.
    # The first width estimate gives the layout enough room to spread layers.
    layer_by_node = calculate_flowchart_layers(flowchart_spec)
    layer_count = max(layer_by_node.values(), default = 0) + 1
    canvas_width = max(1500, layer_count * 560 + 160)
    positions = calculate_flowchart_positions(flowchart_spec, canvas_width = canvas_width)
    node_lookup = {node.id: node for node in flowchart_spec.nodes}
    anchor_lookup = calculate_flowchart_edge_anchors(flowchart_spec, positions)

    # After positions are known, resize the canvas to include every card.
    # This prevents cards from being clipped and allows browser scrollbars.
    max_x = max((position["x"] for position in positions.values()), default = 0)
    max_y = max((position["y"] for position in positions.values()), default = 0)
    canvas_width = max(canvas_width, max_x + FLOWCHART_NODE_WIDTH + 80)
    canvas_height = max(720, max_y + 180)

    # Render cards and arrows separately.
    # SVG edges are drawn first; HTML cards sit above them visually.
    node_html = "\n".join(
        build_flowchart_node_html(node, positions[node.id])
        for node in flowchart_spec.nodes
        if node.id in positions
    )

    edge_svg = "\n".join(
        build_flowchart_edge_svg(edge, positions, node_lookup, anchor_lookup)
        for edge in flowchart_spec.edges
        if edge.source in positions and edge.target in positions
    )

    title = escape_html(flowchart_spec.title)
    summary = escape_html(flowchart_spec.summary or "")

    # Keep the long HTML / CSS / JavaScript in a template file so the Python
    # code stays focused on data preparation and layout.
    template_path = Path(__file__).parent / "templates" / "workflow_flowchart.html"
    template = template_path.read_text(encoding = "utf-8")

    html_output = render_html_template(
        template = template,
        context = {
            "title": title,
            "summary": summary,
            "canvas_width": canvas_width,
            "canvas_height": canvas_height,
            "edge_svg": edge_svg,
            "node_html": node_html,
        },
    )

    output_path.write_text(html_output, encoding = "utf-8")


def render_html_template(template: str, context: dict[str, object]) -> str:
    """
    Helper for rendering standalone HTML templates.
    - Uses {{ placeholder }} tokens in the template file
    - Replaces each token with the provided context value
    - Keeps HTML / CSS / JavaScript outside the Python f-string
    """
    rendered_template = template

    for key, value in context.items():
        rendered_template = rendered_template.replace(
            f"{{{{ {key} }}}}",
            str(value),
        )

    return rendered_template


def calculate_flowchart_positions(flowchart_spec: FlowchartSpec,
                                  canvas_width: int = 1500) -> dict[str, dict[str, int]]:
    """
    Tool for calculating node positions for HTML flowchart output.

    For each FlowchartNode in FlowchartSpec:
    - Retrieve the dependency layer
    - Assign an x coordinate based on the layer
    - Assign a y coordinate based on the row inside the layer
    - Return positions as {node_id: {"x": ..., "y": ...}}

    This function controls the chart's spacing:
    - left / right margin
    - minimum layer gap
    - vertical row gap
    - stable sorting within each layer
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
    node_width = FLOWCHART_NODE_WIDTH

    if layer_count <= 1:
        layer_spacing = 0
    else:
        # Spread layers across the available canvas while keeping a readable gap.
        usable_width = max(canvas_width - left_margin - right_margin - node_width, 360)
        layer_spacing = max(400, usable_width // (layer_count - 1))

    # Terminal nodes are resources or scripts that do not feed anything else.
    # We push them lower so important processing paths remain easier to follow.
    outgoing_count = calculate_flowchart_outgoing_counts(flowchart_spec)

    for layer, nodes in nodes_by_layer.items():
        # Sorting is deterministic, so the same graph produces the same layout
        # across reruns unless the underlying graph changes.
        nodes.sort(key = lambda node: flowchart_node_position_sort_key(node, outgoing_count))

        y = 58
        previous_group = None
        for node in nodes:
            current_group = 1 if outgoing_count.get(node.id, 0) == 0 else 0

            # Terminal nodes are placed after still-active nodes with a little extra air.
            if previous_group is not None and current_group != previous_group:
                y += 42

            positions[node.id] = {
                "x": left_margin + layer * layer_spacing,
                "y": y,
            }
            # Vertical gap between cards in the same layer.
            y += 136
            previous_group = current_group

    return positions


def calculate_flowchart_outgoing_counts(flowchart_spec: FlowchartSpec) -> dict[str, int]:
    """
    Helper for identifying terminal nodes in the visual layout.
    - Counts how many downstream edges each node has
    - Nodes with zero outgoing edges are likely final outputs or dead ends
    - The layout uses this to place terminal nodes lower in their layer
    """
    outgoing_count = {node.id: 0 for node in flowchart_spec.nodes}
    node_ids = set(outgoing_count)

    for edge in flowchart_spec.edges:
        if edge.source in node_ids and edge.target in node_ids:
            outgoing_count[edge.source] += 1

    return outgoing_count


def flowchart_node_position_sort_key(
    node: FlowchartNode,
    outgoing_count: dict[str, int],
) -> tuple[int, int, str, str, str]:
    """
    Helper for stable vertical ordering within each layer.
    - Nodes that continue to later processing stay nearer the top
    - Terminal outputs or one-off resources move lower
    - Scripts are kept before data resources when other values are equal
    """
    is_terminal = 1 if outgoing_count.get(node.id, 0) == 0 else 0
    script_priority = 0 if node.kind in {
        "python_script",
        "sql_script",
        "alteryx_workflow",
        "bat_script",
    } else 1

    return (
        is_terminal,
        script_priority,
        node.stage_order_ID or "",
        node.kind,
        node.label,
    )


def calculate_flowchart_layers(flowchart_spec: FlowchartSpec) -> dict[str, int]:
    """
    Tool for assigning each flowchart node to a workflow layer.

    For each edge in FlowchartSpec:
    - Count incoming relationships
    - Build a source-to-target adjacency map
    - Start from nodes with no incoming dependencies
    - Push downstream nodes to later layers

    The output is a mapping:
    - node ID -> layer number

    This function decides the conceptual left-to-right order.
    calculate_flowchart_positions then converts the layer number into pixels.
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

    # Nodes with no incoming edges can start at the left-most layer.
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

    # Cycles or disconnected nodes may not be reached by the traversal.
    # Put them in layer 0 rather than dropping them from the chart.
    for node_id in sorted(node_ids):
        layer_by_node.setdefault(node_id, 0)

    return layer_by_node


def calculate_flowchart_edge_anchors(
    flowchart_spec: FlowchartSpec,
    positions: dict[str, dict[str, int]],
) -> dict[tuple[str, str, str], dict[str, int]]:
    """
    Tool for assigning evenly distributed edge anchor positions.
    - Incoming anchors sit on the left side of the target card
    - Outgoing anchors sit on the right side of the source card
    - Multiple anchors are distributed vertically with equal spacing
    """
    outgoing_edges: dict[str, list[FlowchartEdge]] = {}
    incoming_edges: dict[str, list[FlowchartEdge]] = {}

    for edge in flowchart_spec.edges:
        if edge.source not in positions or edge.target not in positions:
            continue

        # Source-side anchors are distributed across the right side of a card.
        outgoing_edges.setdefault(edge.source, []).append(edge)

        # Target-side anchors are distributed across the left side of a card.
        incoming_edges.setdefault(edge.target, []).append(edge)

    anchor_lookup: dict[tuple[str, str, str], dict[str, int]] = {}

    for node_id, edges in outgoing_edges.items():
        # Sort by target location so nearby edges receive nearby anchors.
        edges.sort(key = lambda edge: (positions[edge.target]["y"], edge.target, edge.kind, edge.label or ""))
        for index, edge in enumerate(edges):
            anchor_lookup[(edge.source, edge.target, "source")] = {
                "x": positions[node_id]["x"] + FLOWCHART_NODE_WIDTH,
                "y": calculate_distributed_anchor_y(positions[node_id]["y"], len(edges), index),
            }

    for node_id, edges in incoming_edges.items():
        # Sort by source location so incoming edges do not cross more than needed.
        edges.sort(key = lambda edge: (positions[edge.source]["y"], edge.source, edge.kind, edge.label or ""))
        for index, edge in enumerate(edges):
            anchor_lookup[(edge.source, edge.target, "target")] = {
                "x": positions[node_id]["x"],
                "y": calculate_distributed_anchor_y(positions[node_id]["y"], len(edges), index),
            }

    return anchor_lookup


def calculate_distributed_anchor_y(node_y: int, anchor_count: int, anchor_index: int) -> int:
    """
    Helper for vertical anchor distribution.
    - A single anchor lands at the vertical center of the card
    - Multiple anchors use equal spacing between top and bottom padding
    - This mirrors Excel-style vertical distribution for connector points
    """
    if anchor_count <= 1:
        return node_y + FLOWCHART_NODE_HEIGHT // 2

    top_padding = 16
    bottom_padding = 16
    usable_height = FLOWCHART_NODE_HEIGHT - top_padding - bottom_padding

    # Example for 3 anchors: top, middle, bottom within the padded card area.
    return int(node_y + top_padding + (usable_height * anchor_index / (anchor_count - 1)))


def build_flowchart_node_html(node: FlowchartNode, position: dict[str, int]) -> str:
    """
    Tool for building one HTML card for one FlowchartNode.

    For each node passed in:
    - Create the outer card container
    - Add the icon text based on node kind
    - Add the node type, label, subtitle, and stage ID
    - Position the card using the x / y coordinate

    The HTML card is used for scripts and data resources.
    The CSS class controls the color and visual style.
    """
    visual_type = infer_node_visual_type(node)
    kind_class = f"kind-{node.kind} type-{visual_type}"
    icon_html = node_visual_type_to_icon_svg(visual_type)

    # The card shows only the filename, but keeps the full original label in
    # the title attribute so the user can still inspect the full path.
    display_label = format_flowchart_node_display_label(node)
    stage = node.stage_order_ID or node.stage_ID
    stage_html = f'<div class="stage-id">{escape_html(stage)}</div>' if stage else ""
    is_script_node = node.kind in {
        "python_script",
        "sql_script",
        "alteryx_workflow",
        "bat_script",
    }
    high_level_summary = node.details.get("script_high_level_summary", "")
    detailed_summary = node.details.get("script_detailed_summary", "")
    script_type = node.details.get("script_type", node.kind.replace("_", " "))
    script_node_class = " script-node" if is_script_node else ""
    script_data_attrs = ""

    if is_script_node:
        # Script summaries are embedded as data attributes.
        # JavaScript reads these when showing hover previews and click modals.
        script_data_attrs = (
            f' data-script-name="{escape_html(node.label)}"'
            f' data-script-type="{escape_html(script_type)}"'
            f' data-stage-order-id="{escape_html(stage or "")}"'
            f' data-high-level-summary="{escape_html(high_level_summary)}"'
            f' data-detailed-summary="{escape_html(detailed_summary)}"'
        )

    return f"""      <article class="node {kind_class}{script_node_class}" data-node-id="{escape_html(node.id)}" style="left: {position["x"]}px; top: {position["y"]}px;" title="{escape_html(node.label)}"{script_data_attrs}>
        <div class="icon" aria-hidden="true">{icon_html}</div>
        <div>
          <div class="node-kind">{escape_html(format_node_visual_type(visual_type))}</div>
          <div class="node-label">{escape_html(display_label)}</div>
          <div class="node-subtitle">{escape_html(node.subtitle or "")}</div>
          {stage_html}
        </div>
      </article>"""


def build_flowchart_edge_svg(edge: FlowchartEdge,
                             positions: dict[str, dict[str, int]],
                             node_lookup: dict[str, FlowchartNode],
                             anchor_lookup: dict[tuple[str, str, str], dict[str, int]]) -> str:
    """
    Tool for building one SVG arrow for one FlowchartEdge.

    For each edge passed in:
    - Find source and target node positions
    - Draw an orthogonal arrow path
    - Add a transparent hover path for easier mouse interaction
    - Add a relationship label that appears on hover

    This function controls:
    - arrow routing
    - arrow hover behavior
    - relationship label position
    - low-confidence edge styling
    """
    source = positions[edge.source]
    target = positions[edge.target]
    source_anchor = anchor_lookup.get((edge.source, edge.target, "source"))
    target_anchor = anchor_lookup.get((edge.source, edge.target, "target"))

    source_x = source_anchor["x"] if source_anchor else source["x"] + FLOWCHART_NODE_WIDTH
    source_y = source_anchor["y"] if source_anchor else source["y"] + FLOWCHART_NODE_HEIGHT // 2
    target_x = target_anchor["x"] if target_anchor else target["x"]
    target_y = target_anchor["y"] if target_anchor else target["y"] + FLOWCHART_NODE_HEIGHT // 2

    if target_x <= source_x:
        # Same-layer or backward edges need a different route to avoid card overlap.
        source_x = source["x"] + FLOWCHART_NODE_WIDTH // 2
        source_y = source["y"] + FLOWCHART_NODE_HEIGHT
        target_x = target["x"] + FLOWCHART_NODE_WIDTH // 2
        target_y = target["y"]
        mid_y = source_y + max(36, (target_y - source_y) // 2)
        path = f"M{source_x} {source_y} L{source_x} {mid_y} L{target_x} {mid_y} L{target_x} {target_y}"
        label_x = int((source_x + target_x) / 2)
        label_y = int(mid_y) - 12
    else:
        # Standard forward edge: right side of source to left side of target.
        mid_x = int((source_x + target_x) / 2)
        path = f"M{source_x} {source_y} L{mid_x} {source_y} L{mid_x} {target_y} L{target_x} {target_y}"

        # If the line turns vertically, place the label near the first segment.
        # If the line is flat, place it near the middle of the whole edge.
        if abs(source_y - target_y) > 24:
            label_x = int((source_x + mid_x) / 2)
            label_y = int(source_y) - 12
        else:
            label_x = int((source_x + target_x) / 2)
            label_y = int(source_y) - 12

    label = escape_html(format_edge_label(edge.label or edge.kind))
    edge_class = "edge-path edge-low" if edge.confidence == "low" else "edge-path"

    # edge-path is the visible line.
    # edge-hit is a wide invisible line that makes hover easier.
    # data-source / data-target let JavaScript find all edges connected to a card.
    # anchor circles mark the connection points on the source / target cards.
    return f"""        <g class="edge" data-source="{escape_html(edge.source)}" data-target="{escape_html(edge.target)}">
          <path class="{edge_class}" d="{path}"></path>
          <path class="edge-hit" d="{path}"></path>
          <circle class="anchor" cx="{source_x}" cy="{source_y}" r="3"></circle>
          <circle class="anchor" cx="{target_x}" cy="{target_y}" r="3"></circle>
          <text class="edge-tooltip-text" x="{label_x}" y="{label_y}" text-anchor="middle">{label}</text>
        </g>"""


def format_edge_label(value: str) -> str:
    """
    Helper for formatting relationship labels for display.
    - reads / writes -> Data Flow
    - code_dependency -> Code Dependency
    - execution_dependency -> Execution Dependency
    """
    label_mapping = {
        "reads": "Data Flow",
        "writes": "Data Flow",
        "read": "Data Flow",
        "write": "Data Flow",
    }

    if value in label_mapping:
        return label_mapping[value]

    return value.replace("_", " ").title()


def infer_node_visual_type(node: FlowchartNode) -> str:
    """
    Helper for selecting the visual card type.
    - Script nodes use their script kind
    - File nodes use their filename extension when available
    - Database, table, API, and unknown nodes keep their semantic kind
    """
    if node.kind == "python_script":
        return "python"
    if node.kind == "sql_script":
        return "sql"
    if node.kind == "alteryx_workflow":
        return "alteryx"
    if node.kind == "bat_script":
        return "bat"
    if node.kind in {"table", "database", "api"}:
        return node.kind

    label = node.label.lower()
    suffix = Path(label).suffix.lower()

    extension_mapping = {
        ".py": "python",
        ".sql": "sql",
        ".yxmd": "alteryx",
        ".yxwz": "alteryx",
        ".bat": "bat",
        ".cmd": "bat",
        ".md": "markdown",
        ".json": "json",
        ".csv": "csv",
        ".tsv": "csv",
        ".xlsx": "excel",
        ".xls": "excel",
        ".xlsm": "excel",
        ".html": "html",
        ".htm": "html",
        ".log": "log",
    }

    return extension_mapping.get(suffix, "file")


def format_node_visual_type(visual_type: str) -> str:
    """
    Helper for card type display text.
    - Keeps labels concise and readable
    - Avoids exposing internal enum names such as python_script
    """
    display_mapping = {
        "python": "Python",
        "sql": "SQL",
        "alteryx": "Alteryx",
        "bat": "BAT",
        "markdown": "Markdown",
        "json": "JSON",
        "csv": "CSV / TSV",
        "excel": "Excel",
        "database": "Database",
        "table": "Table",
        "html": "HTML",
        "log": "Log",
        "file": "File",
        "api": "API",
        "unknown": "Unknown",
    }
    return display_mapping.get(visual_type, visual_type.replace("_", " ").title())


def format_flowchart_node_display_label(node: FlowchartNode) -> str:
    """
    Helper for shortening labels shown on the flowchart card.
    - Keeps the original node.label untouched in the flowchart spec
    - Shows only the final filename when a label looks like a path
    - Supports both macOS/Linux paths and Windows paths
    """
    label = str(node.label).strip()

    if not label:
        return label

    if "/" in label or "\\" in label:
        return re.split(r"[\\/]", label.rstrip("/\\"))[-1] or label

    return label


def node_visual_type_to_icon_text(visual_type: str) -> str:
    """
    Helper for selecting the short text shown inside each node icon.
    - Uses compact glyph-like text for deterministic HTML output
    - CSS applies the pastel chip and accent color
    - File extensions refine generic file nodes into markdown / json / html / etc.
    """
    mapping = {
        "python": "PY",
        "sql": "SQL",
        "alteryx": "a",
        "bat": ">_",
        "markdown": "MD",
        "json": "{}",
        "csv": "CSV",
        "excel": "X",
        "database": "DB",
        "table": "TBL",
        "html": "</>",
        "log": "LOG",
        "file": "FILE",
        "folder": "DIR",
        "api": "API",
        "unknown": "?"
    }
    return mapping.get(visual_type, "?")


def node_visual_type_to_icon_svg(visual_type: str) -> str:
    """
    Helper for selecting the SVG shown inside each node icon.
    - Uses inline SVG so the final HTML stays standalone
    - Keeps each icon centered inside the pastel card chip
    - Reuses CSS currentColor so each file type controls its own accent color
    """
    icon_path = FLOWCHART_ICON_DIR / f"{visual_type}.svg"

    if not icon_path.exists():
        icon_path = FLOWCHART_ICON_DIR / "file.svg"

    return icon_path.read_text(encoding = "utf-8")


def escape_html(value: object) -> str:
    """
    Helper for safely inserting values into generated HTML.
    - Converts the input value to string
    - Escapes HTML-sensitive characters
    - Prevents script names / paths from breaking the HTML output
    """
    return html.escape(str(value), quote = True)
