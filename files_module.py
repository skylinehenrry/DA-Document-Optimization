#%%
import ast
import tokenize
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET
import sqlglot
import sqlglot.expressions


#%%
@dataclass
class FileInfo:
    """
    Store basic information shared by all supported file types.
    """
    path: str | Path
    name: str


@dataclass
class PythonFileInfo(FileInfo):
    """
    Store metadata and the parsed abstract syntax tree for a Python file.
    """
    ast: ast.AST


@dataclass
class SQLFileInfo(FileInfo):
    """
    Store metadata and parsed SQL expressions for a SQL file.
    """
    parsed_sql: list[sqlglot.expressions.Expression]


@dataclass
class AlteryxFileInfo(FileInfo):
    """
    Store metadata and the parsed XML tree for an Alteryx workflow file.
    """
    xml_tree: ET.ElementTree


@dataclass
class BatFileInfo(FileInfo):
    """
    Store metadata for a Windows batch file.
    """
    pass


#%%
def check_script_folder_exists(script_folder_path: str | Path) -> bool:
    """
    Validate that the supplied script folder path exists and is a directory.
    """
    if not isinstance(script_folder_path, (str, Path)):
        raise TypeError("script_folder_path must be a string or Path object")

    script_folder_path = Path(script_folder_path)

    if not script_folder_path.exists():
        raise FileNotFoundError(f"The script folder path {script_folder_path} does not exist")

    if not script_folder_path.is_dir():
        raise NotADirectoryError(f"The script folder path {script_folder_path} is not a directory")

    return True


def _detect_python_encoding(file: Path) -> str:
    """
    Return the declared encoding for a Python source file.
    """
    with file.open("rb") as f:
        encoding, _ = tokenize.detect_encoding(f.readline)
    return encoding


def _read_text_with_fallback(file: Path, encoding: str = 'utf-8'):
    try:
        return file.read_text(encoding = encoding)
    except UnicodeDecodeError:
        return file.read_text(encoding = 'cp932')


def get_file_info_from_folder(script_folder_path: str | Path) -> list[FileInfo]:
    """
    Collect parsed information for supported files under a script folder.
    Supported file types are Python scripts, SQL files, Alteryx workflow files, and Windows batch files. 
    Any other file type is ignored.
    """

    check_script_folder_exists(script_folder_path)
    print(f"Script folder received: {script_folder_path}")

    script_folder_path = Path(script_folder_path)
    python_suffix = ".py"
    sql_suffix = ".sql"
    alteryx_suffixes = {".yxmd", ".yxmc", ".yxwz"}
    bat_suffix = ".bat"

    files_info: list[FileInfo] = []

    for file in script_folder_path.rglob("*"):
        if file.is_file:
            if file.suffix == python_suffix:
                files_info.append(PythonFileInfo(path = file, name = file.name, ast = ast.parse(file.read_text(encoding = _detect_python_encoding(file)))))
            elif file.suffix == sql_suffix:
                files_info.append(SQLFileInfo(path = file, name = file.name, parsed_sql = sqlglot.parse(_read_text_with_fallback(file), dialect = 'tsql', error_level = sqlglot.ErrorLevel.WARN)))
            elif file.suffix == alteryx_suffixes:
                files_info.append(AlteryxFileInfo(path = file, name = file.name, xml_tree = ET.parse(file)))
            elif file.suffix == bat_suffix:
                pass # To be defined later
            else:
                pass # Ignore other file types

    return files_info
