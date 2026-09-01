"""Package boundary for the DA Document Generator backend.

- Importing this package does not inspect a project, start a provider, or open a
  Microsoft sign-in window.
- Application startup belongs to ``backend.app`` and command-line startup belongs
  to ``backend.main``; keeping those entry points explicit makes tests predictable.
- Shared graph, storage, extraction, editing, and rendering modules can therefore
  be imported independently without triggering work on the user's machine.
"""
