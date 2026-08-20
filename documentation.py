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
        