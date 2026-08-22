#%%
import os
import sys


script_directory = os.path.dirname(os.path.abspath(__file__))
os.chdir(script_directory)
sys.path.insert(0, script_directory)

from langchain_openai import ChatOpenAI
from langchain_ollama import ChatOllama
from langchain_core.prompts import ChatPromptTemplate
from langchain_community.document_loaders import TextLoader
from langchain_core.documents import Document

from pathlib import Path
import json
from importlib import reload
import tools
reload(tools)

from tools import *
from classes import ScriptDependencyProfile, WorkflowDependencyGraph, ScriptSummary
# from documentation import *

#%%
"""
Confirm directory validity
"""
script_path = input("Please Enter The Program's Directory")
print(f"script path: {script_path}")

DA_Document_path = "to be supplied later"

valid_file_list = list_all_scripts(script_path = script_path)


#%%
"""
Retrieve information on 
    - script processing logic
    - dependencies
    - order of execution
"""
async def main():
    profiles = await extract_dependencies_for_folder(valid_file_list = valid_file_list, chains = create_dependency_chains(model = "Ollama")) # generate dependency profile for each and every script
    workflow_network = construct_dependency_network(profiles, model = "Ollama") # construct processing workflow network for the entire program
    summaries = await generate_summary_for_folder(valid_file_list, profiles, workflow_network, "Ollama")

    return profiles, workflow_network, summaries


profiles, workflow_network, summaries = asyncio.run(main())

output_dir = Path("outputs")
output_dir.mkdir(exist_ok = True)

(output_dir / "profiles.json").write_text(
    json.dumps([profile.model_dump() for profile in profiles], indent = 2, ensure_ascii = False),
    encoding = "utf-8"
)

(output_dir / "workflow_network.json").write_text(
    workflow_network.model_dump_json(indent = 2),
    encoding = "utf-8"
)

(output_dir / "summaries.json").write_text(
    json.dumps([summary.model_dump() for summary in summaries], indent = 2, ensure_ascii = False),
    encoding = "utf-8"
)

print(f"Saved output JSON files to {output_dir.resolve()}")


#%%
"""
Reload exported JSON outputs and test flowchart construction
"""

import json

output_dir = Path("outputs")
output_dir.mkdir(exist_ok = True)

profiles_from_json = [
    ScriptDependencyProfile.model_validate(profile)
    for profile in json.loads((output_dir / "profiles.json").read_text(encoding = "utf-8"))
]

workflow_network_from_json = WorkflowDependencyGraph.model_validate_json(
    (output_dir / "workflow_network.json").read_text(encoding = "utf-8")
)

summaries_from_json = [
    ScriptSummary.model_validate(summary)
    for summary in json.loads((output_dir / "summaries.json").read_text(encoding = "utf-8"))
]

flowchart_spec = construct_flowchart_spec(
    workflow_graph = workflow_network_from_json,
    profiles = profiles_from_json,
    summaries = summaries_from_json
)

(output_dir / "flowchart_spec.json").write_text(
    flowchart_spec.model_dump_json(indent = 2),
    encoding = "utf-8"
)

render_flowchart_html(
    flowchart_spec = flowchart_spec,
    output_path = output_dir / "workflow_flowchart.html"
)

print(f"Saved flowchart spec to {(output_dir / 'flowchart_spec.json').resolve()}")
print(f"Saved workflow flowchart to {(output_dir / 'workflow_flowchart.html').resolve()}")
print(f"Flowchart nodes: {len(flowchart_spec.nodes)}")
print(f"Flowchart edges: {len(flowchart_spec.edges)}")


#%%
"""
Compile DA Document based on retrieved information
"""
