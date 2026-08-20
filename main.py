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
from tools import *
from documentation import *

#%%
"""
Confirm directory validity
"""
script_path = "to be supplied later"
DA_Document_path = "to be supplied later"

valid_file_list = list_all_scripts(script_path = script_path)


#%%
"""
Retrieve information on 
    - script processing logic
    - dependencies
    - order of execution
"""
profiles = extract_dependencies_for_folder(valid_file_list) # generate dependency profile for each and every script
workflow_network = construct_dependency_network(profiles, model = "OpenAI") # construct processing workflow network for the entire program
summaries = generate_summary_for_folder(valid_file_list, profiles, workflow_network, "OpenAI")


#%%
"""
Compile DA Document based on retrieved information
"""
