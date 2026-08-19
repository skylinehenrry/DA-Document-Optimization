#%%
import os
import openpyxl
from dataclasses import dataclass

#%%
@dataclass
class DA_Document():
    file_path: str
    