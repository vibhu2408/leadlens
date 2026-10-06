"""LeadLens API + static UI (one process, one container)."""
from __future__ import annotations

import os
import uuid
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, export, outreach, pipeline, scoring
from .ingest import parse_csv

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
SAMPLE = ROOT / "data" / "sample_leads.csv"
MAX_UPLOAD = 10 * 1024 * 1024
MAX_ROWS = int(os.environ.get("LEADLENS_MAX_ROWS", "5000"))

app = FastAPI(title="LeadLens", version="1.0.0")
db.init()


@app.get("/api/health")
def health():
    return {"ok": True, "ai": bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))}


@app.get("/api/presets")
def presets():
    return scoring.PRESETS


def _start(content: bytes, name: str, preset: str, enrich: bool, bg: BackgroundTasks) -> dict:
    if preset not in scoring.PRESETS:
        raise HTTPException(400, f"unknown preset '{preset}'")
    rows, mapping = parse_csv(content)
    if not rows:
        raise HTTPException(400, "No usable rows found. The CSV needs at least a company, website or email column.")
    if len(rows) > MAX_ROWS:
        raise HTTPException(413, f"{len(rows)} rows; this instance accepts up to {MAX_ROWS} per batch.")
    batch_id = uuid.uuid4().hex[:12]
    db.create_batch(batch_id, name, preset, scoring.default_icp(preset), enrich)
    bg.add_task(pipeline.process_batch, batch_id, rows, enrich)
    return {"id": batch_id, "rows": len(rows), "mapped_columns": mapping}


@app.post("/api/batches")
async def create_batch(
    bg: BackgroundTasks,
    file: UploadFile = File(...),
    preset: str = Form("acquisition"),
    enrich: bool = Form(True),
):
    content = await file.read(MAX_UPLOAD + 1)
    if len(content) > MAX_UPLOAD:
        raise HTTPException(413, "File larger than 10 MB")
    return _start(content, file.filename or "upload.csv", preset, enrich, bg)


@app.post("/api/batches/sample")
def create_sample(bg: BackgroundTasks, preset: str = Form("acquisition"), enrich: bool = Form(False)):
    return _start(SAMPLE.read_bytes(), "Sample: home services & industrial", preset, enrich, bg)


@app.get("/api/batches")
def list_batches():
    return db.list_batches()


@app.get("/api/batches/{batch_id}")
def get_batch(batch_id: str):
    b = db.get_batch(batch_id)
    if not b:
        raise HTTPException(404, "batch not found")
    return b


@app.delete("/api/batches/{batch_id}")
def delete_batch(batch_id: str):
    db.delete_batch(batch_id)
    return {"ok": True}


@app.get("/api/batches/{batch_id}/leads")
def get_leads(batch_id: str):
    return db.get_leads(batch_id)


class ICP(BaseModel):
    industries: list[str]
    exclude_keywords: list[str] = []
    employees: tuple[float, float]
    revenue: tuple[float, float]
    min_age_years: int = 0
    titles: list[str]
    weights: dict[str, int]


@app.put("/api/batches/{batch_id}/icp")
def update_icp(batch_id: str, icp: ICP):
    b = get_batch(batch_id)
    new_icp = {**b["icp"], **icp.model_dump()}
    tiers = pipeline.rescore(batch_id, new_icp)
    stats = {**b["stats"], "tiers": tiers}
    db.update_batch(batch_id, icp=new_icp, stats=stats)
    return {"tiers": tiers}


class LeadPatch(BaseModel):
    status: str


@app.patch("/api/leads/{lead_id}")
def patch_lead(lead_id: int, body: LeadPatch):
    if body.status not in export.HUBSPOT_STATUS:
        raise HTTPException(400, "status must be one of " + ", ".join(export.HUBSPOT_STATUS))
    db.update_lead(lead_id, status=body.status)
    return {"ok": True}


class OpenerReq(BaseModel):
    sender: str = "[Your name]"
    offering: str = ""


@app.post("/api/leads/{lead_id}/opener")
def make_opener(lead_id: int, body: OpenerReq):
    lead = db.get_lead(lead_id)
    if not lead:
        raise HTTPException(404, "lead not found")
    preset = db.get_batch(lead["batch_id"])["preset"]
    text, source = outreach.generate_opener(lead, preset, body.sender, body.offering)
    db.update_lead(lead_id, opener=text)
    return {"text": text, "source": source}


@app.get("/api/batches/{batch_id}/export")
def export_batch(batch_id: str, format: str = "hubspot", tiers: str = "A,B,C,D", exclude_invalid: bool = True):
    b = get_batch(batch_id)
    wanted = set(tiers.upper().split(","))
    leads = [l for l in db.get_leads(batch_id)
             if l["tier"] in wanted and not (exclude_invalid and l["email_status"] == "invalid")
             and l["status"] != "disqualified"]
    body = export.to_hubspot(leads) if format == "hubspot" else export.to_full_csv(leads)
    fname = f"leadlens_{b['id']}_{format}.csv"
    return Response(body, media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
