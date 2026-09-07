from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from pathlib import Path
import fitz, io, re
from openpyxl import Workbook

app = FastAPI(title="ELEXORA 2 API", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

MASTER = [
    {"code":"CT2C-1A", "category":"CURRENT TRANSFORMER", "description":"CURRENT TRANSFORMER EPOXY CAST RESIN (WOUND TYPE)"},
    {"code":"PT-11KV", "category":"POTENTIAL TRANSFORMER", "description":"POTENTIAL TRANSFORMER (DRAWOUT TYPE)"},
    {"code":"7SJ6611", "category":"PROTECTION RELAY", "description":"NUMERICAL PROTECTION RELAY"},
    {"code":"EM6400NG", "category":"MULTIFUNCTION METER", "description":"DIGITAL MF METER"},
    {"code":"AMMETER", "category":"DIGITAL AMMETER", "description":"DIGITAL AMMETER WITH BUILT IN SELECTOR SWITCH"},
    {"code":"VOLTMETER", "category":"DIGITAL VOLTMETER", "description":"DIGITAL VOLTMETER WITH BUILT IN SELECTOR SWITCH"},
    {"code":"MCB", "category":"MCB", "description":"MINIATURE CIRCUIT BREAKER"},
]

class BomRow(BaseModel):
    item_no: str
    designation: str
    equipment_no: str
    description: str
    specification: str
    qty: int
    master_code: str
    status: str


def pdf_text(data: bytes) -> str:
    doc = fitz.open(stream=data, filetype="pdf")
    return "\n".join(page.get_text("text") for page in doc)


def extract_text(data: bytes, filename: str) -> str:
    if filename.lower().endswith(".pdf"):
        return pdf_text(data)
    # Image OCR is deliberately isolated for the next iteration; this keeps the API contract ready.
    return ""


def first(pattern, text, default="REVIEW"):
    m = re.search(pattern, text, re.I | re.M)
    return m.group(1).strip() if m else default


def make_bom(msld: str, dis: str, client: str, sales_ref: str, wo: str, voltage: str):
    text = f"{msld}\n{dis}"
    rows = []
    equipment_patterns = [
        (r"CURRENT TRANSFORMER.*?(?=\n[A-Z][A-Z ]{4,}|$)", "CURRENT TRANSFORMER", "CT"),
        (r"POTENTIAL TRANSFORMER.*?(?=\n[A-Z][A-Z ]{4,}|$)", "POTENTIAL TRANSFORMER", "PT"),
        (r"NUM(?:ERICAL)?\.?\s*PROT\.?\s*RELAY.*?(?=\n[A-Z][A-Z ]{4,}|$)", "PROTECTION RELAY", "K55"),
        (r"DIGITAL MF METER.*?(?=\n[A-Z][A-Z ]{4,}|$)", "DIGITAL MF METER", "P20"),
        (r"DIGITAL AMMETER.*?(?=\n[A-Z][A-Z ]{4,}|$)", "DIGITAL AMMETER", "P1"),
        (r"DIGITAL VOLTMETER.*?(?=\n[A-Z][A-Z ]{4,}|$)", "DIGITAL VOLTMETER", "P4"),
        (r"MCB FOR [^\n]+.*", "MCB", "MCB"),
    ]
    for idx, (pattern, category, fallback_designation) in enumerate(equipment_patterns, 1):
        matches = re.findall(pattern, text, re.I | re.S)
        for raw in matches:
            clean = " ".join(raw.split())[:1000]
            master = next((m for m in MASTER if m["category"] == category or m["code"].lower() in clean.lower()), None)
            rows.append(BomRow(item_no=f"P{idx:03d}", designation=master["code"] if master else fallback_designation,
                equipment_no=first(r"(?:DESIG(?:NATION)?|EQUIPMENT)\.?\s*[:=]?\s*([A-Z0-9_-]+)", clean, fallback_designation),
                description=master["description"] if master else category,
                specification=clean, qty=1, master_code=master["code"] if master else "REVIEW", status="MATCHED" if master else "REVIEW"))
    if not rows:
        rows.append(BomRow(item_no="P001", designation="REVIEW", equipment_no="REVIEW", description="No supported equipment confidently extracted", specification="Review source MSLD/DIS", qty=0, master_code="REVIEW", status="REVIEW"))
    return {"header":{"client":client or first(r"Client\s*:\s*(.+)", text),"sales_ref":sales_ref or first(r"Sales Ref(?:erence)?\.?\s*:\s*(.+)", text),"wo":wo or first(r"W\.?O\.?\s*No\.?\s*:\s*(.+)", text),"voltage":voltage or first(r"(?:RATED VOLTAGE|\b)(\d+(?:\.\d+)?)\s*K[Vv]", text)},"rows":[r.model_dump() for r in rows]}

@app.get("/health")
def health(): return {"status":"ok"}

@app.post("/api/bom/preview")
async def bom_preview(client: str = Form(""), sales_ref: str = Form(""), wo: str = Form(""), voltage: str = Form(""), msld: UploadFile = File(...), dis: UploadFile = File(...)):
    mdata, ddata = await msld.read(), await dis.read()
    return make_bom(extract_text(mdata, msld.filename), extract_text(ddata, dis.filename), client, sales_ref, wo, voltage)

@app.post("/api/bom/export")
async def bom_export(payload: dict):
    wb = Workbook(); ws = wb.active; ws.title = "BOM"
    h = payload.get("header", {})
    ws.append(["Client", h.get("client","")]); ws.append(["Sales Ref No.", h.get("sales_ref","")]); ws.append(["W.O. No.", h.get("wo","")]); ws.append(["Voltage Level", h.get("voltage","")]); ws.append([])
    ws.append(["EQPT.NO", "SPECIFICATION", "DESIGNATION", "DESCRIPTION", "QTY", "MASTER CODE", "STATUS"])
    for r in payload.get("rows", []): ws.append([r.get("equipment_no"), r.get("specification"), r.get("designation"), r.get("description"), r.get("qty"), r.get("master_code"), r.get("status")])
    for col in ws.columns:
        ws.column_dimensions[col[0].column_letter].width = min(max(max(len(str(c.value or "")) for c in col)+2, 12), 60)
    out = io.BytesIO(); wb.save(out); out.seek(0)
    return StreamingResponse(out, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition":"attachment; filename=ELEXORA_BOM.xlsx"})
