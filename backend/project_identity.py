"""
Choose the display name for a newly analyzed project.
- An explicit, nonblank user name takes precedence after outer whitespace is removed.
- Otherwise use the selected source folder's name, not a generic application title.
- Understand Windows drive/UNC paths even when formatting a path on another OS.
- Existing GraphDocument titles are not rewritten; this helper is for new analysis.
"""

from pathlib import Path, PurePath, PureWindowsPath
import re
import unicodedata
from urllib.parse import quote


def project_title(script_folder: str | PurePath, requested_title: str | None = None) -> str:
    """
    Return a readable project name without opening or executing a source file.
    - Native Path objects preserve legitimate special characters in folder names.
    - Windows strings use Windows path rules instead of the current host's rules.
    - A share root uses its share name; a drive root uses a readable drive label.
    - A filesystem root with no name falls back to a neutral workflow title.
    """
    if requested_title is not None and requested_title.strip():
        return requested_title.strip()
    if isinstance(script_folder, PurePath):
        folder = script_folder
    else:
        candidate = PureWindowsPath(script_folder)
        folder = (candidate if candidate.drive or ("\\" in script_folder and "/" not in script_folder)
                  else Path(script_folder).expanduser().absolute())
    if folder.name in {".", ".."} and isinstance(folder, Path):
        folder = folder.resolve()
    if folder.name:
        return folder.name
    if isinstance(folder, PureWindowsPath) and folder.drive:
        drive = folder.drive
        if drive.startswith("\\\\"):
            return drive.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        return drive.rstrip(":") + " drive"
    return "Workflow Flowchart"


def flowchart_filename(title: str) -> str:
    """
    Suggest a project-named HTML download without changing artifact identity.
    - Remove Windows-reserved punctuation, control characters and trailing dots.
    - Avoid reserved device names such as CON and LPT1, including before a dot.
    - Bound the UTF-8 length so Unicode names fit ordinary filesystem limits.
    - Keep canonical URLs and manifest filenames independent of this display name.
    """
    stem = re.sub(r'[\x00-\x1f\x7f<>:"/\\|?*]', " ", title)
    stem = " ".join(stem.split()).strip(" .")
    if stem.lower().endswith(".html"):
        stem = stem[:-5].rstrip(" .")
    stem = stem.encode("utf-8")[:180].decode("utf-8", errors="ignore").rstrip(" .") or "Workflow Flowchart"
    device = stem.split(".", 1)[0].rstrip().upper()
    reserved = {"CON", "PRN", "AUX", "NUL", "CLOCK$"}
    reserved.update(prefix + suffix for prefix in ("COM", "LPT") for suffix in "123456789\u00b9\u00b2\u00b3")
    if device in reserved:
        stem = "Workflow " + stem
    return stem + ".html"


def flowchart_attachment(title: str) -> str:
    """
    Build an ASCII-safe HTTP attachment header with a UTF-8 filename when needed.
    - Supply a usable ASCII fallback for older download clients.
    - Percent-encode the Unicode filename using the standard filename* form.
    - User labels cannot insert new headers, quote delimiters or path separators.
    """
    filename = flowchart_filename(title)
    fallback_stem = unicodedata.normalize("NFKD", filename[:-5]).encode("ascii", errors="ignore").decode("ascii")
    fallback = flowchart_filename(fallback_stem)
    header = f'attachment; filename="{fallback}"'
    if filename != fallback:
        header += "; filename*=UTF-8''" + quote(filename, safe="")
    return header
