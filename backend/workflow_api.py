"""
Expose saved drafts, reviewed revisions and generation-specific downloads.
- The browser uses /api/jobs for long operations; direct mutation routes remain
  compatible with existing local API clients and use the same service validation.
- Read/export routes always identify a draft, and may pin a particular revision.
- Generated files are looked up in the database and checked before being served.
- Download links are explicit attachments, while preview links open inline.
"""

from contextlib import asynccontextmanager
import asyncio

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from .drawio import export_drawio
from .graph_edits import EditRequest
from .graph_models import GraphDocument
from .graph_rendering import render_graph_svg
from .workflow_service import AnalysisRequest, GenerateRequest, ImportRequest, SuggestRequest, complete_in_thread, review_report


router = APIRouter(prefix="/api/drafts", tags=["Reviewed flowcharts"])


@asynccontextmanager
async def activity(request: Request):
    manager = getattr(request.app.state, "workflow_jobs", None)
    if request.app.state.stopping or (manager is not None and not manager.accepting):
        raise HTTPException(status_code=503, detail="The backend is stopping. Restart it before continuing.")
    request.app.state.active_workflow_jobs += 1
    try:
        yield request.app.state.workflow_service
    finally:
        request.app.state.active_workflow_jobs -= 1


def logger(request: Request):
    return getattr(request.app.state, "workflow_logger", None)


@router.get("")
async def list_drafts(request: Request, output_folder: str | None = None, limit: int = Query(default=100, ge=1, le=1000)):
    return await asyncio.to_thread(request.app.state.workflow_service.store.list_drafts, output_folder, limit)


@router.get("/schema")
async def graph_schema():
    return GraphDocument.model_json_schema()


@router.post("", status_code=201)
async def analyze(body: AnalysisRequest, request: Request):
    async with activity(request) as service:
        graph = await service.analyze(body, logger(request))
        return await asyncio.to_thread(service.describe, graph)


@router.get("/{draft_id}")
async def get_draft(draft_id: str, request: Request, revision: int | None = Query(default=None, ge=1)):
    service = request.app.state.workflow_service
    graph = await asyncio.to_thread(service.store.load, draft_id, revision)
    return await asyncio.to_thread(service.describe, graph)


@router.get("/{draft_id}/history")
async def history(draft_id: str, request: Request):
    return await asyncio.to_thread(request.app.state.workflow_service.store.history, draft_id)


@router.get("/{draft_id}/review")
async def review(draft_id: str, request: Request, revision: int | None = Query(default=None, ge=1)):
    graph = await asyncio.to_thread(request.app.state.workflow_service.store.load, draft_id, revision)
    return await asyncio.to_thread(review_report, graph)


@router.patch("/{draft_id}")
async def edit(draft_id: str, body: EditRequest, request: Request):
    async with activity(request) as service:
        graph = await complete_in_thread(service.edit, draft_id, body)
        return await asyncio.to_thread(service.describe, graph)


@router.post("/{draft_id}/import")
async def import_diagram(draft_id: str, body: ImportRequest, request: Request):
    async with activity(request) as service:
        graph = await complete_in_thread(service.import_diagram, draft_id, body)
        return await asyncio.to_thread(service.describe, graph)


@router.post("/{draft_id}/suggest")
async def suggest(draft_id: str, body: SuggestRequest, request: Request):
    async with activity(request) as service:
        graph = await service.suggest(draft_id, body, logger(request))
        return await asyncio.to_thread(service.describe, graph)


@router.post("/{draft_id}/generate")
async def generate(draft_id: str, body: GenerateRequest, request: Request):
    async with activity(request) as service:
        manifest = await service.generate(draft_id, body, logger(request))
        graph = await asyncio.to_thread(service.store.load, draft_id, body.expected_revision)
        result = await asyncio.to_thread(service.describe, graph, generation_id=manifest["generation_id"])
        result["generation"] = manifest
        return result


@router.get("/{draft_id}/export/{file_name}")
async def export(draft_id: str, file_name: str, request: Request, revision: int | None = Query(default=None, ge=1)):
    graph = await asyncio.to_thread(request.app.state.workflow_service.store.load, draft_id, revision)
    if file_name == "draft.drawio":
        content, media_type = await asyncio.to_thread(export_drawio, graph), "application/xml"
    elif file_name == "draft.svg":
        content, media_type = await asyncio.to_thread(render_graph_svg, graph), "image/svg+xml"
    elif file_name == "draft.json":
        content, media_type = graph.model_dump_json(indent=2), "application/json"
    else:
        raise FileNotFoundError("Unknown draft export; choose draft.drawio, draft.svg or draft.json.")
    headers = {"Cache-Control": "no-store"}
    if file_name == "draft.drawio":
        headers["Content-Disposition"] = f'attachment; filename="{graph.id}-r{graph.revision}.drawio"'
    return Response(content=content, media_type=media_type, headers=headers)


@router.get("/{draft_id}/generations/{generation_id}/{file_name}")
async def generated_artifact(draft_id: str, generation_id: str, file_name: str, request: Request,
                             download: bool = Query(default=False)):
    content = await asyncio.to_thread(request.app.state.workflow_service.artifact_bytes, draft_id, generation_id, file_name)
    disposition = "attachment" if download else "inline"
    header = f'{disposition}; filename="{file_name}"'
    if download and file_name == "workflow_flowchart.html":
        header = await asyncio.to_thread(request.app.state.workflow_service.flowchart_download_header, draft_id, generation_id)
    return Response(content, media_type="text/html" if file_name.endswith(".html") else "application/json",
                    headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                             "Content-Disposition": header})
