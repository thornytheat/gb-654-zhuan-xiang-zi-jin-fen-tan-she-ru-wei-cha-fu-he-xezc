from fastapi import APIRouter, Body, Request
from fastapi.responses import RedirectResponse

from . import service

router = APIRouter(tags=["M7-合并映射"])


@router.get("/mergers")
def page():
    # 登记入口在预算采信页，短链跳转锚点
    return RedirectResponse("/budgets#mergers")


@router.get("/api/mergers/mappings")
def list_mappings(request: Request):
    return service.list_mappings(request.app.state.merge_store)


@router.post("/api/mergers/mappings/register")
def register(request: Request, payload: dict = Body(default={})):
    return service.register_mapping(request.app.state.merge_store,
                                    payload or {})


@router.post("/api/mergers/mappings/recover")
def recover(request: Request):
    return service.recover(request.app.state.merge_store)


@router.post("/api/mergers/budgets/import")
def import_budget(request: Request, payload: dict = Body(default={})):
    return service.import_budget(request.app.state.merge_store, payload or {})


@router.post("/api/mergers/weights/calculate")
def weights(request: Request, payload: dict = Body(default={})):
    return service.calculate_weights(request.app.state.merge_store,
                                     payload or {})


@router.get("/api/mergers/trace")
def trace(request: Request):
    return service.trace(request.app.state.merge_store)


@router.get("/api/mergers/quarantine")
def quarantine(request: Request):
    return {"records": request.app.state.merge_store.all_quarantine()}
