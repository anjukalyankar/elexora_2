from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from datetime import datetime
import fitz, io, re
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, Border, Side
from openpyxl.utils import get_column_letter

app = FastAPI(title="ELEXORA 2 API", version="0.3.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# Demo Master Data. Production Master Data will be replaceable by XLSX/CSV in the next module.
MASTER = [
    {"code": "CT2C-1A", "aliases": ["CURRENT TRANSFORMER", "CT"], "description": "CURRENT TRANSFORMER EPOXY CAST RESIN (WOUND TYPE)"},
    {"code": "PT", "aliases": ["POTENTIAL TRANSFORMER"], "description": "POTENTIAL TRANSFORMER (DRAWOUT TYPE)"},
    {"code": "7SJ6611", "aliases": ["NUM. PROT. RELAY", "NUMERICAL PROTECTION RELAY"], "description": "NUMERICAL PROTECTION RELAY"},
    {"code": "EM6400NG", "aliases": ["DIGITAL MF METER"], "description": "DIGITAL MF METER"},
    {"code": "AMMETER", "aliases": ["DIGITAL AMMETER"], "description": "DIGITAL AMMETER WITH BUILT IN SELECTOR SWITCH"},
    {"code": "VOLTMETER", "aliases": ["DIGITAL VOLTMETER"], "description": "DIGITAL VOLTMETER WITH BUILT IN SELECTOR SWITCH"},
    {"code": "MCB", "aliases": ["MCB FOR", "MINIATURE CIRCUIT BREAKER"], "description": "MINIATURE CIRCUIT BREAKER"},
]


def pdf_text(data: bytes, filename: str) -> str:
    if not filename.lower().endswith(".pdf"):
        return ""
    doc = fitz.open(stream=data, filetype="pdf")
    return "\n".join(page.get_text("text") for page in doc)


def first(pattern, text, default=""):
    m = re.search(pattern, text, re.I | re.M)
    return m.group(1).strip() if m else default


def voltage_normalize(value: str) -> str:
    value = (value or "").upper().replace("KV", "").strip()
    return value if value in {"6.6", "11", "33"} else ""


def extract_header(msld: str, dis: str, client: str, sales_ref: str, drawing: str, esd: str, wo: str, prep: str, voltage: str):
    s = msld + "\n" + dis
    return {
        "client": client.strip() or first(r"Client\s*:\s*([^\n]+)", s),
        "sales_ref": sales_ref.strip() or first(r"Sales\s*(?:Ref(?:erence)?|Order)\s*:?\s*([^\n]+)", s),
        "drawing": drawing.strip() or first(r"Drg\.?\s*No\.?\s*:?\s*([^\n]+)", s),
        "esd": esd.strip() or first(r"ESD\s*No\.?\s*:?\s*([^\n]+)", s),
        "wo": wo.strip() or first(r"W\.?O\.?\s*No\.?\s*:?\s*([^\n]+)", s),
        "prep_by": prep.strip(),
        "voltage": voltage_normalize(voltage),
    }


def extract_qty_near(anchor: str, text: str):
    # Fixed-template source uses explicit QTY./NOS. values. Never default to 1.
    m = re.search(re.escape(anchor) + r"[^\n]{0,120}?\b(?:QTY\.?|QUANTITY|NOS?\.?)\s*:?\s*(\d+)\b", text, re.I)
    if m:
        return int(m.group(1))
    return None


def extract_typical_feeders(msld: str):
    # MSLD is fixed; feeder designations are represented by explicit designation/range labels.
    # Quantities are accepted only when an explicit QTY/NOS value occurs in the same local block.
    result = []
    seen = set()
    for line in msld.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if not line:
            continue
        for m in re.finditer(r"\b((?:T|E|F|M)\d+(?:\s*[-/]\s*(?:T|E|F|M)\d+)?)\b", line, re.I):
            name = re.sub(r"\s+", "", m.group(1).upper())
            tail = line[m.end():]
            q = re.search(r"\b(?:QTY\.?|QUANTITY|NOS?\.?)\s*:?\s*(\d+)\b", tail, re.I)
            if q and name not in seen:
                result.append({"name": name, "qty": int(q.group(1))})
                seen.add(name)
    return result


def extract_designations(block: str):
    found = []
    for m in re.finditer(r"\b(?:T\d+(?:\s*[-/]\s*T\d+)?|E\d{1,3}|F\d{1,3}|M\d{1,3}|P\d{1,3}|K\d{1,3}|S\d{1,3}|X\d{1,3})\b", block, re.I):
        x = re.sub(r"\s+", "", m.group(0).upper())
        if x not in found:
            found.append(x)
    return found


def component_rows(msld: str, dis: str):
    s = msld + "\n" + dis
    patterns = [
        ("CURRENT TRANSFORMER", r"CURRENT TRANSFORMER(?:\s+EPOXY)?", "CT2C-1A"),
        ("POTENTIAL TRANSFORMER", r"POTENTIAL TRANSFORMER", "PT"),
        ("NUMERICAL PROTECTION RELAY", r"(?:NUM\.?\s*PROT\.?\s*RELAY|NUMERICAL PROTECTION RELAY)", "7SJ6611"),
        ("DIGITAL MF METER", r"DIGITAL MF METER", "EM6400NG"),
        ("DIGITAL AMMETER", r"DIGITAL AMMETER", "AMMETER"),
        ("DIGITAL VOLTMETER", r"DIGITAL VOLTMETER", "VOLTMETER"),
        ("MCB", r"\bMCB\b", "MCB"),
    ]
    rows = []
    sr = 1
    for label, pattern, master_code in patterns:
        matches = list(re.finditer(pattern, s, re.I))
        if not matches:
            continue
        for match in matches[:20]:
            start = max(0, match.start() - 80)
            end = min(len(s), match.end() + 520)
            block = re.sub(r"\s+", " ", s[start:end]).strip()
            master = next((x for x in MASTER if x["code"] == master_code), None)
            code_match = re.search(r"\b7SJ\d{4,}\b|\bEM\d{4,}[A-Z]*\b|\bCT2C-[\w-]+\b", block, re.I)
            designation = extract_designations(block)
            # Keep only designations relevant to component rows; do not copy every MSLD label into every row.
            designation = [x for x in designation if re.match(r"^(?:T\d|E\d|F\d|M\d|P\d|K\d|S\d|X\d)", x)]
            qty = None
            qmatch = re.search(r"\b(?:QTY\.?|QUANTITY|NOS?\.?)\s*:?\s*(\d+)\b", block, re.I)
            if qmatch:
                qty = int(qmatch.group(1))
            spec = block
            if master and master_code not in {"MCB"}:
                spec = block
            rows.append({
                "sr": sr,
                "specification": spec,
                "designation": ", ".join(designation),
                "typical_feeders": "",
                "total": qty,
                "eqpt_qty": qty,
                "mpd": "",
                "amd": "",
                "master_code": code_match.group(0).upper() if code_match else (master["code"] if master else ""),
                "source": "MSLD/DIS fixed template",
            })
            sr += 1
            # One row per component type in MVP; avoid duplicate rows caused by repeated labels in drawing text.
            break
    return rows


def extract(msld: str, dis: str, client: str, sales_ref: str, drawing: str, esd: str, wo: str, prep: str, voltage: str):
    h = extract_header(msld, dis, client, sales_ref, drawing, esd, wo, prep, voltage)
    rows = component_rows(msld, dis)
    feeders = extract_typical_feeders(msld)
    feeder_text = "; ".join(f"{x['name']} - {x['qty']}" for x in feeders)
    for row in rows:
        row["typical_feeders"] = feeder_text
    description_voltage = h["voltage"] or first(r"Description\s*:\s*([\d.]+)KV\s*SWITCHBOARD", msld + "\n" + dis, "")
    return {
        "header": h,
        "document_no": "SI EA/CS/FR/EG/015",
        "revision": "1.0",
        "created_by": "EA CS ENGG",
        "description": f"{description_voltage}kV SWITCHBOARD" if description_voltage else "SWITCHBOARD",
        "rows": rows,
        "typical_feeders": feeders,
        "qty": first(r"Qty\.?\s*:\s*([^\n]+)", msld + "\n" + dis),
        "warnings": ["Some quantities were not explicitly readable from the fixed MSLD/DIS template and were left blank."] if any(r["eqpt_qty"] is None for r in rows) else [],
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/bom/preview")
async def preview(client: str = Form(""), sales_ref: str = Form(""), drawing: str = Form(""), esd: str = Form(""), wo: str = Form(""), prep_by: str = Form(""), voltage: str = Form(""), msld: UploadFile = File(...), dis: UploadFile = File(...)):
    m, d = await msld.read(), await dis.read()
    return extract(pdf_text(m, msld.filename), pdf_text(d, dis.filename), client, sales_ref, drawing, esd, wo, prep_by, voltage)


def set_cell(ws, cell, value, bold=False, size=9, align="left"):
    c = ws[cell]
    c.value = value
    c.font = Font(name="Arial", size=size, bold=bold)
    c.alignment = Alignment(horizontal=align, vertical="center", wrap_text=True)
    return c


def build_fixed_bom(payload: dict):
    wb = Workbook()
    ws = wb.active
    ws.title = "BOM"
    thin = Side(style="thin")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    h = payload.get("header", {})
    rows = payload.get("rows", [])

    # Fixed template header from the supplied BOM FORMAT.pdf.
    ws.merge_cells("A1:H1")
    set_cell(ws, "A1", f"EQUIPMENT LIST   Doc.No.: {payload.get('document_no', 'SI EA/CS/FR/EG/015')}", True, 11, "left")
    ws.merge_cells("A2:H2")
    set_cell(ws, "A2", f"Rev.No.: {payload.get('revision', '1.0')}, Eff.Dt: 17/07/2026     Created By: {payload.get('created_by', 'EA CS ENGG')}", False, 9, "left")

    fields = [
        ("A3", "Item No.", "B3", "100"),
        ("A4", "Client", "B4", h.get("client", "")),
        ("A5", "Sales Ref No.", "B5", h.get("sales_ref", "")),
        ("A6", "DATE", "B6", datetime.now().strftime("%d.%m.%Y")),
        ("A7", "Description", "B7", payload.get("description", "")),
        ("A8", "W.O. No.", "B8", h.get("wo", "")),
        ("E3", "Drg. No.", "F3", h.get("drawing", "")),
        ("E4", "PRE.BY", "F4", h.get("prep_by", "")),
        ("E5", "Qty.", "F5", payload.get("qty", "")),
        ("E6", "ESD No.", "F6", h.get("esd", "")),
    ]
    for label_cell, label, value_cell, value in fields:
        set_cell(ws, label_cell, label, True, 8)
        set_cell(ws, value_cell, value, False, 8)
        ws[label_cell].border = border
        ws[value_cell].border = border

    header_row = 10
    headers = ["EQPT. NO.", "SPECIFICATION", "DESIGNATION", "TYPICAL FEEDERS", "TOTAL", "EQPT QTY", "MPD", "AMD"]
    for col, title in enumerate(headers, 1):
        c = ws.cell(header_row, col, title)
        c.font = Font(name="Arial", size=8, bold=True)
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = border
    ws.row_dimensions[header_row].height = 30

    for r_idx, r in enumerate(rows, header_row + 1):
        values = [r.get("sr", ""), r.get("specification", ""), r.get("designation", ""), r.get("typical_feeders", ""), r.get("total", ""), r.get("eqpt_qty", ""), "", ""]
        for col, value in enumerate(values, 1):
            c = ws.cell(r_idx, col, value if value is not None else "")
            c.font = Font(name="Arial", size=8)
            c.alignment = Alignment(horizontal="left" if col in (2, 3, 4) else "center", vertical="top", wrap_text=True)
            c.border = border
        ws.row_dimensions[r_idx].height = max(30, min(120, 15 * (str(r.get("specification", "")).count(" ") // 10 + 2)))

    footer_row = header_row + len(rows) + 2
    set_cell(ws, f"A{footer_row}", "Client :", True, 8)
    set_cell(ws, f"B{footer_row}", h.get("client", ""), False, 8)
    set_cell(ws, f"A{footer_row+1}", "Sales Ref No.:", True, 8)
    set_cell(ws, f"B{footer_row+1}", h.get("sales_ref", ""), False, 8)
    set_cell(ws, f"A{footer_row+2}", "DATE :", True, 8)
    set_cell(ws, f"B{footer_row+2}", datetime.now().strftime("%d.%m.%Y"), False, 8)
    set_cell(ws, f"A{footer_row+3}", "Description :", True, 8)
    set_cell(ws, f"B{footer_row+3}", payload.get("description", ""), False, 8)
    set_cell(ws, f"A{footer_row+4}", "W.O. No.:", True, 8)
    set_cell(ws, f"B{footer_row+4}", h.get("wo", ""), False, 8)
    set_cell(ws, f"E{footer_row}", "Drg. No.:", True, 8)
    set_cell(ws, f"F{footer_row}", h.get("drawing", ""), False, 8)
    set_cell(ws, f"E{footer_row+1}", "PRE.BY :", True, 8)
    set_cell(ws, f"F{footer_row+1}", h.get("prep_by", ""), False, 8)
    set_cell(ws, f"E{footer_row+2}", "Qty.:", True, 8)
    set_cell(ws, f"F{footer_row+2}", payload.get("qty", ""), False, 8)
    set_cell(ws, f"E{footer_row+3}", "ESD No.:", True, 8)
    set_cell(ws, f"F{footer_row+3}", h.get("esd", ""), False, 8)

    widths = [11, 52, 22, 28, 10, 11, 10, 10]
    for i, width in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.sheet_view.showGridLines = False
    ws.freeze_panes = "A11"
    return wb


@app.post("/api/bom/export")
async def export(payload: dict):
    wb = build_fixed_bom(payload)
    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    return StreamingResponse(out, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": "attachment; filename=ELEXORA_BOM.xlsx"})
