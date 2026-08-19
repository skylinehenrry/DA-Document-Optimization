#%%
import os
import sys

cwd = os.path.dirname(os.path.abspath(__file__))
os.chdir(cwd)
sys.path.insert(cwd)

#%%
"""
Part 1: read in all scripts based on the given directory
"""
import files_module
