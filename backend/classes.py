#%%
import os
import sys

script_directory = os.path.dirname(os.path.abspath(__file__))
os.chdir(script_directory)
sys.path.insert(0, script_directory)

from pydantic import BaseModel, Field
from langchain_ollama import ChatOllama, OllamaEmbeddings
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from typing import Literal, Any
from pathlib import Path


#%%
"""
Data class for first level of reasoning, where the LLM is provided with the script's content and extracts the following information
    - custom package imported
    - input data used
    - output data generated
"""

class ResourceRef(BaseModel):
    type: Literal["file", "table", "script", "module", "database", "api", "unknown"]
    name: str
    path: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class DependencyRef(BaseModel):
    relationship: Literal["reads", "writes", "calls", "imports", "depends_on"]
    target: ResourceRef
    evidence: str | None = None
    confidence: Literal["high", "medium", "low"] = "high"
    details: dict[str, Any] = Field(default_factory=dict)


class DependencyExtraction(BaseModel):
    dependencies: list[DependencyRef] = Field(default_factory=list)
    unclear_items: list[str] = Field(default_factory=list)


class ScriptDependencyProfile(BaseModel):
    file_name: str
    file_path: str
    script_type: str
    dependencies: list[DependencyRef] = Field(default_factory=list)
    unclear_items: list[str] = Field(default_factory=list)


#%%
"""
Data class for second level of reasoning, where the LLM is provided with the script's content, import moduels, input data, output data, and will construct the following
    - workflow node
    - workflow edge
    - workflow dependency graph
"""

class WorkflowNode(BaseModel):
    id: str
    label: str
    script_name: str
    script_path: str
    script_type: str
    role: str | None = None
    stage_ID: str
    order_ID: str
    stage_order_ID: str
    order_confidence: Literal["high", "medium", "low"]
    order_evidence: str | None = None


class WorkflowEdge(BaseModel):
    source: str
    target: str
    relationship: Literal["data_dependency",
                          "code_dependency",
                          "execution_dependency",
                          "inferred_order",
                          "unknown"]
    shared_resource: str | None = None
    evidence: str | None = None
    confidence: Literal["high", "medium", "low"]


class WorkflowDependencyGraph(BaseModel):
    workflow_summary: str
    nodes: list[WorkflowNode] = Field(default_factory=list)
    edges: list[WorkflowEdge] = Field(default_factory=list)
    unresolved_items: list[str] = Field(default_factory=list)
    mermaid_flowchart: str | None = None


#%%
"""
Define output scheme on Script Summary produced by generative AI
The output should be a ScriptSummary object with the following attributes & methods

ScriptSummary
    script_type: str[Python, SQL, Alteryx] # The input script should either be a python script, an SQL script, an Alteryx workflow or a BAT file
    script_name: str # name of the script (e.g. main.py)
    script_location: str # string representation of file location (e.g. O:<path/to/your/script/>)
    script_high_level_summary: str # a high-level summary of the script for an overall view
    script_detailed_summary: str # more detailed description about the processing handled by the script
    script_input_data: list[str] # a list of input data used for processing
    script_output_data: list[str] # a list of output data generated from processing
    script_role: str[B]: str # correspond to the part of processing the script is responsible for
    script_order_ID: str # zero padded string representation of the order in which the script shoudl be executed (i.e. if it's the first script - the script that ingests the raw data, then it should be 01)
"""

class ScriptSummary(BaseModel):
    script_type: Literal["python", "sql", "alteryx", "bat"]
    script_name: str
    script_location: str
    script_stage_order_ID: str
    script_high_level_summary: str
    script_detailed_summary: str
    script_input_data: list[str] = Field(default_factory = list)
    script_output_data: list[str] = Field(default_factory = list)


#%%
"""
Class for constructing flow chart
Integrades both script nodes and data nodes to produce FlowchartSpec which constructs the entire workflow flowchart
"""
FlowchartNodeKind = Literal[
    "file",
    "table",
    "database",
    "api",
    "python_script",
    "sql_script",
    "alteryx_workflow",
    "bat_script",
    "unknown",
]


FlowchartEdgeKind = Literal[
    "reads",
    "writes",
    "data_dependency",
    "code_dependency",
    "execution_dependency",
    "inferred_order",
    "unknown",
]


class FlowchartNode(BaseModel):
    id: str
    label: str
    kind: FlowchartNodeKind
    stage_ID: str | None = None
    stage_order_ID: str | None = None
    subtitle: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class FlowchartEdge(BaseModel):
    source: str
    target: str
    kind: FlowchartEdgeKind
    label: str | None = None
    confidence: Literal["high", "medium", "low"] = "medium"
    evidence: str | None = None


class FlowchartSpec(BaseModel):
    title: str = "Workflow Flowchart"
    summary: str | None = None
    nodes: list[FlowchartNode] = Field(default_factory=list)
    edges: list[FlowchartEdge] = Field(default_factory=list)