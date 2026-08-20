#%%
import os
import sys

script_directory = os.path.dirname(os.path.abspath(__file__))
os.chdir(script_directory)
sys.path.insert(0, script_directory)

from langchain_core.prompts import ChatPromptTemplate

from typing import Literal
from pathlib import Path
from tools import *


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
    LLM = set_up_LLM(model = model)
    
    workflow_prompt = ChatPromptTemplate.from_template(Path("./prompts/prompt_construct_dependency_network.md").read_text(encoding = "utf-8"))
    workflow_chain = workflow_prompt | LLM.with_structured_output(schema = WorkflowDependencyGraph)
    workflow_graph = workflow_chain.invoke({"script_dependency_profiles": profiles})
    
    return workflow_graph


def generate_HTML_flow_chart(workflow_graph: WorkflowDependencyGraph, 
                             model: Literal["OpenAI", "Ollama"] = "OpenAI"):
    LLM = set_up_LLM(model = model)
    build_flow_chart_prompt = ChatPromptTemplate.from_template()

    flow_chart_chain = build_flow_chart_prompt | LLM


def summarize_script_file(file_path)


#%%
"""
Tools for writing script summary to XLSX workbook
"""

import openpyxl

def write_summary_to_excel(workbook_path: str, 
                           summaries: list[ScriptSummary], 
                           target_sheet: str = "Sheet1"):
    workbook: openpyxl.Workbook = openpyxl.workbook(workbook_path)
    worksheet = 
    for summary in summaries:
        