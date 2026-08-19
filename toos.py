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
from dataclasses_module import ScriptDependencyProfile, DependencyRef, ResourceRef

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


def create_dependency_chains(LLM: ChatOpenAI | ChatOllama):
    """
    Use LLM to extract the following information
        1. The list of imported modules (only include custom modules, and exclude built-in modules and packages installed from pip)
        2. The list of input data (e.g. csv files, tsv files, excel files, or database connection)
        3. The list of output data (e.g. csv files, tsv files, excel files or tables)
    The output will be a DependencyExtraction object, defined in dataclasses_module
    """

    import_prompt = ChatPromptTemplate.from_template(Path("./prompts/prompt_detect_import.md").read_text(encoding = "utf-8"))
    input_data_prompt = ChatPromptTemplate.from_template(Path("./prompts/prompt_detect_input_data.md").read_text(encoding = "utf-8"))
    output_data_prompt = ChatPromptTemplate.from_template(Path("./prompts/prompt_detect_output_data.md").read_text(encoding = "utf-8"))

    structured_LLM = LLM.with_structured_output(DependencyRef)

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
    document = TextLoader(file_path = file, encoding = "utf-8", autodetect_encoding = True).load()[0]
    payload = {"script_content": document.page_content}
    async with semaphore:
        import_result, input_data_result, output_data_result = await asyncio.gather(chains["imports"].invoke(payload),
                                                                                     chains["input data"].invoke(payload),
                                                                                     chains["output data"].invoke(payload))

    profile = ScriptDependencyProfile(file_name = file.name,
                                      file_path = str(file), 
                                      script_type = detect_script_type(file))

    profile.dependencies.extend(import_result.depndencies)
    profile.dependencies.extend(input_data_result.depndencies)
    profile.dependencies.extend(output_data_result.depndencies)

    profile.unclear_items.extend(import_result.unclear_items)
    profile.unclear_items.extend(input_data_result.unclear_items)
    profile.unclear_items.extend(output_data_result.unclear_items)

    return profile


async def extract_dependencies_for_folder(valid_file_list: list[Path],
                                          chains: dict,
                                          max_concurrent_files: int = 5):
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
This will be used to generate the flow chart used in DA Document
"""

import json
from dataclasses_module import WorkflowNode, WorkflowEdge, WorkflowDependencyGraph

def profiles_to_json(profiles: list[ScriptDependencyProfile]) -> str:
    return json.dumps([profile.model_dump() for profile in profiles], indent = 2, ensure_ascii = False)


def construct_dependency_network(profiles: list[ScriptDependencyProfile], 
                                 model: Literal["OpenAI", "Ollama"] = "OpenAI") -> WorkflowDependencyGraph:
    if model == "OpenAI":
        LLM = ChatOpenAI(temperature = 0) # model to be supplied later
    elif model == "Ollama":
        LLM = ChatOllama(temperature = 0)
    
    workflow_prompt = ChatPromptTemplate.from_template(Path("./prompts/prompt_construct_dependency_network.md").read_text(encoding = "utf-8"))
    workflow_chain = workflow_prompt | LLM.with_structured_output(schema = WorkflowDependencyGraph)
    workflow_graph = workflow_chain.invoke({"script_dependency_profiles": profiles})
    
    return workflow_graph