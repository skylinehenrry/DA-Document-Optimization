#!/bin/zsh

# Open DA Document Generator on macOS.
# - Finds the project even when this file is opened from another folder.
# - Uses the project's virtual environment when one has been created.
# - Delegates readiness checks and detached server startup to launch.py.
# - Closing this window does not stop the backend or discard a running job.
# - Keeps startup errors visible instead of automatically closing the window.

DA_PROJECT_DIR="${0:A:h}"
if [[ -x "$DA_PROJECT_DIR/.venv/bin/python" ]]; then
    DA_PYTHON="$DA_PROJECT_DIR/.venv/bin/python"
else
    DA_PYTHON="python3"
fi

"$DA_PYTHON" "$DA_PROJECT_DIR/launch.py" "$@"
DA_EXIT_CODE=$?
if [[ "$DA_EXIT_CODE" -ne 0 && -t 0 ]]; then
    echo ""
    read "DA_DISMISS?Press Return to close this window after reading the error. "
fi
exit "$DA_EXIT_CODE"
