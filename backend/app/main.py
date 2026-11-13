from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from datetime import datetime
import fitz, io, re
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, Border, Side
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
import os
import secrets
import hashlib
import hmac
from fastapi import HTTPException, Depends, Header
from pydantic import BaseModel
from .relay_nlp import analyze_7sj66
from .siemens_live import lookup_siemens_product
from .admin_mlfb import build_admin_selection_chart_router

DATABASE_URL = os.getenv("DATABASE_URL")
TOKEN_TTL_SECONDS = 60 * 60 * 24 * 7

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable is required")

def db():
    import psycopg
    from psycopg.rows import dict_row
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)

def init_auth_db():
    with db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS users (
            id BIGSERIAL PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""")
        # Additive, restart-safe migration. Existing accounts default to user.
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'user'")
        # Never promote a hard-coded account. Configure the first Super Admin
        # explicitly in Render; ADMIN_USERNAME remains as a compatibility fallback.
        configured_super_admin = (
            os.getenv("SUPER_ADMIN_USERNAME")
            or os.getenv("ADMIN_USERNAME")
            or ""
        ).strip().lower()
        if configured_super_admin:
            conn.execute(
                "UPDATE users SET role='super_admin' WHERE username=%s AND role='user'",
                (configured_super_admin,)
            )
        conn.execute("""CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            expires_at BIGINT NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS bom_history (
            id BIGSERIAL PRIMARY KEY,
            username TEXT NOT NULL,
            customer_name TEXT NOT NULL DEFAULT '',
            work_order_no TEXT NOT NULL DEFAULT '',
            voltage TEXT NOT NULL DEFAULT '',
            msld_files JSONB NOT NULL DEFAULT '[]'::jsonb,
            dis_filename TEXT NOT NULL DEFAULT '',
            row_count INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""")
        # Staging is separate from the approved CSV used by BOM matching.
        conn.execute("""CREATE TABLE IF NOT EXISTS master_data_proposals (
            id BIGSERIAL PRIMARY KEY,
            record_data JSONB NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected')),
            submitted_by TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            reviewed_by TEXT,
            reviewed_at TIMESTAMPTZ,
            review_note TEXT NOT NULL DEFAULT ''
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS master_data_audit (
            id BIGSERIAL PRIMARY KEY,
            proposal_id BIGINT NOT NULL REFERENCES master_data_proposals(id) ON DELETE CASCADE,
            action TEXT NOT NULL,
            actor TEXT NOT NULL,
            details JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_master_data_proposals_status_created ON master_data_proposals(status, created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_master_data_audit_created ON master_data_audit(created_at DESC)")
        conn.commit()


def validate_password(password):
    errors = []
    if len(password) < 8: errors.append("at least 8 characters")
    if not re.search(r"[A-Z]", password): errors.append("one uppercase letter")
    if not re.search(r"[a-z]", password): errors.append("one lowercase letter")
    if not re.search(r"\d", password): errors.append("one number")
    if not re.search(r"[^A-Za-z0-9]", password): errors.append("one special character")
    return errors

def password_hash(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310000)
    return salt.hex() + "$" + digest.hex()

def password_ok(password, stored):
    try:
        salt_hex, digest_hex = stored.split("$", 1)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 310000).hex()
        return hmac.compare_digest(actual, digest_hex)
    except (ValueError, TypeError):
        return False

class AuthRequest(BaseModel):
    username: str
    password: str

def current_user(authorization: str = Header(default="")):
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Please sign in.")
    token = authorization[7:].strip()
    with db() as conn:
        row = conn.execute(
            "SELECT users.username FROM sessions JOIN users ON users.id=sessions.user_id "
            "WHERE sessions.token=%s AND sessions.expires_at>%s",
            (token, int(datetime.now().timestamp()))
        ).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="Session expired. Please sign in again.")
    return row["username"]


def require_admin(user: str = Depends(current_user)):
    with db() as conn:
        row = conn.execute(
            "SELECT role FROM users WHERE username=%s",
            (user.strip().lower(),)
        ).fetchone()
    if not row or row["role"] not in ("admin", "super_admin"):
        raise HTTPException(status_code=403, detail="Admin access only.")
    return user


def require_super_admin(user: str = Depends(current_user)):
    with db() as conn:
        row = conn.execute(
            "SELECT role FROM users WHERE username=%s",
            (user.strip().lower(),)
        ).fetchone()
    if not row or row["role"] != "super_admin":
        raise HTTPException(status_code=403, detail="Super Admin access only.")
    return user


app = FastAPI(title='ELEXORA 2 API', version='0.7.0')
app.add_middleware(CORSMiddleware, allow_origins=['https://elexora-2-nine.vercel.app', 'http://localhost:5173'], allow_origin_regex=r'https://.*\.vercel\.app', allow_credentials=True, allow_methods=['*'], allow_headers=['*'])
app.include_router(build_admin_selection_chart_router(db, require_admin))
REFERENCE_FEEDER_COLUMN = 'FEEDER TYPICAL'
MASTER = {'CT':'CURRENT TRANSFORMER','PT':'POTENTIAL TRANSFORMER (DRAWOUT TYPE)','7SJ6611':'NUMERICAL PROTECTION RELAY','EM6400NG':'DIGITAL MF METER','AMMETER':'DIGITAL AMMETER WITH BUILT IN SEL. S/W','VOLTMETER':'DIGITAL VOLTMETER WITH BUILT IN SEL. S/W','MCB':'MINIATURE CIRCUIT BREAKER'}
LED_MASTER_CACHE = None

def load_led_master():
    global LED_MASTER_CACHE
    if LED_MASTER_CACHE is not None:
        return LED_MASTER_CACHE
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'master_database.xlsx')
    csv_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'master_database.csv')
    try:
        if os.path.exists(path):
            from openpyxl import load_workbook
            wb = load_workbook(path, read_only=True, data_only=True)
            ws = wb.active
            headers = [str(c.value or '').strip().upper() for c in ws[1]]
            data_rows = ws.iter_rows(min_row=2, values_only=True)
        elif os.path.exists(csv_path):
            import csv
            fh = open(csv_path, newline='', encoding='utf-8')
            reader = csv.DictReader(fh)
            headers = [str(h or '').strip().upper() for h in (reader.fieldnames or [])]
            data_rows = ([row.get(h, '') for h in reader.fieldnames or []] for row in reader)
        else:
            LED_MASTER_CACHE = []
            return LED_MASTER_CACHE
        idx = {name:i for i,name in enumerate(headers)}
        records = []
        for values in data_rows:
            category = str(values[idx['CATEGORY']] or '').strip().upper() if 'CATEGORY' in idx else ''
            if category != 'LED LAMP':
                continue
            description = str(values[idx['DISCRIPTION']] or '').strip() if 'DISCRIPTION' in idx else ''
            manufacturer = str(values[idx['MANUFACTURER']] or '').strip() if 'MANUFACTURER' in idx else ''
            model = str(values[idx['MODEL']] or '').strip() if 'MODEL' in idx else ''
            colour = first_match(r'COLOUR\s*:\s*([^,]+)', description)
            voltage = first_match(r'VOLTAGE\s*:\s*([^,]+)', description)
            if description and colour and voltage:
                records.append({'description':description,'manufacturer':manufacturer,'model':model,'colour':clean_value(colour).upper(),'voltage':clean_value(voltage).upper()})
        if 'wb' in locals(): wb.close()
        if 'fh' in locals(): fh.close()
        LED_MASTER_CACHE = records
    except Exception:
        LED_MASTER_CACHE = []
    return LED_MASTER_CACHE

def load_mcb_master():
    """Load MCB master records from the existing CSV, preserving the CSV as source of truth."""
    import csv
    csv_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'master_database.csv')
    records = []
    if not os.path.exists(csv_path):
        return records
    with open(csv_path, newline='', encoding='utf-8-sig') as fh:
        for row in csv.DictReader(fh):
            if (row.get('CATEGORY') or '').strip().upper() != 'MCB':
                continue
            records.append({key.lower(): (value or '').strip() for key, value in row.items()})
    return records


def load_mcb_addon_master():
    import csv
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'master_database.csv')
    if not os.path.exists(path): return None
    with open(path, newline='', encoding='utf-8-sig') as fh:
        for row in csv.DictReader(fh):
            if (row.get('CATEGORY') or '').strip().upper() == 'ADDON' and (row.get('MATERIAL_CODE') or '').strip().upper() == 'MCB-ADDON':
                return {key.lower(): (value or '').strip() for key, value in row.items()}
    return None



def load_control_link_master(material_code):
    """Load a control-link or control-fuse-base description from the material master."""
    import csv
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'master_database.csv')
    if not os.path.exists(path):
        return None
    target = str(material_code or '').strip().upper()
    with open(path, newline='', encoding='utf-8-sig') as fh:
        for row in csv.DictReader(fh):
            if (row.get('MATERIAL_CODE') or '').strip().upper() == target:
                return {key.lower(): (value or '').strip() for key, value in row.items()}
    return None

def load_vcb_master():
    """Load the Q10 VCB catalogue entry from the existing material master CSV."""
    import csv
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'master_database.csv')
    if not os.path.exists(path):
        return None
    with open(path, newline='', encoding='utf-8-sig') as fh:
        for row in csv.DictReader(fh):
            if (row.get('CATEGORY') or '').strip().upper() == 'VCB' and (row.get('MATERIAL_CODE') or '').strip().upper() == 'VCB-Q10-3AH3-3152':
                return {key.lower(): (value or '').strip() for key, value in row.items()}
    return None


def vcb_spec(record, dis_text, msld_text):
    """Build Q10 VCB specification and MLFB from the Siemens 3AH3 selection chart."""
    from .vcb_chart import build_3ah3_q10_spec
    return build_3ah3_q10_spec(record, dis_text, msld_text)

def mcb_master_match(description, details, designation):
    """Match an MCB to its master description using current, pole count and AC/DC type."""
    text = clean_value(' '.join([description or '', details or '', designation or ''])).upper()
    records = load_mcb_master()
    if not records:
        return None

    # If the extracted text contains an exact master model, use that record first.
    model_candidates = [
        item for item in records
        if item.get('model', '').upper() and item.get('model', '').upper() in text
    ]
    candidates = model_candidates if model_candidates else records

    def extract_pole(value):
        value = re.sub(r'\bPOLES?\s*:\s*', '', value, flags=re.IGNORECASE)
        value = re.sub(r'\b([1-4])\s*POLES?\b', r'\1P', value, flags=re.IGNORECASE)
        value = re.sub(r'\b([1-4])\s*P\s*\+\s*N\b', r'\1P+N', value, flags=re.IGNORECASE)
        match = re.search(r'\b([1-4]P\+N|[1-4]P)\b', value, re.IGNORECASE)
        return re.sub(r'\s+', '', match.group(1)).upper() if match else ''

    def extract_current(value):
        # Read the explicit rating first to avoid confusing breaking capacity with current.
        match = re.search(r'\bRATING\s*:\s*(\d+(?:\.\d+)?)\s*(?:A\b|AMPS?\b)', value, re.IGNORECASE)
        if not match:
            match = re.search(r'\b(\d+(?:\.\d+)?)\s*(?:A\b|AMPS?\b)', value, re.IGNORECASE)
        return float(match.group(1)) if match else None

    def extract_supply_type(value):
        # Numeric voltage is intentionally ignored; only AC vs DC is compared.
        voltage_match = re.search(
            r'\b(?:RATED\s+)?VOLTAGE\s*:?\s*\d+(?:\.\d+)?\s*V?\s*(AC|DC)\b'
            r'|\b\d+(?:\.\d+)?\s*V\s*(AC|DC)\b',
            value, re.IGNORECASE
        )
        if voltage_match:
            return next((group.upper() for group in voltage_match.groups() if group), '')
        cleaned = re.sub(r'\bAC\s*/\s*DC\b|\bDC\s*/\s*AC\b', ' ', value, flags=re.IGNORECASE)
        types = set(x.upper() for x in re.findall(r'\b(AC|DC)\b', cleaned, re.IGNORECASE))
        return next(iter(types)) if len(types) == 1 else ''

    requested_pole = extract_pole(text)
    requested_current = extract_current(text)
    requested_type = extract_supply_type(text)
    if not requested_pole or requested_current is None or not requested_type:
        return None

    verified = []
    for item in candidates:
        desc = item.get('discription', '')
        if (extract_pole(desc) != requested_pole
                or extract_current(desc) != requested_current
                or extract_supply_type(desc) != requested_type):
            continue
        verified.append(item)

    # A unique attribute match is safe. If multiple records match, prefer the
    # material code that explicitly encodes the same AC/pole/current attributes.
    if len(verified) == 1:
        return verified[0]
    if len(verified) > 1:
        expected_code = f"MCB-{requested_type}-{requested_pole}-{int(requested_current) if requested_current.is_integer() else requested_current}A"
        exact = [item for item in verified if item.get('material_code', '').upper() == expected_code]
        if len(exact) == 1:
            return exact[0]
    return None

def mcb_spec(item):
    """Return only the master database DISCRIPTION, preserving its wording and order."""
    if not item:
        return '', []
    description = item.get('discription', '').strip()
    lines = [part.strip() for part in description.split(',') if part.strip()]
    return '\n'.join(lines), lines


def extract_led_attributes(description, details, designation):
    text = clean_value(' '.join([description or '', details or '', designation or '']))
    colour = first_match(r'\b(RED|GREEN|AMBER|YELLOW|BLUE|WHITE|CLEAR)\b', text).upper()
    voltage = first_match(r'\b(\d+(?:/\d+)?V(?:\s*AC/DC|\s*AC|\s*DC)?)\b', text)
    if not voltage:
        voltage = first_match(r'\b(\d+(?:\.\d+)?(?:\s*TO\s*\d+(?:\.\d+)?)?V(?:\s*AC\s*&\s*\d+(?:\.\d+)?(?:\s*TO\s*\d+)?V)?\s*(?:AC|DC))\b', text)
    return colour, clean_value(voltage).upper()

def led_master_match(description, details, designation):
    colour, voltage = extract_led_attributes(description, details, designation)
    if not colour or not voltage:
        return None
    target = re.sub(r'\s+', ' ', voltage).upper()
    for item in load_led_master():
        if item['colour'] == colour and re.sub(r'\s+', ' ', item['voltage']).upper() == target:
            return item
    return None

def led_spec(description, details, designation):
    item = led_master_match(description, details, designation)
    if not item:
        return [], None
    make = (item.get('manufacturer') or '').strip()
    desc = item.get('description','').strip()
    parts = [p.strip() for p in desc.split(',') if p.strip()]
    colour = next((p for p in parts if p.upper().startswith('COLOUR')), '')
    voltage = next((p for p in parts if p.upper().startswith('VOLTAGE')), '')
    typ = next((p for p in parts if p.upper().startswith('TYPE')), '')
    lines = []
    if make: lines.append(f'{make.upper()} MAKE')
    lines.append('LED LAMP (COMPLETE UNIT)')
    if colour: lines.append(re.sub(r'\s*:\s*', '  : ', colour, count=1))
    if voltage: lines.append(re.sub(r'\s*:\s*', ' : ', voltage, count=1))
    if typ: lines.append(re.sub(r'\s*:\s*', ' : ', typ, count=1))
    return lines, item


def led_spec_from_item(item):
    """Format a selected master-database LED record using the standard BOM layout."""
    if not item:
        return [], None
    make = (item.get('manufacturer') or '').strip()
    desc = (item.get('description') or '').strip()
    parts = [p.strip() for p in desc.split(',') if p.strip()]
    colour = next((p for p in parts if p.upper().startswith('COLOUR')), '')
    voltage = next((p for p in parts if p.upper().startswith('VOLTAGE')), '')
    typ = next((p for p in parts if p.upper().startswith('TYPE')), '')
    lines = []
    if make:
        lines.append(f'{make.upper()} MAKE')
    lines.append('LED LAMP (COMPLETE UNIT)')
    if colour:
        lines.append(re.sub(r'\s*:\s*', '  : ', colour, count=1))
    if voltage:
        lines.append(re.sub(r'\s*:\s*', ' : ', voltage, count=1))
    if typ:
        lines.append(re.sub(r'\s*:\s*', ' : ', typ, count=1))
    return lines, item


def led_master_match_for_colour(colour, voltage):
    """Match exact master voltage; only 63.5V AC/DC can use the specified wide-range LED."""
    colour = clean_value(colour).upper()
    target = re.sub(r'\s+', ' ', clean_value(voltage).upper()).strip()
    if not colour or not target:
        return None
    for item in load_led_master():
        if item.get('colour', '').upper() != colour:
            continue
        master = re.sub(r'\s+', ' ', item.get('voltage', '').upper()).strip()
        if master == target:
            return item
        if re.fullmatch(r'63\.5\s*V\s*AC', target, re.I) and re.search(r'42\s*TO\s*240\s*V\s*AC', master, re.I) and re.search(r'42\s*TO\s*220\s*V\s*DC', master, re.I):
            return item
        if re.fullmatch(r'63\.5\s*V\s*DC', target, re.I) and re.search(r'42\s*TO\s*220\s*V\s*DC', master, re.I) and re.search(r'42\s*TO\s*240\s*V\s*AC', master, re.I):
            return item
    return None


def led_specs_for_h7_h8_h9(description, details, designation):
    combined = clean_value(' '.join([str(description or ''), str(details or ''), str(designation or '')]))
    des = re.sub(r'\s+', '', str(designation or '').upper())
    has_group = ('H7' in des and 'H8' in des and 'H9' in des)
    has_group = has_group or bool(re.search(r'\bH7\b.*\bH8\b.*\bH9\b', combined, re.I))
    has_group = has_group or bool(re.search(r'\bH7\s*[/,-]\s*H8\s*[/,-]\s*H9\b', combined, re.I))
    if not has_group:
        return []
    vm = re.search(r'\b(\d+(?:\.\d+)?)\s*V\s*(AC|DC)\b', combined, re.I)
    if not vm:
        return []
    voltage = f'{vm.group(1)}V {vm.group(2).upper()}'
    results = []
    for code, colour in (('H7', 'RED'), ('H8', 'YELLOW'), ('H9', 'BLUE')):
        item = led_master_match_for_colour(colour, voltage)
        lines, item = led_spec_from_item(item)
        if lines:
            results.append({'designation': code, 'specification_lines': lines, 'item': item})
    return results

# Fixed CT BOM structure. Project values are extracted from the fixed MSLD/DIS templates;
# wording and line order remain fixed.
CT_TEMPLATE = [
 ('CT_CODE','MSLD_GENERATED'),('MAKE','DIS'),('CT_TYPE','MSLD'),
 ('- SINGLE PHASE, AS PER IS/IEC STANDARD','FIXED'),('- RATED VOLTAGE','DIS'),
 ('- RATED FREQUENCY','DIS'),('- SHORT TIME CURRENT','MSLD'),
 ('- BIL','DIS_CONSTRUCTED'),('INSULATION CLASS:-B','EDITABLE_FIXED'),
 ('AS GIVEN BELOW:-','FIXED'),('TWO CORE CT, CTR','MSLD'),
 ('- CORE 1','MSLD'),('- CORE 2','MSLD'),('SUITABLE FOR','DIS'),
 ('CT SECONDARY TERMINAL ON P2 SIDE','EDITABLE_FIXED')]

def pdf_pages(data,filename):
 return fitz.open(stream=data,filetype='pdf') if filename.lower().endswith('.pdf') else []
def pdf_text(data,filename): return '\n'.join(p.get_text('text') for p in pdf_pages(data,filename))
def first(pattern,text,default=''):
 m=re.search(pattern,text,re.I|re.M); return m.group(1).strip() if m else default
def norm_voltage(v):
 v=re.sub(r'\s+', '', str(v or '').strip().upper())
 v=re.sub(r'KV$', '', v)
 return v

def transform_word(page,word):
 pts=[fitz.Point(word[0],word[1])*page.rotation_matrix,fitz.Point(word[2],word[3])*page.rotation_matrix]
 return {'x':(pts[0].x+pts[1].x)/2,'y':(pts[0].y+pts[1].y)/2,'text':word[4]}
def page_words(page): return [transform_word(page,w) for w in page.get_text('words') if w[4].strip()]
def clean(s): return re.sub(r'\s+',' ',s).strip(' -')
def words_text(words): return ' '.join(w['text'] for w in sorted(words,key=lambda z:(round(z['y'],1),z['x'])))
def text_in_band(words,xmin,xmax,ymin,ymax): return clean(words_text([w for w in words if xmin<=w['x']<xmax and ymin<=w['y']<ymax]))
def rows_by_designation(words,xmin,xmax,ymin,ymax):
 vals=[]
 for w in words:
  if xmin<=w['x']<=xmax and ymin<=w['y']<=ymax:
   t=clean(w['text'])
   if t.upper() in {'DESIG.','QTY.','NO.'}: continue
   if re.fullmatch(r'[A-Z0-9]+(?:[-/,+][A-Z0-9]+)*',t,re.I) and (re.search(r'\d',t) or re.fullmatch(r'PS(?:\+S28)?',t,re.I)): vals.append((w['y'],t))
 vals.sort(); out=[]
 for y,t in vals:
  if not out or abs(y-out[-1][0])>5: out.append([y,t])
 return out
def designation_qty(des):
 total=0
 for part in re.split(r'[,/]',(des or '').upper()):
  part=part.strip()
  # Supports both letter-prefixed (F1-F3) and digit+letter (1H1-1H3)
  # designations used by the fixed MSLD template.
  m=re.fullmatch(r'(?:[A-Z]+|\d+[A-Z]+)(\d+)-(?:[A-Z]+|\d+[A-Z]+)?(\d+)',part)
  if m: total+=max(1,int(m.group(2))-int(m.group(1))+1)
  elif re.fullmatch(r'(?:[A-Z]+|\d+[A-Z]+)\d+',part): total+=1
 if total==0 and des:
  m=re.fullmatch(r'(?:[A-Z]+|\d+[A-Z]+)(\d+)-(?:[A-Z]+|\d+[A-Z]+)(\d+)',des.upper())
  if m: total=max(1,int(m.group(2))-int(m.group(1))+1)
 return total or None
def is_test_service_limit_switch(description, details, designation):
    """Identify the breaker test/service limit switch from its function and contacts."""
    source = clean_value(' '.join([
        str(description or ''), str(details or ''), str(designation or '')
    ])).upper()
    if not re.search(r'\bLIMIT\s+SWITCH\b', source):
        return False
    if not (re.search(r'\bTEST\b', source) and re.search(r'\bSERVICE\b', source)):
        return False

    # Accept explicit per-position contact statements or the compact
    # "4NO TEST + 4NO SERVICE" wording found in extracted MSLD rows.
    test_contacts = bool(
        re.search(r'\b4\s*NO\s*(?:AT\s*)?TEST\b', source)
        or re.search(r'\bTEST\s*[:=-]?\s*4\s*NO\b', source)
    )
    service_contacts = bool(
        re.search(r'\b4\s*NO\s*(?:AT\s*)?SERVICE\b', source)
        or re.search(r'\bSERVICE\s*[:=-]?\s*4\s*NO\b', source)
    )
    combined_contacts = bool(re.search(
        r'\b4\s*NO\s+AT\s+EACH\s+TEST\s*(?:&|AND|/)\s*SERVICE\b',
        source
    ))
    return combined_contacts or (test_contacts and service_contacts)


def master_match(description,details,designation):
 s=(description+' '+details+' '+str(designation or '')).upper()
 des=re.sub(r'\s+','',str(designation or '')).upper()
 if is_test_service_limit_switch(description, details, designation):return 'S16_AUX_SWITCH'
 if des == 'S16':return 'S16_AUX_SWITCH'
 if des == 'E2':return 'E2_LED'
 if 'LED LAMP' in s or re.search(r'\bLED\b',s):return 'LED'
 if 'CURRENT TRANSFORMER' in s:return 'CT'
 if 'PT DAMPING RESISTOR' in s or ('DAMPING RESISTOR' in s and ('PT' in s or 'OPEN DELTA' in s)):return 'PT_DAMPING_RESISTOR'
 if 'POTENTIAL TRANSFORMER' in s:return 'PT'
 if re.search(r'\b7SJ66\d{2}\b', s) or 'NUM. PROT. RELAY' in s or 'NUMERICAL PROTECTION RELAY' in s:return '7SJ6611'
 if 'VAJH13' in s or 'MASTER TRIP RELAY' in s:return 'VAJH13'
 if 'DIGITAL MF METER' in s or 'EM6400NG' in s:return 'EM6400NG'
 if 'DIGITAL VOLTMETER' in s:return 'VOLTMETER'
 if 'DIGITAL AMMETER' in s:return 'AMMETER'
 if '3NW32NNSL' in re.sub(r'[^A-Z0-9]','',s) or 'CONTROL LINK' in s:return 'CONTROL_LINK'
 if 'MINIATURE CIRCUIT BREAKER' in s or re.search(r'\bMCB\b',s) or '5SL4206-7RC' in s:return 'MCB'
 return designation or 'UNMATCHED'

def extract_fixed_table(data,filename):
 pages=pdf_pages(data,filename); records=[]; feeders=[]; warnings=[]
 for page_no,page in enumerate(pages,1):
  if page_no!=1:continue
  words=page_words(page); feeder_type=text_in_band(words,440,520,410,430); feeder_designation=text_in_band(words,440,520,395,415); feeder_rating=text_in_band(words,440,520,430,450); wiring=text_in_band(words,440,520,450,465); feeder_quantity=text_in_band(words,440,520,360,380)
  if feeder_type or feeder_designation:feeders.append({'name':feeder_designation or feeder_type,'type':feeder_type,'designation':feeder_designation,'rating':feeder_rating,'wiring':wiring,'quantity':feeder_quantity or '1'})
  # Q10 occupies the CB TYPE / RATING / DETAILS / MAKE table row.
  # Its designation is in the item-number column (x=275..320), while the
  # two-line VCB specification is in the brief-description column (x=320..615).
  q10_designation=text_in_band(words,275,320,460,480)
  if re.search(r'\bQ10\b',q10_designation,re.I):
   q10_desc=text_in_band(words,50,275,460,480)
   q10_details=text_in_band(words,320,615,465,495)
   records.append({'source_page':page_no,'description':q10_desc,'designation':'Q10','details':q10_details,'master_code':'VCB','quantity':1,'section':'MSLD equipment'})
  left=rows_by_designation(words,275,320,450,790)
  for i,(y,des) in enumerate(left):
   if des.upper() == 'Q10':
    continue
   next_y=left[i+1][0] if i+1<len(left) else 790
   # T1-T3 is a two-line CT block in the fixed MSLD template. Capture the
   # complete technical-data block rather than only one visual row.
   row_top = y-18 if des.upper() == 'T1-T3' else y-9
   lo,hi=max(490,row_top),min(790,(y+next_y)/2)
   desc=text_in_band(words,50,275,lo,hi); details=text_in_band(words,320,615,lo,hi)
   if desc or details:records.append({'source_page':page_no,'description':desc,'designation':des,'details':details,'master_code':master_match(desc,details,des),'quantity':designation_qty(des),'section':'MSLD equipment'})
  for ymin,ymax in [(35,345),(350,725)]:
   right=rows_by_designation(words,840,880,ymin,ymax)
   for i,(y,des) in enumerate(right):
    next_y=right[i+1][0] if i+1<len(right) else ymax; lo,hi=max(ymin,y-8),min(ymax,(y+next_y)/2); desc=text_in_band(words,630,835,lo,hi); details=text_in_band(words,890,1175,lo,hi)
    if desc or details:records.append({'source_page':page_no,'description':desc,'designation':des,'details':details,'master_code':master_match(desc,details,des),'quantity':designation_qty(des),'section':'MSLD equipment'})
 return feeders,records,warnings

def extract_header_from_page(data,filename):
 pages=pdf_pages(data,filename)
 if not pages:return {}
 words=page_words(pages[0]); band=lambda a,b,c,d:text_in_band(words,a,b,c,d); drawing_band=band(980,1110,805,830); drawing=first(r'(G\d{4,}-[A-Z0-9]+-[A-Z0-9]+)',drawing_band,drawing_band); esd=first(r'\b(SED\d+)\b',drawing_band) or first(r'\b(SED\d+)\b',words_text(words))
 return {'client':band(345,440,782,792),'sales_ref':band(910,960,782,793),'drawing':drawing,'esd':esd,'wo':band(905,960,793,805),'description':band(690,835,775,795),'qty':band(1000,1040,790,805),'master_singleline':band(690,835,800,815)}
def header(msld,dis,client,sales,drawing,esd,wo,prep,voltage):
 s=msld+'\n'+dis
 return {'client':client.strip() or first(r'Client\s*:?\s*([^\n]+)',s),'sales_ref':sales.strip() or first(r'Sales\s*Ref\.?\s*:?\s*([^\n]+)',s),'drawing':drawing.strip() or first(r'Drg\.?\s*No\.?\s*:?\s*([^\n]+)',s),'esd':esd.strip() or first(r'\b(SED\d+)\b',s) or first(r'ESD\s*No\.?\s*:?\s*([^\n]+)',s),'wo':wo.strip() or first(r'W\.?O\.?\s*No\.?\s*:?\s*([^\n]+)',s),'prep_by':prep.strip(),'voltage':norm_voltage(voltage)}

def clean_value(value):
    if not value:
        return ''
    value = value.replace('\n', ' ')
    value = re.sub(r'\s+', ' ', value)
    return value.strip()

def first_match(pattern, text, flags=re.IGNORECASE):
    if not text:
        return ''
    m = re.search(pattern, text, flags)
    return clean_value(m.group(1)) if m else ''

def extract_ct_ratio(ct_text):
    m = re.search(r'\bCTR\s*:\s*([0-9]+(?:\.[0-9]+)?(?:-[0-9]+(?:\.[0-9]+)?)?)\s*/', ct_text or '', re.I)
    return m.group(1) if m else ''

def extract_ct_secondary_current(ct_text):
    m = re.search(r'\bCTR\s*:\s*[0-9]+(?:\.[0-9]+)?(?:-[0-9]+(?:\.[0-9]+)?)?\s*/\s*([0-9]+(?:\.[0-9]+)?)\s*A', ct_text or '', re.I)
    return f'{m.group(1)}A' if m else ''

def extract_ct_core_lines(ct_text):
    labeled = re.findall(r'CORE\s*(\d+)\s*:\s*(.*?)(?=,\s*CORE\s*\d+\s*:|$)', ct_text or '', re.I)
    if labeled:
        return [clean_value(v) for _, v in sorted(labeled, key=lambda x:int(x[0]))]
    values=[]
    # The fixed MSLD extraction normalizes line breaks, so multiple CTR
    # entries can arrive on the same line. Stop each core at the next CTR
    # (or STC) instead of consuming the whole technical-data string.
    for m in re.finditer(r'\bCTR\s*:\s*(.*?)(?=\s+CTR\s*:|\s+STC\s*:|\s+SHORT\s+TIME\s+CURRENT\s*:|$)', ct_text or '', re.I):
        value=clean_value(m.group(1))
        if value: values.append(value)
    return values

def extract_ct_core_count(ct_text):
    explicit=first_match(r'NO\.?\s*OF\s*CORES\s*[:\-]?\s*(\d+)',ct_text)
    if explicit: return int(explicit)
    return len(extract_ct_core_lines(ct_text)) or 1

def generate_ct_code(ct_text):
    ratio=extract_ct_ratio(ct_text)
    secondary=extract_ct_secondary_current(ct_text)
    count=extract_ct_core_count(ct_text)
    return f'CT{ratio}{count}C-{secondary}' if ratio and secondary else ''

def extract_ct_type(ct_text):
    for pattern in [r'(EPOXY\s+CAST\s+RESIN\s*\(\s*WOUND\s+TYPE\s*\))',r'(WOUND\s+TYPE)',r'(WINDOW\s+TYPE)']:
        value=first_match(pattern,ct_text)
        if value:return value.upper()
    return ''

def build_ct_description(ct_text):
    t=extract_ct_type(ct_text)
    # Fixed CT wording required by the BOM template.
    return f'CURRENT TRANSFORMER EPOXY CAST RESIN ({t})' if t else 'CURRENT TRANSFORMER EPOXY CAST RESIN (WOUND TYPE)'

def build_ctr_line(ct_text):
    n=extract_ct_core_count(ct_text)
    word={1:'SINGLE',2:'TWO',3:'THREE'}.get(n,str(n))
    return f'{word} CORE CT'

def dis_search_text(dis_text):
    return re.sub(r'\s+', ' ', dis_text or '').strip()

def dis_field(dis_text, code, label=''):
    # The DIS is a fixed template. Use the numbered field as the primary
    # anchor because PDF text extraction can change spacing or wording.
    text = dis_search_text(dis_text)
    pattern = rf'{re.escape(code)}\s*(?:{label}\s*)?:?\s*(.*?)(?=\s+\d{{1,2}}\.\d{{2}}\.\d{{2}}\s+|$)'
    m = re.search(pattern, text, re.IGNORECASE)
    return clean_value(m.group(1)) if m else ''

def extract_annexure_make(dis_text, equipment):
    """Return the common CT/PT make list from DIS Annexure 1."""
    text = dis_search_text(dis_text)
    section_match = re.search(
        r'ANNEXURE\s*[-:]?\s*1\b(.*?)(?=ANNEXURE\s*[-:]?\s*[2-9]\b|$)',
        text, re.IGNORECASE | re.DOTALL
    )
    section = section_match.group(1) if section_match else text
    # The approved description lists all three makes together for both CT and PT.
    required = ['ECS', 'NPT', 'PRAGATI']
    present = [name for name in required if re.search(rf'\b{re.escape(name)}\b', section, re.IGNORECASE)]
    if len(present) == 3:
        return 'ECS/NPT/PRAGATI'
    # Annexure text extraction may lose part of a table. Keep the requested
    # standard list consistent rather than outputting a misleading single make.
    return 'ECS/NPT/PRAGATI'



def extract_ct_make(dis_text):
    text = dis_search_text(dis_text)
    # Fixed DIS make may appear as "PRAGATI MAKE", "PRAGATI/ECS MAKE",
    # "CT MAKE: PRAGATI", or "MAKE OF CT: PRAGATI". Do not depend on one
    # exact PDF text layout.
    patterns = [
        r'\b(PRAGATI(?:\s*/\s*[A-Z][A-Z0-9.&\-]*)+)\s+MAKE\b',
        r'\b(PRAGATI(?:\s*/\s*[A-Z][A-Z0-9.&\-]*)+)\b',
        r'\b(PRAGATI)\s+MAKE\b',
        r'(?:CT\s*/?\s*MAKE|CT/PT\s+MAKE|MAKE\s+OF\s+CT)\s*[:\-]?\s*([A-Za-z][A-Za-z0-9.&/\-]*(?:\s*/\s*[A-Za-z][A-Za-z0-9.&/\-]*)*)',
        r'\bMAKE\s*[:\-]\s*([A-Za-z][A-Za-z0-9.&/\-]*(?:\s*/\s*[A-Za-z][A-Za-z0-9.&/\-]*)*)',
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            value = re.sub(r'\s*/\s*', '/', clean_value(m.group(1)))
            if value and value.upper() not in {'OF','CT','PT','MAKE'}:
                return value
    return ''

def extract_rated_voltage(dis_text):
    value = dis_field(dis_text, '1.03.00', r'(?:Rated\s+(?:operational\s+)?voltage)')
    m = re.search(r'([0-9]+(?:\.[0-9]+)?)\s*(KV|kV)', value or '', re.IGNORECASE)
    if m:
        return f'{m.group(1)}kV'
    text = dis_search_text(dis_text)
    m = re.search(r'Rated\s+(?:operational\s+)?voltage\s*:?\s*([0-9]+(?:\.[0-9]+)?)\s*(KV|kV)', text, re.IGNORECASE)
    return f'{m.group(1)}kV' if m else ''

def extract_frequency(dis_text):
    value = dis_field(dis_text, '1.02.00', r'Main\s+System')
    text = value or dis_search_text(dis_text)
    m = re.search(r'([0-9]+(?:\.[0-9]+)?)\s*Hz', text, re.IGNORECASE)
    return f'{m.group(1)}Hz' if m else ''

def extract_bil(dis_text):
    values = []
    for code in ('1.04.00','1.06.00','1.07.00'):
        value = dis_field(dis_text, code)
        m = re.search(r'([0-9]+(?:\.[0-9]+)?)', value)
        if not m:
            return ''
        values.append(m.group(1))
    return '/'.join(values) + 'KVp'

def extract_panel_suitability(dis_text):
    # Source is DIS 2.01.00 "Type of switchboard", not 2.02.01 Location.
    # Example fixed-template value:
    # 8BK80(1000mm)+3AH3 VCB
    value = dis_field(dis_text, '2.01.00', r'Type\s+of\s+switchboard')
    if value:
        m = re.search(r'\b(8BK80)\s*\(?\s*(\d{3,4})\s*mm\s*\)?', value, re.I)
        if m:
            return f'{m.group(1).upper()}-{m.group(2)}mm WIDTH PANEL'
    # Fallback: search the whole DIS only if the numbered field was not
    # extracted cleanly.
    text = dis_search_text(dis_text)
    m = re.search(r'\b(8BK80)\s*\(?\s*(\d{3,4})\s*mm\s*\)?', text, re.I)
    if m:
        return f'{m.group(1).upper()}-{m.group(2)}mm WIDTH PANEL'
    return ''

def extract_pt_core_lines(pt_text):
    """Extract only each PT core's ratio, burden and accuracy class; never absorb adjacent MSLD equipment."""
    text = clean_value(pt_text)
    # The PT core table is followed by other equipment rows in flattened PDF text.
    # Extract bounded engineering tokens instead of consuming everything until the next CORE label.
    core_matches = re.findall(
        r'(?:\b(?:CORE\s*\d+\s*:\s*)?)'
        r'((?:\d+(?:\.\d+)?\s*kV\s*/\s*(?:√|\bSQRT\b)?\s*3\s*/\s*'
        r'\d+(?:\.\d+)?\s*/\s*(?:√|\bSQRT\b)?\s*3\s*,?\s*)?'
        r'\d+(?:\.\d+)?\s*VA\s*,?\s*CL\.?\s*-?\s*[A-Z0-9.]+)',
        text, re.IGNORECASE
    )
    result = []
    for value in core_matches:
        value = clean_value(value).rstrip(' ,;')
        if value and value not in result:
            result.append(value)
    return result



def extract_pt_fuse_rating(msld_text, dis_text):
    text = clean_value((msld_text or '') + ' ' + (dis_text or ''))
    patterns = [
        r'(?:IN[- ]?BUILT\s+)?PRIMARY\s+FUSE(?:\s+OF|\s+RATING)?\s*[:=]?\s*(\d+(?:\.\d+)?)\s*A\b',
        r'FUSE\s+(?:RATING|CURRENT)\s*[:=]?\s*(\d+(?:\.\d+)?)\s*A\b',
    ]
    for pattern in patterns:
        value = first_match(pattern, text)
        if value:
            return f'{value}A'
    return ''


def extract_pt_panel_suitability(msld_text, dis_text=''):
    """Use the exact same panel extraction source/rule as CT: DIS 2.01.00."""
    return extract_panel_suitability(dis_text)



def pt_spec(pt_text, msld_text, dis_text):
    """Format PT description from MSLD/DIS using the approved fixed layout."""
    source = clean_value((pt_text or '') + ' ' + (msld_text or ''))
    rated_voltage = extract_rated_voltage(dis_text)
    if not rated_voltage:
        rated_voltage = first_match(r'\b([0-9]+(?:\.[0-9]+)?\s*kV)\b', source)
        rated_voltage = re.sub(r'\s+', '', rated_voltage)
    # PT make is sourced from DIS.
    make = extract_annexure_make(dis_text, 'PT') or extract_ct_make(dis_text)
    if not make:
        dis_source = dis_search_text(dis_text)
        make = first_match(r'(?:Manufacturer(?:\s+name)?|Make\s+of\s+(?:PT|potential\s+transformer))\s*[:=\-]?\s*([A-Z][A-Z0-9.&/\-]*(?:\s*/\s*[A-Z][A-Z0-9.&/\-]*)?)', dis_source)
        if not make:
            make = first_match(r'\b([A-Z][A-Z0-9.&/\-]*(?:\s*/\s*[A-Z][A-Z0-9.&/\-]*)?)\s+MAKE\b', dis_source)
        make = re.sub(r'\s*/\s*', '/', make)
    # Preserve transformer type from MSLD when it is present.
    transformer_type = first_match(
        r'(POTENTIAL\s+TRANSFORMERS?\s+IN\s+.+?(?:DRAWOUT\s+TYPE|TYPE))',
        source
    )
    if not transformer_type:
        transformer_type = 'POTENTIAL TRANSFORMERS IN CAST RESIN EPOXY CASE (DRAWOUT TYPE)'
    transformer_type = clean_value(transformer_type).upper()
    cores = extract_pt_core_lines(pt_text) or extract_pt_core_lines(msld_text)
    core_count = len(cores) or 3
    core_word = {1: 'SINGLE', 2: 'TWO', 3: 'THREE'}.get(core_count, str(core_count))
    frequency = extract_frequency(dis_text)
    bil = extract_bil(dis_text)
    if bil:
        bil = bil.upper()
    panel = extract_pt_panel_suitability(msld_text, dis_text)
    lines = [
        f'PT {rated_voltage}' if rated_voltage else 'PT',
        f'{make.upper()} MAKE' if make else 'MAKE',
        transformer_type,
        '- SINGLE PHASE WITH IN BUILT PRIMARY FUSE OF 3.15A',
        f'{core_word} CORE PT, AS PER IS/IEC STANDARD',
        'AS GIVEN BELOW:-',
    ]
    for index, value in enumerate(cores, 1):
        lines.append(f'- CORE {index}: {value}')
    lines.extend([
        f'- RATED FREQUENCY: {frequency}' if frequency else '- RATED FREQUENCY:',
        '- VF: 1.2COUNT/1.9 FOR 8 HOURS',
        f'- BIL: {bil}' if bil else '- BIL:',
        'INSULATION CLASS:- B',
        f'SUITABLE FOR {panel}' if panel else 'SUITABLE FOR',
    ])
    return lines

def ct_spec(ct_text, dis_text):
    ct_code=generate_ct_code(ct_text)
    ct_make=extract_annexure_make(dis_text, 'CT') or extract_ct_make(dis_text)
    # Retain the legacy default only if Annexure 1 and the DIS text expose no CT make.
    if not ct_make:
        ct_make = ''
    ct_description=build_ct_description(ct_text)
    rated_voltage=extract_rated_voltage(dis_text)
    frequency=extract_frequency(dis_text)
    stc=first_match(r'(?:SHORT\s+TIME\s+CURRENT|STC)\s*:?\s*(.*?)(?=\s+CTR\s*:|\s+CORE\s*\d+\s*:|\s+STC\s*:|\s*$)',ct_text or '')
    bil=extract_bil(dis_text)
    cores=extract_ct_core_lines(ct_text)
    panel=extract_panel_suitability(dis_text)
    lines=[
        ct_code or 'CT',
        f'{ct_make.upper()} MAKE' if ct_make else '',
        ct_description,
        '- SINGLE PHASE, AS PER IS/IEC STANDARD',
        f'- RATED VOLTAGE: {rated_voltage}' if rated_voltage else '- RATED VOLTAGE:',
        f'- RATED FREQUENCY: {frequency}' if frequency else '- RATED FREQUENCY:',
        f'- SHORT TIME CURRENT: {stc}' if stc else '- SHORT TIME CURRENT:',
        f'- BIL: {bil}' if bil else '- BIL:',
        'INSULATION CLASS:-B',
        'AS GIVEN BELOW:-',
        build_ctr_line(ct_text)
    ]
    for i,value in enumerate(cores,1):
        lines.append(f'- CORE {i}: {value}')
    lines.extend([
        f'SUITABLE FOR {panel}' if panel else 'SUITABLE FOR',
        'CT SECONDARY TERMINAL ON P2 SIDE'
    ])
    return lines




# Cortex-derived VAJH13 catalogue. Values follow the "Relay details VAJx13"
# sheet: replacement model, auxiliary voltage, contact combination and burden.
VAJH13_MODELS = [
    ("VAJH13YF0250ABA", "30V DC", "4 N/O H/R", "LOW"),
    ("VAJH13YF0250BBA", "30V DC", "4 N/O H/R", "HIGH"),
    ("VAJH13YF0251ABA", "30V DC", "3 N/O 1 N/C H/R", "LOW"),
    ("VAJH13YF0251BBA", "30V DC", "3 N/O 1 N/C H/R", "HIGH"),
    ("VAJH13YF0252ABA", "30V DC", "2 N/O 2 N/C H/R", "LOW"),
    ("VAJH13YF0252BBA", "30V DC", "2 N/O 2 N/C H/R", "HIGH"),
    ("VAJH13YF0501ABA", "48 / 50V DC", "3 N/O 1 N/C H/R", "LOW"),
    ("VAJH13YF0501BBA", "48 / 50V DC", "3 N/O 1 N/C H/R", "HIGH"),
    ("VAJH13YF0502ABA", "48 / 50V DC", "2 N/O 2 N/C H/R", "LOW"),
    ("VAJH13YF0502BBA", "48 / 50V DC", "2 N/O 2 N/C H/R", "HIGH"),
    ("VAJH13YF0750ABA", "110 / 125 V DC", "4 N/O H/R", "LOW"),
    ("VAJH13YF0750BBA", "110 / 125 V DC", "4 N/O H/R", "HIGH"),
    ("VAJH13YF0751ABA", "110 / 125 V DC", "3 N/O 1 N/C H/R", "LOW"),
    ("VAJH13YF0751BBA", "110 / 125 V DC", "3 N/O 1 N/C H/R", "HIGH"),
    ("VAJH13YF0752ABA", "110 / 125 V DC", "2 N/O 2 N/C H/R", "LOW"),
    ("VAJH13YF0752BBA", "110 / 125 V DC", "2 N/O 2 N/C H/R", "HIGH"),
    ("VAJH13YF1000ABA", "220 / 250V DC", "4 N/O H/R", "LOW"),
    ("VAJH13YF1000BBA", "220 / 250V DC", "4 N/O H/R", "HIGH"),
    ("VAJH13YF1001ABA", "220 / 250V DC", "3 N/O 1 N/C H/R", "LOW"),
    ("VAJH13YF1001BBA", "220 / 250V DC", "3 N/O 1 N/C H/R", "HIGH"),
    ("VAJH13YF1002ABA", "220 / 250V DC", "2 N/O 2 N/C H/R", "LOW"),
    ("VAJH13YF1002BBA", "220 / 250V DC", "2 N/O 2 N/C H/R", "HIGH"),
    ("VAJH13YF0000BBA", "24 V DC", "4 N/O H/R", "HIGH"),
    ("VAJH13YF0001BBA", "24 V DC", "3 N/O 1 N/C H/R", "HIGH"),
    ("VAJH13YF0002BBA", "24 V DC", "2 N/O 2 N/C H/R", "HIGH"),
    ("VAJH13YF6001ABA", "220-250V AC", "3 N/O 1 N/C H/R", "LOW"),
    ("VAJH13YF6002ABA", "220-250V AC", "2 N/O 2 N/C H/R", "LOW"),
]

def vajh13_spec(record):
    """Build the approved VAJH13 BOM text and keep MSLD discrepancies separate."""
    source = clean_value(" ".join(str(record.get(k) or "") for k in ("designation", "description", "details")))
    upper = source.upper()
    if not re.search(r"\bVAJH13\b|\bMASTER\s+TRIP\s+RELAY\b", upper):
        return None, []

    warnings = []
    model_match = re.search(r"\bVAJH13YF[A-Z0-9]+\b", upper)
    source_model = model_match.group(0) if model_match else ""
    voltage_match = re.search(r"\b(?:AUX(?:ILIARY)?\.?\s*(?:SUPPLY|VOLTAGE)?|CONTROL\s+VOLTAGE)\s*:?\s*(\d+(?:\s*[-/]\s*\d+)?\s*V\s*(?:DC|AC))\b|\b(24|30|48|50|110|125|220|230|240|250)\s*V\s*(DC|AC)\b", upper)
    source_voltage = ""
    if voltage_match:
        source_voltage = clean_value(voltage_match.group(1) or (voltage_match.group(2) + "V " + voltage_match.group(3))).upper()
    contact_match = re.search(r"\b(4\s*N/?O|3\s*N/?O\s*\+?\s*1\s*N/?C|2\s*N/?O\s*\+?\s*2\s*N/?C|1\s*N/?O\s*\+?\s*3\s*N/?C)\b(?:\s*(?:H\s*/\s*R|S\s*/\s*R))?", upper)
    source_contacts = ""
    if contact_match:
        raw = re.sub(r"\s+", " ", contact_match.group(0).upper()).strip()
        raw = re.sub(r"\s*\+\s*", " ", raw)
        raw = re.sub(r"\bNO\b", "N/O", raw)
        raw = re.sub(r"\bNC\b", "N/C", raw)
        if "H/R" not in raw and re.search(r"\bH\s*/\s*R\b", upper):
            raw += " H/R"
        source_contacts = raw
    burden_match = re.search(r"\b(HIGH|LOW)\s*(?:BURDEN)?\b", upper)
    source_burden = next((group for group in burden_match.groups() if group), "") if burden_match else ""

    def voltage_options(value):
        """Return explicit voltage values and AC/DC type from a single value or family."""
        normalized = re.sub(r"\s+", " ", (value or "").upper()).strip()
        match = re.search(r"\b(\d{2,3})(?:\s*/\s*(\d{2,3}))?\s*V\s*(DC|AC)\b", normalized)
        if not match:
            return set()
        voltage_type = match.group(3)
        options = {(match.group(1), voltage_type)}
        if match.group(2):
            options.add((match.group(2), voltage_type))
        return options

    def voltage_matches_cortex(source_value, cortex_value):
        source_options = voltage_options(source_value)
        cortex_options = voltage_options(cortex_value)
        return bool(source_options and cortex_options and source_options.issubset(cortex_options))

    def norm_contacts(value):
        value = re.sub(r"\s+", "", value.upper())
        value = value.replace("NO", "N/O").replace("NC", "N/C").replace("+", "")
        value = value.replace("H/R", "")
        return value

    selected = None
    if source_model:
        selected = next((item for item in VAJH13_MODELS if item[0] == source_model), None)
        if not selected:
            warnings.append("MLFB " + source_model + " is not present in the supplied VAJH13 Cortex mapping.")
    else:
        candidates = VAJH13_MODELS
        if source_voltage:
            sv = norm_voltage(source_voltage)
            source_vm = re.search(r"(\d+)\s*V?\s*(DC|AC)", sv)
            def same_aux_voltage(value):
                candidate_vm = re.search(r"(\d+)\s*V?\s*(DC|AC)", norm_voltage(value))
                return bool(source_vm and candidate_vm and source_vm.group(1) == candidate_vm.group(1) and source_vm.group(2) == candidate_vm.group(2))
            candidates = [item for item in candidates if same_aux_voltage(item[1])]
        if source_contacts:
            sc = norm_contacts(source_contacts)
            candidates = [item for item in candidates if norm_contacts(item[2]).startswith(sc.replace("H/R", "")) or sc.startswith(norm_contacts(item[2]).replace("H/R", ""))]
        if source_burden:
            candidates = [item for item in candidates if item[3] == source_burden]
        # The agreed VAJH13 BOM variant is the high-burden, flag-equipped
        # YF 1/4 non-drawout model. Select it only when the MSLD confirms the
        # VAJH13 type, 110/125 V DC family and 3 N/O + 1 N/C H/R contacts.
        target = next((item for item in VAJH13_MODELS if item[0] == "VAJH13YF0751BBA"), None)
        if (target and source_voltage and source_vm and source_vm.group(1) in {"110", "125"}
                and source_vm.group(2) == "DC" and source_contacts
                and norm_contacts(source_contacts) == norm_contacts(target[2])):
            selected = target
        elif len(candidates) == 1:
            selected = candidates[0]
        elif len(candidates) > 1:
            warnings.append("VAJH13 MLFB is ambiguous in the Cortex mapping; MSLD must confirm burden or exact MLFB.")
        else:
            warnings.append("No VAJH13 MLFB matches the MSLD auxiliary voltage/contact combination in the Cortex mapping.")

    if source_voltage and selected and not voltage_matches_cortex(source_voltage, selected[1]):
        warnings.append("Auxiliary voltage mismatch: MSLD specifies " + source_voltage + "; Cortex model " + selected[0] + " specifies " + selected[1] + ".")
    if source_contacts and selected and norm_contacts(source_contacts) != norm_contacts(selected[2]):
        warnings.append("Contact combination mismatch: MSLD specifies " + source_contacts + "; Cortex model " + selected[0] + " specifies " + selected[2] + ".")
    if source_burden and selected and source_burden != selected[3]:
        warnings.append("Burden mismatch: MSLD specifies " + source_burden + "; Cortex model " + selected[0] + " specifies " + selected[3] + ".")

    # Retain the exact agreed BOM layout. If no unique MLFB can be verified,
    # keep the supplied type code rather than inventing a model number.
    burden = selected[3] if selected else (source_burden or "[NOT CONFIRMED]")
    # Cortex is the source of truth for the BOM AUX. VOLTAGE field.
    # For MSLD 110V DC or 125V DC, retain the full Cortex family label.
    aux_voltage = selected[1] if selected else (source_voltage or "[NOT CONFIRMED]")
    contacts = selected[2] if selected else (source_contacts or "[NOT CONFIRMED]")
    model_no = selected[0] if selected else (source_model or "[NOT CONFIRMED]")
    protection = first_match(r"\[\s*(86)\s*\]", upper) or ("86" if re.search(r"\b86\b", upper) else "[NOT CONFIRMED]")
    lines = [
        "GE MAKE",
        "1P HIGH SPEED TRIP RELAY",
        "BURDEN : " + burden,
        "FLUSH MTG, NON DROWOUT, CASE : 1/4 NV",
        "AUX. VOLTAGE : " + aux_voltage,
        "CONTACT COMBINATION : " + ("3NO+1NC HR" if "3NO1NC" in re.sub(r"[^A-Z0-9]", "", contacts.upper()) else "2NO+2NC HR" if "2NO2NC" in re.sub(r"[^A-Z0-9]", "", contacts.upper()) else "4NO HR" if "4NO" in re.sub(r"[^A-Z0-9]", "", contacts.upper()) else re.sub(r"\s+", "", contacts.replace("N/O", "NO").replace("N/C", "NC")).replace("H/R", " HR")) ,
        "WITH FLAG",
        "TYPE : VAJH13",
        "PROT.: " + protection,
        "MODEL.NO: " + model_no
    ]
    if not source_voltage:
        warnings.append("Auxiliary/control voltage is not specified clearly in the MSLD.")
    if not source_contacts:
        warnings.append("Contact combination is not specified clearly in the MSLD.")
    if not protection or protection == "[NOT CONFIRMED]":
        warnings.append("Protection function is not confirmed in the MSLD.")
    return lines, warnings


def pt_damping_resistor_spec(record, records=None):
    """Return the approved PT damping-resistor BOM text and MSLD mismatch warnings."""
    source = clean_value(' '.join(str(record.get(k) or '') for k in ('designation', 'description', 'details')))
    # PT/open-delta context is often printed on the PT row, not repeated on
    # the separate R11 damping-resistor row. Use same-page PT data as context.
    related_pt_text = ' '.join(
        clean_value(' '.join(str(item.get(k) or '') for k in ('designation', 'description', 'details')))
        for item in (records or [])
        if item.get('master_code') == 'PT'
        and item.get('source_page') == record.get('source_page')
    )
    application_context = source + ' ' + related_pt_text
    resistance_match = re.search(r'(?<![A-Z0-9])(\d+(?:\.\d+)?)\s*(?:OHMS?|Ω)(?![A-Z])', source, re.I)
    power_match = re.search(r'(?<![A-Z0-9])(\d+(?:\.\d+)?)\s*(?:WATTS?|W)\b', source, re.I)
    warnings = []
    if not resistance_match:
        warnings.append('Resistance value is missing or unreadable in the MSLD; required value is 75 Ohms.')
    elif float(resistance_match.group(1)) != 75:
        warnings.append('Resistance mismatch: MSLD specifies ' + resistance_match.group(1) + ' Ohms; required value is 75 Ohms.')
    if not power_match:
        warnings.append('Power rating is missing or unreadable in the MSLD; required value is 200 Watts.')
    elif float(power_match.group(1)) != 200:
        warnings.append('Power rating mismatch: MSLD specifies ' + power_match.group(1) + ' Watts; required value is 200 Watts.')
    if not re.search(r'PT|POTENTIAL\s+TRANSFORMER|OPEN\s+DELTA|\bOD\b', application_context, re.I):
        warnings.append('PT open-delta application is not confirmed in the MSLD.')
    return [
        'PT DAMPING RESISTOR',
        'DAMPING RESISTOR SUITABLE FOR PT OPEN DELTA CORE',
        'RATING: 75 Ohms, 200 Watts.'
    ], warnings

def em6400ng_spec(record, records):
    """Build EM6400NG BOM text, preferring values printed on the meter row."""
    meter_text = clean_value(' '.join(str(record.get(k) or '') for k in ('designation', 'description', 'details')))
    page = record.get('source_page')
    related = [item for item in records if page is not None and item.get('source_page') == page]
    ct_record = next((item for item in related if item.get('master_code') == 'CT'), None)
    pt_record = next((item for item in related if item.get('master_code') == 'PT'), None)
    ct_text = clean_value(' '.join(str(ct_record.get(k) or '') for k in ('designation', 'description', 'details'))) if ct_record else ''
    pt_text = clean_value(' '.join(str(pt_record.get(k) or '') for k in ('designation', 'description', 'details'))) if pt_record else ''

    # The meter row is the primary source: keep the complete ratio tokens,
    # including transformer connection notation such as /RT3/.
    accuracy = first_match(r'\b(?:ACC(?:URACY)?\.?\s*CLASS|CLASS\s*OF\s*ACCURACY|CLASS)\s*[:=\-]?\s*(\d+(?:\.\d+)?\s*[A-Z]?)', meter_text)
    ct_ratio = first_match(r'\b(?:CTR|CT\s*RATIO)\s*[:=]?\s*([^,;\n]+)', meter_text)
    if not ct_ratio:
        ct_ratio = first_match(r'\b(?:CTR|CT\s*RATIO)\s*[:=]?\s*([^,;\n]+)', ct_text)
    ct_ratio = clean_value(ct_ratio).rstrip(' .') if ct_ratio else ''
    secondary_match = re.search(r'/\s*(\d+(?:\.\d+)?)\s*A?\b', ct_ratio, re.I)
    ct_secondary = secondary_match.group(1) + 'A' if secondary_match else ''

    pt_ratio = first_match(r'\b(?:PTR|PT\s*RATIO|POTENTIAL\s+TRANSFORMER\s+RATIO)\s*[:=]?\s*([^,;\n]+)', meter_text)
    if not pt_ratio:
        pt_ratio = first_match(r'\b(?:PTR|PT\s*RATIO|POTENTIAL\s+TRANSFORMER\s+RATIO)\s*[:=]?\s*([^,;\n]+)', pt_text)
    if not pt_ratio:
        pt_ratio = first_match(r'\bRATIO\s*[:=]?\s*([^,;\n]+)', pt_text)
    pt_ratio = clean_value(pt_ratio).rstrip(' .') if pt_ratio else ''

    spec_lines = [
        'EM6400NG',
        'SCHNEIDER ELECTRIC CONZERV MAKE',
        'DIGITAL MF METER',
        '3 PHASE,4 WIRE,50HZ SYSTEM,',
        f'ACC CLASS : {accuracy}',
        'FLUSH MOUNTING,SIZE : 96x96 SQMM',
        f'CT SEC : {ct_secondary}, CT RATIO : {ct_ratio}',
        f'PT RATIO : {pt_ratio}',
        'AUX SUPPLY : 110V DC',
        'WITH MODBUS RS 485 PORT'
    ]
    missing = []
    if not accuracy:
        missing.append('accuracy class is missing or unreadable in the meter row')
    if not ct_ratio or not ct_secondary:
        missing.append('CT ratio/secondary is missing or unreadable in the meter row and corresponding CT row')
    if not pt_ratio:
        missing.append('PT ratio is missing or unreadable in the meter row and corresponding PT row')
    return spec_lines, missing

def pt_primary_fuse_designation(record):
    """Return the F-designation range for a PT's built-in primary-fuse row."""
    designation = clean_value(str(record.get('designation') or '')).upper().replace(' ', '')
    source = clean_value(' '.join([
        str(record.get('description') or ''),
        str(record.get('details') or ''),
    ])).upper()
    if not re.fullmatch(r'F\d+-F\d+', designation):
        return None
    if not re.search(r'PRIMARY\s+FUSE|IN[- ]?BUILT', source):
        return None
    return designation


def build_rows(feeder_info,records,msld_text='',dis_text=''):
 feeder_name=feeder_info[0].get('designation') or feeder_info[0].get('name','') if feeder_info else ''; feeder_qty=feeder_info[0].get('quantity','1') if feeder_info else '1'; feeder_match=re.search(r'\d+',str(feeder_qty)); feeder_qty_num=int(feeder_match.group()) if feeder_match else 1; rows=[]
 # Keep the PT and its built-in primary-fuse description as separate BOM rows.
 # The fuse row retains its own F-designation; the PT row retains its T-designation.
 for i,r in enumerate(records,1):
  # Normalize Control Link rows from the material/type text before choosing
  # the BOM description branch. Some MSLD extractions retain the raw
  # designation/type text and bypass the master classifier.
  control_source = re.sub(r'[^A-Z0-9]', '', ' '.join(str(r.get(k) or '') for k in ('description','details','designation')).upper())
  if '3NW32NNSL' in control_source or 'CONTROLLINK' in control_source:
   r = dict(r)
   r['master_code'] = 'CONTROL_LINK'
  elif '3NW32NNSF' in control_source or 'CONTROLFUSEBASE' in control_source:
   r = dict(r)
   r['master_code'] = 'CONTROL_FUSE_BASE'
  relay_analysis = None
  meter_validation = None
  vcb_validation = None
  if r['master_code']=='CT':
   ct_row_text = ' '.join([r.get('designation',''), r.get('description',''), r.get('details','')])
   spec_lines=ct_spec(ct_row_text,dis_text)
   spec='\n'.join(spec_lines)
   editable_fields=['INSULATION CLASS:-B','CT SECONDARY TERMINAL ON P2 SIDE']
   # Core count changes the position of the SUITABLE FOR line. Resolve
   # editable positions from their actual labels so 3-core CTs never mark
   # the SUITABLE FOR row as editable by mistake.
   editable_indices = []
   for editable_label in editable_fields:
    target = editable_label.split(' ON P2 SIDE')[0]
    line_index = next((idx for idx, line in enumerate(spec_lines)
                       if target in line), None)
    if line_index is not None:
     editable_indices.append(line_index)
  elif r['master_code']=='PT':
   pt_row_text = ' '.join([r.get('designation',''), r.get('description',''), r.get('details','')])
   spec_lines=pt_spec(pt_row_text, msld_text, dis_text)
   spec='\n'.join(spec_lines)
   editable_fields=['INSULATION CLASS:- B']
   editable_indices=[12]
  elif r['master_code']=='VCB':
   spec_lines, vcb_validation = vcb_spec(r, dis_text, msld_text)
   spec='\n'.join(spec_lines)
   editable_fields=[]
   editable_indices=[]
  elif r['master_code']=='S16_AUX_SWITCH':
   spec_lines=[
    'B3NZ331',
    'SIEMENS MAKE',
    'AUX SWITCH FOR 3AH CIRCUIT',
    'BREAKER TEST/SERVICE POSITION',
    '4NO AT EACH TEST & SERVICE POSITION'
   ]
   spec='\n'.join(spec_lines)
   editable_fields=[]
   editable_indices=[]
  elif r['master_code']=='CONTROL_FUSE_BASE':
   base_item = load_control_link_master('CONTROL-FUSE-BASE-3NW-32NNSF')
   base_description = (base_item or {}).get('discription', '')
   spec_lines = [part.strip() for part in base_description.split(',') if part.strip()]
   spec = '\n'.join(spec_lines)
   editable_fields=[]
   editable_indices=[]
  elif r['master_code']=='CONTROL_LINK':
   master_item = load_control_link_master('CONTROL-LINK-3NW-32NNSL')
   spec = (master_item or {}).get('discription', '')
   spec_lines = [part.strip() for part in spec.split(',') if part.strip()]
   editable_fields=[]
   editable_indices=[]
  elif r['master_code']=='MCB':
   mcb_item = mcb_master_match(r.get('description',''), r.get('details',''), r.get('designation',''))
   if mcb_item:
    spec, spec_lines = mcb_spec(mcb_item)
   else:
    spec = r['description']+((' | '+r['details']) if r['details'] else '')
    spec_lines = [clean(spec)] if spec else []
   editable_fields=[]
   editable_indices=[]
  elif r['master_code']=='EM6400NG':
    spec_lines, meter_missing = em6400ng_spec(r, records)
    spec = '\n'.join(spec_lines)
    meter_validation = {'warnings': meter_missing}
    editable_fields=[]
    editable_indices=[]
  elif r['master_code']=='7SJ6611':
   # Relay validation must use only this equipment row. Passing the full MSLD
   # could borrow an MLFB from another section/row and create a relay BOM line
   # that is not actually specified for this designation.
   relay_source = ' '.join([r.get('designation',''), r.get('description',''), r.get('details','')])
   relay_analysis = analyze_7sj66(relay_source)
   # Never drop a detected relay row just because its MLFB is missing or
   # its chart-coded options conflict with the MSLD details. Preserve the
   # source text in the BOM so engineering details are not lost, and attach
   # validation feedback for the UI warning list.
   source_relay_text = clean_value(' | '.join(
    part for part in [r.get('description',''), r.get('details','')] if part
   ))
   relay_validation = relay_analysis.get('validation') or {}
   has_mlfb = bool(relay_analysis.get('fields', {}).get('mlfb', {}).get('value'))
   # Only validate/report a missing MLFB when this MSLD row explicitly
   # identifies a 7SJ66 relay. A generic numerical-relay label is not enough.
   has_7sj66_evidence = bool(re.search(r'\b7SJ66\w*\b', relay_source, re.IGNORECASE))
   if not has_mlfb and not has_7sj66_evidence:
    continue
   # When an MLFB is extracted, always show the structured description.
   # Validation errors/warnings stay separate and do not replace BOM details.
   if has_mlfb:
    spec = relay_analysis['description']
    spec_lines = spec.splitlines()
   else:
    # Keep source text only when the MSLD actually identifies a relay.
    spec = source_relay_text
    spec_lines = [spec] if spec else []
   editable_fields=[]
   editable_indices=[]
  elif r['master_code']=='E2_LED':
   spec_lines=[
    'LED',
    'PHILIPS/REPUTED MAKE LED 5W- TUBE FIXTURE',
    'SUITABLE FOR 240V AC, 50Hz AC SUPPLY',
    'SUITABLE FOR SURFACE MOUNTING'
   ]
   spec='\n'.join(spec_lines)
   editable_fields=[]
   editable_indices=[]
  elif r['master_code']=='LED':
   h789 = led_specs_for_h7_h8_h9(r.get('description',''), r.get('details',''), r.get('designation',''))
   if h789:
    # Split the source quantity across the colour-specific H7/H8/H9 rows.
    # Example: source qty 3 and three colours -> 1 each, total remains 3 per feeder.
    group_qty = int(r.get('quantity') or 1)
    split_count = len(h789)
    base_qty, remainder = divmod(group_qty, split_count)
    for part_index, part in enumerate(h789):
     part_lines=part['specification_lines']
     part_qty = base_qty + (1 if part_index < remainder else 0)
     rows.append({'sr':len(rows)+1,'specification':'\n'.join(part_lines),'specification_lines':part_lines,'editable_fields':[],'editable_indices':[],'designation':part['designation'],'feeder_name':feeder_name,'feeder_qty':feeder_qty,'total':part_qty*feeder_qty_num,'eqpt_qty':part_qty,'mpd':'','amd':'','master_code':'LED'})
    continue
   spec_lines, led_item = led_spec(r.get('description',''), r.get('details',''), r.get('designation',''))
   spec='\n'.join(spec_lines) if spec_lines else r['description']+((' | '+r['details']) if r['details'] else '')
   spec_lines=spec_lines or ([clean(spec)] if spec else [])
   editable_fields=[]
   editable_indices=[]
  else:
   spec=r['description']+((' | '+r['details']) if r['details'] else '')
   spec_lines=[clean(spec)] if spec else []
   editable_fields=[]
   editable_indices=[]
  rows.append({'sr':len(rows)+1,'specification':spec,'specification_lines':spec_lines,'editable_fields':editable_fields,'editable_indices':editable_indices,'designation':r['designation'],'feeder_name':feeder_name,'feeder_qty':feeder_qty,'total':(r['quantity'] or 1)*feeder_qty_num,'eqpt_qty':(r['quantity'] or 1),'mpd':'','amd':'','master_code':r['master_code'],'relay_validation':relay_analysis.get('validation') if relay_analysis else None,'vcb_validation':vcb_validation,'meter_validation':meter_validation if r['master_code']=='EM6400NG' else None,'auxiliary_relay_validation':auxiliary_relay_validation if r['master_code']=='VAJH13' else None,'damping_resistor_validation':damping_resistor_validation if r['master_code']=='PT_DAMPING_RESISTOR' else None})
  if r['master_code']=='CONTROL_LINK':
   link_qty = (r['quantity'] or 1)
   base_designation = str(r.get('designation','')) + 'A'
   # If the base is already present as an extracted equipment row, its own
   # CONTROL_FUSE_BASE branch above formats it; do not append a duplicate.
   base_already_present = any(
    ('3NW32NNSF' in re.sub(r'[^A-Z0-9]', '', ' '.join(str(item.get(k) or '') for k in ('description','details','designation')).upper())
     or 'CONTROLFUSEBASE' in re.sub(r'[^A-Z0-9]', '', ' '.join(str(item.get(k) or '') for k in ('description','details','designation')).upper()))
    and str(item.get('designation','')).strip().upper() == base_designation.upper()
    for item in records
   )
   base_item = None if base_already_present else load_control_link_master('CONTROL-FUSE-BASE-3NW-32NNSF')
   base_spec = (base_item or {}).get('discription', '')
   base_lines = [part.strip() for part in base_spec.split(',') if part.strip()]
   if base_lines:
    rows.append({'sr':len(rows)+1,'specification':'\n'.join(base_lines),'specification_lines':base_lines,'editable_fields':[],'editable_indices':[],'designation':base_designation,'feeder_name':feeder_name,'feeder_qty':feeder_qty,'total':link_qty*feeder_qty_num,'eqpt_qty':link_qty,'mpd':'','amd':'','master_code':'CONTROL_FUSE_BASE'})
  if r['master_code']=='MCB':
   mcb_source_text = clean_value(' '.join([r.get('description',''), r.get('details','')])).upper()
   has_aux_1no_1nc = bool(re.search(r'\b1\s*NO\s*\+\s*1\s*NC\b', mcb_source_text) and re.search(r'\bAUX(?:ILIARY)?\b|\bCONTACTS?\b', mcb_source_text))
   if has_aux_1no_1nc:
    mcb_qty = (r['quantity'] or 1)
    addon_item = load_mcb_addon_master()
    if addon_item and addon_item.get('discription'):
     addon_spec = addon_item['discription']
     addon_lines = [part.strip() for part in addon_spec.split(',') if part.strip()]
     rows.append({'sr':len(rows)+1,'specification':'\n'.join(addon_lines),'specification_lines':addon_lines,'editable_fields':[],'editable_indices':[],'designation':str(r.get('designation',''))+'A','feeder_name':feeder_name,'feeder_qty':feeder_qty,'total':mcb_qty*feeder_qty_num,'eqpt_qty':mcb_qty,'mpd':'','amd':'','master_code':'MCB ADD-ON'})
 return rows,feeder_name,feeder_qty

def extract_from_files(msld_bytes,msld_name,dis_bytes,dis_name,client,sales,drawing,esd,wo,prep,voltage):
 msld_text=pdf_text(msld_bytes,msld_name); dis_text=pdf_text(dis_bytes,dis_name); same=bool(msld_text.strip()) and re.sub(r'\s+','',msld_text)==re.sub(r'\s+','',dis_text); feeder_info,records,warnings=extract_fixed_table(msld_bytes,msld_name)
 if same:warnings.append('The same PDF was supplied in both MSLD and DIS slots. It was processed once; DIS was used as the validation copy.')
 elif dis_text.strip():warnings.append('DIS supplied: used as the fixed specification source; MSLD remains the primary project-data source.')
 rows,feeder_name,feeder_qty=build_rows(feeder_info,records,msld_text,dis_text)
 for row in rows:
  damping_validation = row.get('damping_resistor_validation')
  if damping_validation:
   damping_label = 'PT damping resistor ' + str(row.get('designation', ''))
   warnings.extend([damping_label + ': ' + message for message in damping_validation.get('warnings', [])])
  auxiliary_validation = row.get('auxiliary_relay_validation')
  if auxiliary_validation:
   auxiliary_label = 'VAJH13 auxiliary trip relay ' + str(row.get('designation', ''))
   warnings.extend([auxiliary_label + ': ' + message for message in auxiliary_validation.get('warnings', [])])
  vcb_validation = row.get('vcb_validation')
  if vcb_validation:
   warnings.extend(['Q10 VCB ' + str(row.get('designation', '')) + ': ' + message for message in vcb_validation.get('warnings', [])])
  meter_validation = row.get('meter_validation')
  if meter_validation:
   meter_label = 'EM6400NG meter ' + str(row.get('designation', ''))
   warnings.extend([meter_label + ': ' + message for message in meter_validation.get('warnings', [])])
  auxiliary_validation = row.get('auxiliary_relay_validation')
  if auxiliary_validation:
   auxiliary_label = 'VAJH13 master trip relay ' + str(row.get('designation', ''))
   warnings.extend([auxiliary_label + ': ' + message for message in auxiliary_validation.get('warnings', [])])
  validation=row.get('relay_validation')
  if validation:
   relay_label='7SJ66 relay '+str(row.get('designation',''))
   warnings.extend([relay_label+': '+message for message in validation.get('errors',[])])
   warnings.extend([relay_label+': '+message for message in validation.get('warnings',[])])
 h=header(msld_text,dis_text,client,sales,drawing,esd,wo,prep,voltage); ph=extract_header_from_page(msld_bytes,msld_name)
 for key in ('client','sales_ref','drawing','wo'):
  if ph.get(key):h[key]=ph[key]
 if ph.get('esd'):h['esd']=ph['esd']
 source=msld_text+'\n'+dis_text; q=first(r'\bQty\.\s*:\s*(\d+x?)\b',source) or first(r'\bQTY\.\s*:\s*(\d+x?)\b',source) or ph.get('qty',''); v=norm_voltage(voltage) or first(r'\b(6\.6|11|33)\s*KV\b',source); description=f'{v}kV SWITCHBOARD' if v else 'SWITCHBOARD'
 if not rows:warnings.append('No equipment rows were confidently extracted from the fixed MSLD table.')
 unmatched=[r['designation'] for r in records if r['master_code']=='UNMATCHED']
 if unmatched:warnings.append('Master Data match not available for: '+', '.join(unmatched)+'. Source description was preserved; no engineering item was guessed.')
 unmatched_mcb=[r['designation'] for r in records if r['master_code']=='MCB' and not mcb_master_match(r.get('description',''),r.get('details',''),r.get('designation',''))]
 if unmatched_mcb:warnings.append('MCB specifications could not be fully verified against the master DISCRIPTION for: '+', '.join(unmatched_mcb)+'. Source text was preserved; verify before issuing the BOM.')
 return {'header':h,'document_no':'SI EA/CS/FR/EG/015','revision':'1.0','effective_date':'17/07/2026','created_by':'EA CS ENGG','description':description,'rows':rows,'feeder_name':feeder_name,'feeder_qty':feeder_qty,'qty':q,'warnings':warnings,'feeder_details':feeder_info,'same_input_file':same}


def extract_multiple_from_files(msld_files, dis_bytes, dis_name, client, sales, drawing, esd, wo, prep, voltage):
    """Build one BOM across many MSLDs, using one common DIS as the specification source."""
    dis_text = pdf_text(dis_bytes, dis_name)
    all_rows = []
    feeders = []
    warnings = []
    header_source = ""
    first_ph = {}
    qty_text = ""
    for idx, item in enumerate(msld_files):
        msld_bytes, msld_name = item
        msld_text = pdf_text(msld_bytes, msld_name)
        if idx == 0:
            header_source = msld_text
            first_ph = extract_header_from_page(msld_bytes, msld_name)
            source = msld_text + "\n" + dis_text
            qty_text = first(r'\bQty\.\s*:\s*(\d+x?)\b', source) or first(r'\bQTY\.\s*:\s*(\d+x?)\b', source) or first_ph.get('qty', '')
        feeder_info, records, file_warnings = extract_fixed_table(msld_bytes, msld_name)
        if not feeder_info:
            warnings.append(f"{msld_name}: feeder information could not be confidently extracted.")
            feeder_info = [{'name': os.path.splitext(os.path.basename(msld_name))[0], 'designation': os.path.splitext(os.path.basename(msld_name))[0], 'quantity': '1'}]
        feeder_name = feeder_info[0].get('designation') or feeder_info[0].get('name') or f"MSLD {idx + 1}"
        feeder_qty_text = feeder_info[0].get('quantity', '1')
        qty_match = re.search(r'\d+', str(feeder_qty_text))
        feeder_qty_num = int(qty_match.group()) if qty_match else 1
        feeders.append({'name': str(feeder_name), 'qty': str(feeder_qty_text), 'quantity': feeder_qty_num, 'source_file': msld_name})
        built, _, _ = build_rows(feeder_info, records, msld_text, dis_text)
        for row in built:
            auxiliary_validation = row.get('auxiliary_relay_validation')
            if auxiliary_validation:
                auxiliary_label = str(msld_name) + ': VAJH13 auxiliary trip relay ' + str(row.get('designation', ''))
                warnings.extend([auxiliary_label + ': ' + message for message in auxiliary_validation.get('warnings', [])])
            vcb_validation = row.get('vcb_validation')
            if vcb_validation:
                warnings.extend([str(msld_name) + ': Q10 VCB ' + str(row.get('designation', '')) + ': ' + message for message in vcb_validation.get('warnings', [])])
            meter_validation = row.get('meter_validation')
            if meter_validation:
                meter_label = str(msld_name) + ': EM6400NG meter ' + str(row.get('designation', ''))
                warnings.extend([meter_label + ': ' + message for message in meter_validation.get('warnings', [])])
            auxiliary_validation = row.get('auxiliary_relay_validation')
            if auxiliary_validation:
                auxiliary_label = str(msld_name) + ': VAJH13 master trip relay ' + str(row.get('designation', ''))
                warnings.extend([auxiliary_label + ': ' + message for message in auxiliary_validation.get('warnings', [])])
            validation = row.get('relay_validation')
            if validation:
                relay_label = str(msld_name) + ': 7SJ66 relay ' + str(row.get('designation', ''))
                warnings.extend([relay_label + ': ' + message for message in validation.get('errors', [])])
                warnings.extend([relay_label + ': ' + message for message in validation.get('warnings', [])])
        for row in built:
            row['_feeder_index'] = idx
            all_rows.append(row)
        warnings.extend([f"{msld_name}: {w}" for w in file_warnings])
        for record in records:
            if (record.get('master_code') == 'PLUG_SOCKET_SWITCH' and re.search(r'\b(?:1P|1 POLE|SINGLE POLE|SP)\b', ' '.join([str(record.get('description') or ''), str(record.get('details') or ''), str(record.get('designation') or '')]), re.I) and re.search(r'\bMCB\b|MINIATURE CIRCUIT BREAKER', ' '.join([str(record.get('description') or ''), str(record.get('details') or ''), str(record.get('designation') or '')]), re.I)):
                warnings.append(f"{msld_name}: Plug socket {record.get('designation') or ''}: 1P MCB is shown in the plug socket row. Please use a switched plug socket with built-in switch instead of a separate 1P MCB.")
        unmatched = [r['designation'] for r in records if r['master_code'] == 'UNMATCHED']
        if unmatched:
            warnings.append(f"{msld_name}: Master Data match not available for: " + ', '.join(unmatched) + ". Source description was preserved; no engineering item was guessed.")
        unmatched_mcb = [r['designation'] for r in records if r['master_code'] == 'MCB' and not mcb_master_match(r.get('description',''), r.get('details',''), r.get('designation',''))]
        if unmatched_mcb:
            warnings.append(f"{msld_name}: MCB specifications could not be fully verified against the master DISCRIPTION for: " + ', '.join(unmatched_mcb) + ". Source text was preserved; verify before issuing the BOM.")
    # Merge identical equipment only when its normalized specification and category match.
    merged = {}
    for row in all_rows:
        idx = row.pop('_feeder_index')
        spec_lines = row.get('specification_lines') or []
        identity_text = row.get('specification') or '\n'.join(spec_lines)
        key = (str(row.get('master_code', '')).strip().upper(), re.sub(r'\s+', ' ', identity_text).strip().casefold())
        if key not in merged:
            entry = dict(row)
            entry['sr'] = len(merged) + 1
            entry['feeder_quantities'] = [0] * len(feeders)
            entry['designation_by_feeder'] = [''] * len(feeders)
            entry['total'] = 0
            entry['eqpt_qty'] = 0
            merged[key] = entry
        entry = merged[key]
        qty = row.get('eqpt_qty')
        if qty is None:
            qty = 1
        try:
            qty = int(qty)
        except (ValueError, TypeError):
            qty = 1
        # Feeder columns show equipment quantity per typical panel. Total is
        # calculated only after merging, using that feeder's panel multiplier.
        entry['feeder_quantities'][idx] += qty
        designation = str(row.get('designation', '') or '').strip()
        if designation:
            existing = entry['designation_by_feeder'][idx]
            if isinstance(existing, list):
                if designation not in existing:
                    existing.append(designation)
            elif existing and existing != designation:
                entry['designation_by_feeder'][idx] = [existing, designation]
            else:
                entry['designation_by_feeder'][idx] = [designation]
        entry['eqpt_qty'] += qty
    rows = list(merged.values())
    for row in rows:
        quantities = row.get('feeder_quantities') or []
        per_feeder_quantities = []
        total = 0
        for i, feeder in enumerate(feeders):
            try:
                equipment_qty = int(quantities[i] or 0) if i < len(quantities) else 0
            except (TypeError, ValueError):
                equipment_qty = 0
            try:
                feeder_qty = int(feeder.get('quantity') or 1)
            except (TypeError, ValueError):
                feeder_qty = 1
            per_feeder_quantities.append(equipment_qty)
            total += equipment_qty * max(1, feeder_qty)
        row['feeder_quantities'] = per_feeder_quantities
        row['total'] = total
    def equipment_order(row):
        code = str(row.get('master_code', '')).strip().upper()
        spec = str(row.get('specification', '')).upper()
        text = ' '.join([code, spec, str(row.get('designation', '')).upper()])
        # Requested BOM order: CT, PT, indication lamps, MCBs/add-ons,
        # plug sockets, LED lamps, control-link bases, then VCB limit switches.
        if code == 'CT' or re.search(r'\bCURRENT TRANSFORMER\b|\bC\.T\.\b', text):
            rank = 0
        elif code in ('PT', 'VT') or re.search(r'\b(POTENTIAL|VOLTAGE) TRANSFORMER\b|\bP\.T\.\b', text):
            rank = 1
        elif code in ('INDICATION_LAMP', 'INDICATOR_LAMP', 'INDICATION LAMP') or 'INDICATION LAMP' in text:
            rank = 2
        elif code.startswith('MCB') or re.search(r'\bMCB\b', text):
            rank = 3
        elif code in ('PLUG_SOCKET_SWITCH', 'PLUG_SOCKET', 'PS') or 'PLUG SOCKET' in text:
            rank = 4
        elif code in ('LED', 'LAMP', 'E2_LED') or 'LED LAMP' in text or re.search(r'\bLED\b', text):
            rank = 5
        elif code in ('CONTROL_FUSE_BASE', 'CONTROL_LINK'):
            rank = 6
        elif code in ('VCB', 'CB', 'BREAKER') or 'CIRCUIT BREAKER' in text:
            rank = 7
        elif code in ('LIMIT_SWITCH', 'S16_AUX_SWITCH') or 'LIMIT SWITCH' in text:
            rank = 8
        else:
            rank = 9
        subrank = 0 if code == 'CONTROL_LINK' else 1 if code == 'CONTROL_FUSE_BASE' else 0
        return (rank, subrank, code, re.sub(r'\s+', ' ', str(row.get('specification', ''))).casefold(), str(row.get('designation', '')).casefold())
    rows.sort(key=equipment_order)
    for i, row in enumerate(rows, 1):
        row['sr'] = i
        designations = []
        for values in row.pop('designation_by_feeder', []):
            if isinstance(values, str):
                values = [values]
            for value in values or []:
                value = str(value).strip()
                if value and value not in designations:
                    designations.append(value)
        row['designation'] = ', '.join(designations)
        row['feeder_name'] = ''
        row['feeder_qty'] = ''
    h = header(header_source, dis_text, client, sales, drawing, esd, wo, prep, voltage)
    for key in ('client', 'sales_ref', 'drawing', 'wo'):
        if first_ph.get(key):
            h[key] = first_ph[key]
    if first_ph.get('esd'):
        h['esd'] = first_ph['esd']
    source = header_source + '\n' + dis_text
    v = norm_voltage(voltage) or first(r'\b(6\.6|11|33)\s*KV\b', source)
    description = f'{v}kV SWITCHBOARD' if v else 'SWITCHBOARD'
    if not rows:
        warnings.append('No equipment rows were confidently extracted from the supplied MSLD files.')
    if dis_text.strip():
        warnings.append('One common DIS supplied: used as the specification source for all MSLDs.')
    return {
        'header': h, 'document_no': 'SI EA/CS/FR/EG/015', 'revision': '1.0',
        'effective_date': '17/07/2026', 'created_by': 'EA CS ENGG',
        'description': description, 'rows': rows, 'feeders': feeders,
        'feeder_name': feeders[0]['name'] if feeders else '',
        'feeder_qty': feeders[0]['qty'] if feeders else '',
        'qty': qty_text, 'warnings': list(dict.fromkeys(warnings)),
        'feeder_details': feeders, 'same_input_file': False
    }

@app.post('/api/bom/check-customers')
async def check_msld_customers(
    mslds: list[UploadFile] = File(...),
    dis: UploadFile = File(...),
    client: str = Form(''),
    wo: str = Form(''),
    voltage: str = Form(''),
    user: str = Depends(current_user)
):
    """Check MSLD and common DIS header values before BOM generation."""
    results = []
    for upload in mslds:
        data = await upload.read()
        filename = upload.filename or 'MSLD.pdf'
        extracted = extract_header_from_page(data, filename)
        source_text = pdf_text(data, filename)
        customer = clean_value(extracted.get('client', '')).strip(' :-')
        if not customer:
            customer = first(r'Client\s*[:\-]*\s*([^\n]+)', source_text).strip(' :-') or first(r'Customer\s*Name\s*[:\-]*\s*([^\n]+)', source_text).strip(' :-')
        work_order = clean_value(extracted.get('wo', '')).strip(' :-')
        if not work_order:
            work_order = first(r'W\.?\s*O\.?\s*(?:No\.?)?\s*[:\-]\s*([^\n]+)', source_text).strip(' :-')
        voltage_match = first(r'\b(6\.6|11|33)\s*k?V\b', source_text)
        results.append({'filename': filename, 'customer': clean_value(customer), 'work_order': clean_value(work_order), 'voltage': clean_value(voltage_match)})
    dis_bytes = await dis.read()
    dis_name = dis.filename or 'DIS.pdf'
    dis_header = extract_header_from_page(dis_bytes, dis_name)
    dis_text = pdf_text(dis_bytes, dis_name)
    # DIS is a two-column footer table. Parse the Client/W.O. values from
    # words on the same visual row, stopping before the right-hand columns.
    dis_words = page_words(pdf_pages(dis_bytes, dis_name)[0]) if pdf_pages(dis_bytes, dis_name) else []
    def footer_value(label_pattern):
        # Read only words to the right of the requested label on the same visual row.
        labels = [
            w for w in dis_words
            if re.fullmatch(label_pattern, w['text'].strip(' :.-'), re.IGNORECASE)
        ]
        for label_word in labels:
            row = sorted(
                [w for w in dis_words if abs(w['y'] - label_word['y']) <= 8 and w['x'] > label_word['x']],
                key=lambda w: w['x']
            )
            picked = []
            for word in row:
                token = word['text'].strip(' :.-')
                if not token:
                    continue
                if re.match(
                    r'^(?:Sales|Project|W\.?O\.?|Item|Qty|Description|Sh\.?|Work|Details|Drg\.?|Date|Prep|Ckd|Client)\b',
                    token,
                    re.IGNORECASE
                ):
                    break
                picked.append(token)
            value = clean_value(' '.join(picked)).strip(' :-')
            if value:
                return value
        return ''

    dis_customer = footer_value(r'^Client$')
    dis_wo = footer_value(r'^(?:W\.?O\.?|Work\s*Order)$')
    if not dis_customer:
        dis_customer = clean_value(dis_header.get('client', '')).strip(' :-')
    if not dis_wo:
        dis_wo = clean_value(dis_header.get('wo', '')).strip(' :-')
    dis_voltage = first(r'\b(6\.6|11|33)\s*k?V\b', dis_text)
    def distinct(values):
        return list(dict.fromkeys(re.sub(r'\s+', ' ', str(v)).strip() for v in values if str(v or '').strip()))
    customers = distinct([x['customer'] for x in results])
    work_orders = distinct([x['work_order'] for x in results])
    voltages = distinct([x['voltage'] for x in results])
    mismatches = []
    if len({x.casefold() for x in customers}) > 1:
        mismatches.append('The uploaded MSLD PDFs contain different customer names.')
    if len({x.casefold() for x in work_orders}) > 1:
        mismatches.append('The uploaded MSLD PDFs contain different work order numbers.')
    if len({x.casefold() for x in voltages}) > 1:
        mismatches.append('The uploaded MSLD PDFs contain different voltage values.')
    entered = {'customer': clean_value(client), 'work_order': clean_value(wo), 'voltage': clean_value(voltage)}
    for label, values, dis_value, entered_value in [
        ('Customer name', customers, dis_customer, entered['customer']),
        ('Work order number', work_orders, dis_wo, entered['work_order']),
        ('Voltage', voltages, dis_voltage, entered['voltage'])
    ]:
        all_values = distinct(values + ([dis_value] if dis_value else []) + ([entered_value] if entered_value else []))
        if label == 'Voltage':
            all_values = distinct([norm_voltage(value) or value for value in all_values])
        if len({x.casefold() for x in all_values}) > 1:
            mismatches.append(f'{label} differs between entered details, MSLD and/or DIS.')

    # Do not silently proceed when a PDF header field could not be extracted.
    # The existing verification modal displays these messages and lets the user
    # review the extracted values before choosing whether to continue.
    for item in results:
        if not item['work_order']:
            mismatches.append(f"Work order number could not be read from MSLD: {item['filename']}.")
        if not item['voltage']:
            mismatches.append(f"Voltage could not be read from MSLD: {item['filename']}.")
    if not dis_wo:
        mismatches.append(f"Work order number could not be read from DIS: {dis_name}.")
    if not dis_voltage:
        mismatches.append(f"Voltage could not be read from DIS: {dis_name}.")
    return {
        'mslds': results, 'customers': customers, 'different_customers': len({x.casefold() for x in customers}) > 1,
        'unknown_customer_files': [x['filename'] for x in results if not x['customer']],
        'mismatches': list(dict.fromkeys(mismatches)),
        'dis': {'filename': dis_name, 'customer': dis_customer, 'work_order': dis_wo, 'voltage': clean_value(dis_voltage)}
    }

@app.post('/api/bom/preview-multiple')
async def preview_multiple(
    client: str = Form(''), sales_ref: str = Form(''), drawing: str = Form(''),
    esd: str = Form(''), wo: str = Form(''), prep_by: str = Form(''),
    voltage: str = Form(''), mslds: list[UploadFile] = File(...),
    dis: UploadFile = File(...), user: str = Depends(current_user)
):
    if not mslds:
        raise HTTPException(status_code=400, detail='Upload at least one MSLD PDF.')
    dis_name = dis.filename or 'DIS.pdf'
    dis_bytes = await dis.read()
    items = []
    filenames = []
    for upload in mslds:
        filename = upload.filename or 'MSLD.pdf'
        items.append((await upload.read(), filename))
        filenames.append(filename)
    result = extract_multiple_from_files(items, dis_bytes, dis_name, client, sales_ref, drawing, esd, wo, prep_by, voltage)
    import json
    with db() as conn:
        conn.execute(
            """INSERT INTO bom_history (username, customer_name, work_order_no, voltage, msld_files, dis_filename, row_count)
               VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)""",
            (user, str(client or result.get('header', {}).get('client', '')), str(wo or result.get('header', {}).get('wo', '')),
             str(voltage or ''), json.dumps(filenames), dis_name, len(result.get('rows') or []))
        )
        conn.commit()
    return result




class SiemensLookupRequest(BaseModel):
    mlfb: str


@app.post('/api/siemens/product-lookup')
def siemens_product_lookup(payload: SiemensLookupRequest, user: str = Depends(current_user)):
    """POC: live lookup of a supplied 7SJ66 MLFB on Siemens Industry Mall."""
    return lookup_siemens_product(payload.mlfb)

@app.get('/api/admin/bom-history')
def admin_bom_history(user: str = Depends(require_admin)):
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) AS total FROM bom_history").fetchone()['total']
        rows = conn.execute(
            """SELECT id, username, customer_name, work_order_no, voltage, msld_files,
                      dis_filename, row_count, created_at
               FROM bom_history ORDER BY created_at DESC LIMIT 500"""
        ).fetchall()
    return {'total_generated': total, 'history': rows}


# Master Data staging API. The approved CSV remains read-only and continues to
# be the sole source for deterministic BOM matching.
MASTER_DATA_FIELDS = ('ID','MATERIAL_CODE','CATEGORY','DISCRIPTION','MANUFACTURER','MODEL','UNIT','UNIT_PRICE')
MASTER_DATA_CSV = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'master_database.csv')

class MasterDataProposal(BaseModel):
    record: dict

class MasterDataReview(BaseModel):
    decision: str
    note: str = ''


def _clean_master_record(raw):
    record = {field: str(raw.get(field, '') or '').strip() for field in MASTER_DATA_FIELDS}
    if not record['DISCRIPTION']:
        record['DISCRIPTION'] = str(raw.get('DESCRIPTION', '') or '').strip()
    if not record['MATERIAL_CODE'] or not record['CATEGORY'] or not record['DISCRIPTION']:
        raise HTTPException(status_code=400, detail='Material Code, Category and Description are required.')
    if len(record['MATERIAL_CODE']) > 120 or len(record['DISCRIPTION']) > 2000:
        raise HTTPException(status_code=400, detail='Material Code or Description exceeds the allowed length.')
    return record


@app.get('/api/admin/master-data')
def admin_master_data(user: str = Depends(require_admin)):
    import csv
    records = []
    try:
        with open(MASTER_DATA_CSV, newline='', encoding='utf-8-sig') as source:
            for row in csv.DictReader(source):
                records.append({field: str(row.get(field, '') or '') for field in MASTER_DATA_FIELDS})
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail='Approved master_database.csv was not found.')
    return {'total': len(records), 'fields': list(MASTER_DATA_FIELDS), 'records': records}


@app.get('/api/admin/master-data/proposals')
def admin_master_data_proposals(status: str = 'all', user: str = Depends(require_admin)):
    if status not in ('all', 'pending', 'approved', 'rejected'):
        raise HTTPException(status_code=400, detail='Invalid proposal status.')
    with db() as conn:
        if status == 'all':
            rows = conn.execute("""SELECT id, record_data, status, submitted_by, created_at,
                updated_at, reviewed_by, reviewed_at, review_note
                FROM master_data_proposals ORDER BY created_at DESC LIMIT 500""").fetchall()
        else:
            rows = conn.execute("""SELECT id, record_data, status, submitted_by, created_at,
                updated_at, reviewed_by, reviewed_at, review_note
                FROM master_data_proposals WHERE status=%s ORDER BY created_at DESC LIMIT 500""", (status,)).fetchall()
    return {'proposals': rows}


@app.post('/api/admin/master-data/proposals')
def create_master_data_proposal(payload: MasterDataProposal, user: str = Depends(require_admin)):
    record = _clean_master_record(payload.record)
    with db() as conn:
        row = conn.execute("""INSERT INTO master_data_proposals(record_data, submitted_by)
            VALUES(%s::jsonb,%s) RETURNING id, record_data, status, submitted_by, created_at""",
            (json.dumps(record), user)).fetchone()
        conn.execute("INSERT INTO master_data_audit(proposal_id,action,actor,details) VALUES(%s,'created',%s,%s::jsonb)",
            (row['id'], user, json.dumps({'record': record})))
        conn.commit()
    return {'proposal': row}


@app.put('/api/admin/master-data/proposals/{proposal_id}')
def update_master_data_proposal(proposal_id: int, payload: MasterDataProposal, user: str = Depends(require_admin)):
    record = _clean_master_record(payload.record)
    with db() as conn:
        row = conn.execute("""UPDATE master_data_proposals SET record_data=%s::jsonb, updated_at=NOW()
            WHERE id=%s AND status='pending' RETURNING id""", (json.dumps(record), proposal_id)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail='Pending proposal not found; reviewed proposals cannot be edited.')
        conn.execute("INSERT INTO master_data_audit(proposal_id,action,actor,details) VALUES(%s,'updated',%s,%s::jsonb)",
            (proposal_id, user, json.dumps({'record': record})))
        conn.commit()
    return {'message': 'Proposal updated. It remains pending approval.'}


@app.post('/api/admin/master-data/proposals/{proposal_id}/review')
def review_master_data_proposal(proposal_id: int, payload: MasterDataReview, user: str = Depends(require_super_admin)):
    decision = payload.decision.strip().lower()
    if decision not in ('approved', 'rejected'):
        raise HTTPException(status_code=400, detail='Decision must be approved or rejected.')
    note = payload.note.strip()[:1000]
    with db() as conn:
        row = conn.execute("""UPDATE master_data_proposals SET status=%s, reviewed_by=%s,
            reviewed_at=NOW(), review_note=%s, updated_at=NOW()
            WHERE id=%s AND status='pending' RETURNING id, record_data, status, submitted_by,
            created_at, reviewed_by, reviewed_at, review_note""",
            (decision, user, note, proposal_id)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail='Pending proposal not found or already reviewed.')
        conn.execute("INSERT INTO master_data_audit(proposal_id,action,actor,details) VALUES(%s,%s,%s,%s::jsonb)",
            (proposal_id, decision, user, json.dumps({'note': note, 'live_csv_changed': False})))
        conn.commit()
    return {'proposal': row, 'live_master_csv_changed': False}


@app.get('/api/admin/master-data/audit')
def admin_master_data_audit(user: str = Depends(require_admin)):
    with db() as conn:
        rows = conn.execute("""SELECT id, proposal_id, action, actor, details, created_at
            FROM master_data_audit ORDER BY created_at DESC LIMIT 500""").fetchall()
    return {'events': rows}

@app.get('/api/admin/master-data/approved-export')
def export_approved_master_data_proposals(user: str = Depends(require_super_admin)):
    """Download approved proposals as a CSV patch; never edits the live master CSV."""
    import csv
    output = io.StringIO(newline='')
    writer = csv.DictWriter(output, fieldnames=MASTER_DATA_FIELDS, extrasaction='ignore')
    writer.writeheader()
    with db() as conn:
        rows = conn.execute(
            """SELECT id, record_data FROM master_data_proposals
               WHERE status='approved' ORDER BY reviewed_at ASC, id ASC"""
        ).fetchall()
    for row in rows:
        record = row['record_data'] or {}
        writer.writerow({field: str(record.get(field, '') or '') for field in MASTER_DATA_FIELDS})
    content = output.getvalue().encode('utf-8-sig')
    from fastapi.responses import Response
    return Response(
        content=content,
        media_type='text/csv; charset=utf-8',
        headers={'Content-Disposition': 'attachment; filename="approved_master_data_proposals.csv"'}
    )



# Bulk Excel intake: validated rows enter the existing proposal queue only.
class BulkMasterDataSubmit(BaseModel):
    records: list[dict]
    source_filename: str = 'master-data-upload.xlsx'


def _bulk_master_data_validate(records, source_filename='master-data-upload.xlsx'):
    import csv
    approved_codes = set()
    try:
        with open(MASTER_DATA_CSV, newline='', encoding='utf-8-sig') as source:
            for row in csv.DictReader(source):
                code = str(row.get('MATERIAL_CODE') or '').strip().casefold()
                if code:
                    approved_codes.add(code)
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail='Approved master_database.csv was not found.')
    with db() as conn:
        staged_rows = conn.execute(
            """SELECT record_data->>'MATERIAL_CODE' AS material_code
               FROM master_data_proposals WHERE status IN ('pending','approved')"""
        ).fetchall()
    staged_codes = {str(row.get('material_code') or '').strip().casefold()
                    for row in staged_rows if str(row.get('material_code') or '').strip()}
    seen_codes, clean_rows, errors, warnings = set(), [], [], []
    for index, raw in enumerate(records, start=2):
        try:
            record = _clean_master_record(raw)
        except HTTPException as exc:
            errors.append({'row': index, 'message': str(exc.detail)})
            continue
        code = record['MATERIAL_CODE'].casefold()
        if code in seen_codes:
            errors.append({'row': index, 'message': f"Duplicate Material Code within this upload: {record['MATERIAL_CODE']}."})
        elif code in approved_codes:
            errors.append({'row': index, 'message': f"Material Code already exists in the approved master CSV: {record['MATERIAL_CODE']}."})
        elif code in staged_codes:
            errors.append({'row': index, 'message': f"Material Code already exists in a pending/approved proposal: {record['MATERIAL_CODE']}."})
        seen_codes.add(code)
        model = record.get('MODEL', '').strip().upper()
        if '7SJ66' in model and not re.fullmatch(
            r'7SJ66\d{2}-[A-Z0-9]{5}-[A-Z0-9]{4}(?:\+[A-Z0-9]{3})?', model
        ):
            warnings.append({'row': index, 'message': '7SJ66 MLFB format should be checked against its Siemens selection chart.'})
        clean_rows.append(record)
    return {'records': clean_rows, 'errors': errors, 'warnings': warnings,
            'source_filename': str(source_filename or 'master-data-upload.xlsx')[:180]}


@app.post('/api/admin/master-data/bulk-upload/preview')
async def preview_master_data_bulk_upload(file: UploadFile = File(...), user: str = Depends(require_admin)):
    from openpyxl import load_workbook
    filename = file.filename or 'master-data-upload.xlsx'
    if not filename.lower().endswith('.xlsx'):
        raise HTTPException(status_code=400, detail='Upload an .xlsx workbook using the ELEXORA master-data format.')
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail='The uploaded Excel file is empty.')
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail='Excel upload exceeds the 10 MB limit.')
    try:
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        sheet = workbook.active
        header_values = [str(value or '').strip().upper() for value in next(
            sheet.iter_rows(min_row=1, max_row=1, values_only=True), ())]
        if 'DESCRIPTION' in header_values and 'DISCRIPTION' not in header_values:
            header_values[header_values.index('DESCRIPTION')] = 'DISCRIPTION'
        missing_headers = [field for field in MASTER_DATA_FIELDS if field not in header_values]
        if missing_headers:
            workbook.close()
            raise HTTPException(status_code=400, detail='Missing required ELEXORA columns: ' + ', '.join(missing_headers))
        positions = {field: header_values.index(field) for field in MASTER_DATA_FIELDS}
        raw_records = []
        for values in sheet.iter_rows(min_row=2, values_only=True):
            if not values or not any(value is not None and str(value).strip() for value in values):
                continue
            raw_records.append({field: (values[pos] if pos < len(values) else '') for field, pos in positions.items()})
            if len(raw_records) > 2000:
                workbook.close()
                raise HTTPException(status_code=400, detail='A single upload may contain at most 2,000 non-empty rows.')
        workbook.close()
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail='Could not read this Excel workbook. Please use the ELEXORA .xlsx template.')
    if not raw_records:
        raise HTTPException(status_code=400, detail='The Excel workbook contains no data rows.')
    result = _bulk_master_data_validate(raw_records, filename)
    result['total_rows'] = len(raw_records)
    result['valid_rows'] = len(result['records']) - len(result['errors'])
    return result


@app.post('/api/admin/master-data/bulk-upload/submit')
def submit_master_data_bulk_upload(payload: BulkMasterDataSubmit, user: str = Depends(require_admin)):
    if not payload.records:
        raise HTTPException(status_code=400, detail='There are no valid rows to submit.')
    if len(payload.records) > 2000:
        raise HTTPException(status_code=400, detail='A single upload may contain at most 2,000 rows.')
    result = _bulk_master_data_validate(payload.records, payload.source_filename)
    if result['errors']:
        raise HTTPException(status_code=409, detail={
            'message': 'The upload changed or conflicts were detected. Please upload the corrected workbook and preview it again.',
            'errors': result['errors']
        })
    import json
    with db() as conn:
        for record in result['records']:
            row = conn.execute(
                """INSERT INTO master_data_proposals(record_data, submitted_by)
                   VALUES(%s::jsonb,%s) RETURNING id""",
                (json.dumps(record), user)
            ).fetchone()
            conn.execute(
                """INSERT INTO master_data_audit(proposal_id,action,actor,details)
                   VALUES(%s,'bulk_created',%s,%s::jsonb)""",
                (row['id'], user, json.dumps({
                    'record': record, 'source_filename': result['source_filename'],
                    'live_csv_changed': False
                }))
            )
        conn.commit()
    return {'submitted': len(result['records']), 'status': 'pending',
            'message': 'Rows submitted as pending proposals. Super Admin review is required.',
            'live_master_csv_changed': False}


def export_combined_book(payload):
    wb = Workbook()
    ws = wb.active
    ws.title = 'BOM'
    thin = Side(style='thin')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    header_data = payload.get('header', {})
    feeders = payload.get('feeders') or []
    rows = payload.get('rows') or []
    n = max(1, len(feeders))
    qty_col = 4
    feeder_start_col = 5
    total_col = feeder_start_col + n
    mpd_col, amd_col = total_col + 1, total_col + 2
    last_col = amd_col
    ws.page_setup.orientation = 'landscape'
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 1
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_margins.left = .25
    ws.page_margins.right = .25
    ws.page_margins.top = .45
    ws.page_margins.bottom = .4
    ws.sheet_view.showGridLines = False
    spec_width = max(36, min(78, max([max((len(line) for line in str(r.get('specification','')).splitlines()), default=0) for r in rows] + [36]) * 0.75))
    designation_width = max(14, min(26, max([len(str(r.get('designation',''))) for r in rows] + [14]) * 0.9))
    feeder_width = max(10, min(18, max([len(str(f.get('name',''))) for f in feeders] + [8]) * 0.9))
    widths = [9, spec_width, designation_width, 9] + [feeder_width] * n + [13, 7, 7]
    for col, width in enumerate(widths, 1):
        ws.column_dimensions[__import__('openpyxl').utils.get_column_letter(col)].width = width
    ws.merge_cells(start_row=1, start_column=1, end_row=2, end_column=min(3, last_col))
    ws.cell(1, 1, '-: Equipment List :-')
    ws.cell(1, 1).font = Font(name='Courier New', size=10)
    ws.cell(1, 1).alignment = Alignment(horizontal='center', vertical='center')
    meta_col = max(4, last_col - 2)
    ws.merge_cells(start_row=1, start_column=meta_col, end_row=2, end_column=last_col)
    ws.cell(1, meta_col, f'Doc.No.: {payload.get("document_no","SI EA/CS/FR/EG/015")}\nRev.No.: {payload.get("revision","1.0")}, Eff.Dt: {payload.get("effective_date","17/07/2026")}\nCreated By: {payload.get("created_by","EA CS ENGG")}')
    ws.cell(1, meta_col).font = Font(name='Arial', size=7)
    ws.cell(1, meta_col).alignment = Alignment(horizontal='right', vertical='top', wrap_text=True)
    ws.row_dimensions[1].height = 34
    ws.row_dimensions[2].height = 16
    for col, label in [(1,'EQPT.NO'),(2,'SPECIFICATION'),(3,'DESIGNATION'),(total_col,'TOTAL EQPT QTY.'),(mpd_col,'MPD'),(amd_col,'AMD')]:
        ws.cell(4, col, label)
    for col in [1,2,3,total_col,mpd_col,amd_col]:
        ws.merge_cells(start_row=4, start_column=col, end_row=6, end_column=col)
    ws.merge_cells(start_row=5, start_column=qty_col, end_row=6, end_column=qty_col)
    ws.cell(5,qty_col,'QTY.')
    ws.merge_cells(start_row=4, start_column=feeder_start_col, end_row=4, end_column=feeder_start_col+n-1)
    ws.cell(4,feeder_start_col,'FEEDER TYPICAL')
    for idx, feeder in enumerate(feeders):
        col=feeder_start_col+idx
        ws.cell(5,col,feeder.get('name',''))
        ws.cell(6,col,feeder.get('quantity') or feeder.get('qty') or 1)
    for r in range(4,7):
        for c in range(1,last_col+1):
            cell=ws.cell(r,c)
            cell.border=border
            cell.alignment=Alignment(horizontal='center',vertical='center',wrap_text=True)
            cell.font=Font(name='Courier New',size=8,bold=True)
    ws.row_dimensions[4].height=23
    ws.row_dimensions[5].height=18
    ws.row_dimensions[6].height=28
    for i,row in enumerate(rows[:64],7):
        quantities = row.get('feeder_quantities') or []
        vals=[row.get('sr',''),row.get('specification',''),row.get('designation','')]
        vals.append('')
        vals.extend(quantities[j] if j < len(quantities) and quantities[j] not in (0, '0', None, '') else '' for j in range(n))
        total_value = row.get('total', '')
        vals.extend(['' if total_value in (0, '0') else total_value, '', ''])
        for col,val in enumerate(vals,1):
            cell=ws.cell(i,col,val)
            cell.border=border
            cell.font=Font(name='Courier New',size=7)
            cell.alignment=Alignment(horizontal='center' if col!=2 else 'left',vertical='center',wrap_text=col==2)
        spec_lines = str(row.get('specification', '')).splitlines() or ['']
        chars_per_line = max(12, int(spec_width * 1.35))
        wrapped_lines = sum(max(1, (len(line) + chars_per_line - 1) // chars_per_line) for line in spec_lines)
        ws.row_dimensions[i].height = max(18, min(240, 10 * wrapped_lines + 6))
    for i in range(7+len(rows[:64]),70):
        for c in range(1,last_col+1):
            ws.cell(i,c).border=border
            ws.cell(i,c).font=Font(name='Courier New',size=7)
        ws.row_dimensions[i].height=15
    # Keep worksheet view focused on the equipment list. Document metadata is
    # printed in the page header/footer when the user presses Ctrl+P.
    ws.oddHeader.center.text = "&B Equipment List &B"
    ws.oddHeader.right.text = f"Doc.No.: {payload.get('document_no','SI EA/CS/FR/EG/015')}\\nRev.No.: {payload.get('revision','1.0')}  Eff.Dt: {payload.get('effective_date','17/07/2026')}"
    ws.oddHeader.right.size = 7
    ws.oddFooter.left.text = f"Client: {header_data.get('client','')}  |  Sales Ref: {header_data.get('sales_ref','')}\\nDescription: {payload.get('description','')}  |  WO: {header_data.get('wo','')}  |  Drawing: {header_data.get('drawing','')}"
    ws.oddFooter.center.text = f"Prepared by: {header_data.get('prep_by','')}  |  Qty: {payload.get('qty','')}  |  ESD: {header_data.get('esd','')}"
    ws.oddFooter.right.text = "Page &P of &N"
    ws.oddFooter.left.size = 7
    ws.oddFooter.center.size = 7
    ws.oddFooter.right.size = 7
    ws.print_area=f'A1:{__import__("openpyxl").utils.get_column_letter(last_col)}76'
    return wb

def export_combined_pdf(payload):
    out=io.BytesIO()
    doc=SimpleDocTemplate(out,pagesize=A4,rightMargin=5*mm,leftMargin=5*mm,topMargin=6*mm,bottomMargin=20*mm)
    styles=getSampleStyleSheet()
    title=ParagraphStyle('combinedtitle',parent=styles['Normal'],fontName='Helvetica-Bold',fontSize=6,leading=7,alignment=TA_CENTER)
    cell=ParagraphStyle('combinedcell',parent=styles['Normal'],fontName='Helvetica',fontSize=4.2,leading=4.8,spaceAfter=0,spaceBefore=0,wordWrap='CJK')
    center=ParagraphStyle('combinedcenter',parent=cell,alignment=TA_CENTER)
    head=ParagraphStyle('combinedhead',parent=cell,fontName='Helvetica-Bold',alignment=TA_CENTER,fontSize=4.0,leading=4.5)
    feeders=payload.get('feeders') or []
    rows=payload.get('rows') or []
    n=max(1,len(feeders))
    data=[[Paragraph('EQPT.NO',head),Paragraph('SPECIFICATION',head),Paragraph('DESIGNATION',head),''] + [Paragraph('FEEDER<br/>TYPICAL',head) if i==0 else '' for i in range(n)] + [Paragraph('TOTAL<br/>EQPT QTY.',head),Paragraph('MPD',head),Paragraph('AMD',head)]]
    data.append(['','','',Paragraph('QTY.',head)]+[Paragraph(str(f.get('name','')),head) for f in feeders]+['','',''])
    data.append(['','','','']+[Paragraph(str(f.get('quantity') or f.get('qty') or 1),center) for f in feeders]+['','',''])
    for row in rows[:64]:
        spec=str(row.get('specification','') or '').replace('&','&amp;').replace('<','&lt;').replace('>','&gt;').replace('\n','<br/>')
        qs=row.get('feeder_quantities') or []
        data.append([Paragraph(str(row.get('sr','')),center),Paragraph(spec,cell),Paragraph(str(row.get('designation','') or ''),center),Paragraph('',center)]+[Paragraph(str(qs[i] if i<len(qs) and qs[i] not in (0, '0', None, '') else ''),center) for i in range(n)]+[Paragraph('' if row.get('total') in (0, '0') else str(row.get('total') or ''),center),'',''])
    available=200*mm
    # Keep each typical-feeder column compact. For one feeder it is 25 mm;
    # with multiple feeders the combined feeder area is capped at 60 mm.
    feeder_total=min(available-74*mm, max(25*mm, 60*mm if n>2 else 25*mm*n))
    feeder_labels=[len(str(f.get('name',''))) for f in feeders]
    weights=[max(1.0,min(2.0,length/10.0)) for length in feeder_labels]
    weight_sum=sum(weights) or n
    feeder_widths=[feeder_total*w/weight_sum for w in weights] if feeders else [feeder_total]
    spec_width=available-74*mm-feeder_total
    widths=[8*mm,spec_width,25*mm,9*mm]+feeder_widths+[12*mm,10*mm,10*mm]
    tbl=Table(data,colWidths=widths,rowHeights=[18,14,20]+[None]*(len(data)-3),repeatRows=3)
    spans=[('SPAN',(0,0),(0,2)),('SPAN',(1,0),(1,2)),('SPAN',(2,0),(2,2)),('SPAN',(3,1),(3,2)),('SPAN',(4,0),(3+n,0)),('SPAN',(4+n,0),(4+n,2)),('SPAN',(5+n,0),(5+n,2)),('SPAN',(6+n,0),(6+n,2))]
    # A compact first-row group label and feeder-specific second/third header rows.
    tbl.setStyle(TableStyle(spans+[('GRID',(0,0),(-1,-1),0.45,colors.black),('BACKGROUND',(0,0),(-1,2),colors.HexColor('#d9e7f0')),('VALIGN',(0,0),(-1,-1),'MIDDLE'),('ALIGN',(0,0),(-1,2),'CENTER'),('LEFTPADDING',(0,0),(-1,-1),2),('RIGHTPADDING',(0,0),(-1,-1),2),('TOPPADDING',(0,0),(-1,-1),1),('BOTTOMPADDING',(0,0),(-1,-1),1)]))
    story=[Paragraph('-: Equipment List :-',title),Spacer(1,3*mm),tbl]
    h=payload.get('header',{})
    def footer(canvas, doc_obj):
        canvas.saveState()
        w,hp=doc_obj.pagesize
        y=8*mm
        canvas.setFont('Helvetica',6.5)
        canvas.drawString(6*mm,y+7*mm,f"Client: {h.get('client','')}  Sales Ref: {h.get('sales_ref','')}")
        canvas.drawString(6*mm,y+4*mm,f"Description: {payload.get('description','')}  WO: {h.get('wo','')}  Drawing: {h.get('drawing','')}")
        canvas.drawString(6*mm,y+1*mm,f"Prepared by: {h.get('prep_by','')}  Qty: {payload.get('qty','')}  ESD: {h.get('esd','')}")
        canvas.restoreState()
    doc.build(story,onFirstPage=footer,onLaterPages=footer,canvasmaker=NumberedCanvas)
    out.seek(0)
    return out

@app.get('/')
def root():
    return {
        'service': 'ELEXORA 2 API',
        'status': 'running',
        'health': '/health',
        'docs': '/docs',
    }


@app.get('/health')
def health():
    return {'status':'ok'}

@app.on_event("startup")
def startup():
    init_auth_db()

@app.post('/api/auth/signup')
def signup(payload: AuthRequest):
    username = payload.username.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username):
        raise HTTPException(status_code=400, detail="Username must be 3-32 characters and use only letters, numbers, dot, underscore or hyphen.")
    errors = validate_password(payload.password)
    if errors:
        raise HTTPException(status_code=400, detail="Password must contain " + ", ".join(errors) + ".")
    try:
        with db() as conn:
            conn.execute(
                "INSERT INTO users(username,password_hash) VALUES(%s,%s)",
                (username.lower(), password_hash(payload.password))
            )
            conn.commit()
    except Exception as exc:
        if getattr(exc, "sqlstate", None) == "23505":
            raise HTTPException(status_code=409, detail="That username already exists. Please choose another.")
        raise
    return {"message":"Account created successfully. You can now sign in."}

@app.post('/api/auth/login')
def login(payload: AuthRequest):
    username = payload.username.strip().lower()
    with db() as conn:
        row = conn.execute(
            "SELECT id,password_hash,username,role FROM users WHERE username=%s",
            (username,)
        ).fetchone()
        if not row or not password_ok(payload.password, row["password_hash"]):
            raise HTTPException(status_code=401, detail="Invalid username or password.")
        token = secrets.token_urlsafe(32)
        expires = int(datetime.now().timestamp()) + TOKEN_TTL_SECONDS
        conn.execute(
            "INSERT INTO sessions(token,user_id,expires_at) VALUES(%s,%s,%s)",
            (token, row["id"], expires)
        )
        conn.commit()
    return {"token":token,"username":row["username"],"role":row["role"],"expires_at":expires}


@app.get('/api/auth/me')
def auth_me(user: str = Depends(current_user)):
    with db() as conn:
        row = conn.execute(
            "SELECT username, role FROM users WHERE username=%s",
            (user.strip().lower(),)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="Account not found.")
    return {
        "username": row["username"],
        "role": row["role"],
        "is_admin": row["role"] in ("admin", "super_admin"),
        "is_super_admin": row["role"] == "super_admin",
    }

@app.post('/api/auth/logout')
def logout(authorization: str = Header(default="")):
    token = authorization[7:].strip() if authorization.startswith("Bearer ") else ""
    if token:
        with db() as conn:
            conn.execute("DELETE FROM sessions WHERE token=%s", (token,))
            conn.commit()
    return {"message":"Signed out."}

@app.post('/api/bom/preview')
async def preview(client:str=Form(''),sales_ref:str=Form(''),drawing:str=Form(''),esd:str=Form(''),wo:str=Form(''),prep_by:str=Form(''),voltage:str=Form(''),msld:UploadFile=File(...),dis:UploadFile=File(...),user:str=Depends(current_user)):
 m,d=await msld.read(),await dis.read(); return extract_from_files(m,msld.filename,d,dis.filename,client,sales_ref,drawing,esd,wo,prep_by,voltage)

def export_book(payload):
 if payload.get('feeders'):
  return export_combined_book(payload)
 wb=Workbook(); ws=wb.active; ws.title='BOM'; thin=Side(style='thin'); border=Border(left=thin,right=thin,top=thin,bottom=thin); h=payload.get('header',{}); rows=payload.get('rows',[]); ws.page_setup.orientation='portrait'; ws.page_setup.paperSize=ws.PAPERSIZE_A4; ws.page_setup.fitToWidth=1; ws.page_setup.fitToHeight=1; ws.sheet_properties.pageSetUpPr.fitToPage=True; ws.page_margins.left=.58; ws.page_margins.right=.75; ws.page_margins.top=.60; ws.page_margins.bottom=.55; ws.sheet_view.showGridLines=False
 for col,width in {'A':12.43,'B':80.99,'C':18.43,'D':10.0,'E':11.87,'F':9.10,'G':5.87,'H':5.87}.items():ws.column_dimensions[col].width=width
 ws.merge_cells('A1:E2'); ws['A1']='-: Equipment List :-'; ws['A1'].font=Font(name='Courier New',size=10); ws['A1'].alignment=Alignment(horizontal='center',vertical='center'); ws['F1']=f'Doc.No.: {payload.get("document_no","SI EA/CS/FR/EG/015")}\nRev.No.: {payload.get("revision","1.0")}, Eff.Dt: {payload.get("effective_date","17/07/2026")}\nCreated By: {payload.get("created_by","EA CS ENGG")}'; ws['F1'].alignment=Alignment(horizontal='right',vertical='top',wrap_text=True); ws['F1'].font=Font(name='Arial',size=7); ws.merge_cells('F1:H2'); ws.row_dimensions[1].height=34; ws.row_dimensions[2].height=16
 for c,v in [('A4','EQPT.NO'),('B4','SPECIFICATION'),('C4','DESIGNATION'),('F4','TOTAL EQPT QTY.'),('G4','MPD'),('H4','AMD')]:ws[c]=v
 ws.merge_cells('A4:A6'); ws.merge_cells('B4:B6'); ws.merge_cells('C4:C6'); ws.merge_cells('D4:E4'); ws.merge_cells('F4:F6'); ws.merge_cells('G4:G6'); ws.merge_cells('H4:H6'); ws['D4']='FEEDER TYPICAL'; ws['D5']='QTY.'; ws['E5']=payload.get('feeder_name',''); ws['E6']=payload.get('feeder_qty','')
 for row in range(4,7):
  for col in range(1,9):ws.cell(row,col).border=border; ws.cell(row,col).alignment=Alignment(horizontal='center',vertical='center',wrap_text=True); ws.cell(row,col).font=Font(name='Courier New',size=10 if row==4 else 9,bold=True)
 ws.row_dimensions[4].height=23.25; ws.row_dimensions[5].height=18; ws.row_dimensions[6].height=18
 for i,row in enumerate(rows[:64],7):
  vals=[row.get('sr',''),row.get('specification',''),row.get('designation',''),' ',row.get('eqpt_qty') if row.get('eqpt_qty') is not None else '',row.get('total') if row.get('total') is not None else '','','']
  for col,val in enumerate(vals,1):ws.cell(i,col,val).border=border; ws.cell(i,col).font=Font(name='Courier New',size=7); ws.cell(i,col).alignment=Alignment(horizontal='center' if col!=2 else 'left',vertical='center',wrap_text=True if col==2 else False)
  ws.row_dimensions[i].height=max(15.6,min(180,15.6*max(1,len(row.get('specification_lines',[])))))
 for i in range(7+len(rows[:64]),70):
  for col in range(1,9):ws.cell(i,col).border=border; ws.cell(i,col).font=Font(name='Courier New',size=7)
  ws.row_dimensions[i].height=15.6
 fr=72; footer_left=[('Item No.','100'),('Client :',h.get('client','')),('Sales Ref No.:',h.get('sales_ref','')),('DATE :',datetime.now().strftime('%d.%m.%Y'))]; footer_mid=[('Description :',payload.get('description','')),('W.O. No.:',h.get('wo','')),('Drg. No.:',h.get('drawing',''))]; footer_right=[('PRE.BY :',h.get('prep_by','')),('Qty.:',payload.get('qty','')),('ESD No.:',h.get('esd','')),('','1 of 1')]
 for i,(label,value) in enumerate(footer_left):
  ws.cell(fr+i,1,label).font=Font(name='Arial',size=7); ws.cell(fr+i,2,value).font=Font(name='Arial',size=7)
  ws.cell(fr+i,1).alignment=Alignment(horizontal='left',vertical='center'); ws.cell(fr+i,2).alignment=Alignment(horizontal='left',vertical='center')
 for i,(label,value) in enumerate(footer_mid):
  ws.cell(fr+i,4,label).font=Font(name='Arial',size=7); ws.cell(fr+i,5,value).font=Font(name='Arial',size=7)
  ws.cell(fr+i,4).alignment=Alignment(horizontal='left',vertical='center'); ws.cell(fr+i,5).alignment=Alignment(horizontal='left',vertical='center')
 for i,(label,value) in enumerate(footer_right):
  ws.cell(fr+i,7,label).font=Font(name='Arial',size=7); ws.cell(fr+i,8,value).font=Font(name='Arial',size=7)
  ws.cell(fr+i,7).alignment=Alignment(horizontal='right',vertical='center'); ws.cell(fr+i,8).alignment=Alignment(horizontal='right',vertical='center')
 ws.print_area='A1:H76'; return wb
@app.post('/api/bom/export')
async def export(payload:dict, user:str=Depends(current_user)):
 wb=export_book(payload);out=io.BytesIO();wb.save(out);out.seek(0);return StreamingResponse(out,media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',headers={'Content-Disposition':'attachment; filename=ELEXORA_BOM.xlsx'})

class NumberedCanvas:
    def __init__(self, *args, **kwargs):
        from reportlab.pdfgen import canvas as pdfcanvas
        self._canvas = pdfcanvas.Canvas(*args, **kwargs)
        self._saved_page_states = []

    def __getattr__(self, name):
        return getattr(self._canvas, name)

    def showPage(self):
        self._saved_page_states.append(dict(self._canvas.__dict__))
        self._canvas._startPage()

    def save(self):
        total = len(self._saved_page_states)
        for state in self._saved_page_states:
            self._canvas.__dict__.update(state)
            self._draw_page_number(total)
            self._canvas.showPage()
        self._canvas.save()

    def _draw_page_number(self, total):
        # Page number occupies its own fourth line in the right footer block.
        w, hp = A4
        self._canvas.setFont('Helvetica', 6.5)
        self._canvas.drawRightString(
            w - 8*mm, 4*mm,
            f'{self._canvas.getPageNumber()} of {total}'
        )


def export_pdf(payload):
    if payload.get('feeders'):
        return export_combined_pdf(payload)
    out = io.BytesIO()
    doc = SimpleDocTemplate(
        out, pagesize=A4,
        rightMargin=8*mm, leftMargin=8*mm,
        topMargin=27*mm, bottomMargin=28*mm
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('bomtitle', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=9, leading=11, alignment=TA_CENTER)
    meta_style = ParagraphStyle('bommeta', parent=styles['Normal'], fontName='Helvetica', fontSize=6.5, leading=8, alignment=TA_RIGHT)
    cell_style = ParagraphStyle('cell', parent=styles['Normal'], fontName='Helvetica', fontSize=6.3, leading=7.5, spaceAfter=0, spaceBefore=0)
    cell_center = ParagraphStyle('cellcenter', parent=cell_style, alignment=TA_CENTER)
    head_style = ParagraphStyle('head', parent=cell_style, fontName='Helvetica-Bold', alignment=TA_CENTER, leading=8)
    h = payload.get('header', {})
    rows = payload.get('rows', [])
    story = [
        Paragraph('-: Equipment List :-', title_style),
        Spacer(1, 4*mm),
    ]
    data = [
        [Paragraph('EQPT.NO',head_style), Paragraph('SPECIFICATION',head_style), Paragraph('DESIGNATION',head_style),
         Paragraph('FEEDER TYPICAL',head_style), '', Paragraph('TOTAL<br/>EQPT<br/>QTY.',head_style), Paragraph('MPD',head_style), Paragraph('AMD',head_style)],
        ['', '', '', Paragraph('QTY.',head_style), Paragraph(str(payload.get('feeder_name','')),head_style), '', '', ''],
        ['', '', '', '', Paragraph(str(payload.get('feeder_qty','')),cell_center), '', '', '']
    ]
    for row in rows[:64]:
        spec = row.get('specification','') or ''
        data.append([
            Paragraph(str(row.get('sr','') or ''), cell_center),
            Paragraph(str(spec).replace('&','&amp;').replace('<','&lt;').replace('>','&gt;').replace('\n','<br/>'), cell_style),
            Paragraph(str(row.get('designation','') or ''), cell_center),
            '',
            Paragraph(str(row.get('eqpt_qty','') or ''), cell_center),
            Paragraph(str(row.get('total','') or ''), cell_center),
            '', ''
        ])
    col_widths=[16*mm, 81*mm, 24*mm, 17*mm, 20*mm, 18*mm, 9*mm, 9*mm]
    tbl=Table(data,colWidths=col_widths,repeatRows=3)
    tbl.setStyle(TableStyle([
        ('SPAN',(0,0),(0,2)),('SPAN',(1,0),(1,2)),('SPAN',(2,0),(2,2)),
        ('SPAN',(3,0),(4,0)),('SPAN',(5,0),(5,2)),('SPAN',(6,0),(6,2)),('SPAN',(7,0),(7,2)),
        ('GRID',(0,0),(-1,-1),0.45,colors.black),
        ('BACKGROUND',(0,0),(-1,2),colors.HexColor('#d9e7f0')),
        ('VALIGN',(0,0),(-1,-1),'MIDDLE'),
        ('ALIGN',(0,0),(-1,2),'CENTER'),
        ('LEFTPADDING',(0,0),(-1,-1),2),('RIGHTPADDING',(0,0),(-1,-1),2),
        ('TOPPADDING',(0,0),(-1,-1),2),('BOTTOMPADDING',(0,0),(-1,-1),2),
    ]))
    story.append(tbl)

    def footer(canvas, doc_obj):
        canvas.saveState()
        w, hp = A4
        y = 11*mm
        canvas.setFont('Helvetica', 6.5)

        # Fixed footer zones matching the supplied BOM: left / center / right.
        left_x = 8*mm
        mid_x = 82*mm
        right_x = 164*mm
        line_step = 3*mm

        left=[
            ('Item No.','100'),
            ('Client :',h.get('client','')),
            ('Sales Ref No.:',h.get('sales_ref','')),
            ('DATE :',datetime.now().strftime('%d.%m.%Y'))
        ]
        mid=[
            ('Description :',payload.get('description','')),
            ('W.O. No.:',h.get('wo','')),
            ('Drg. No.:',h.get('drawing',''))
        ]
        right=[
            ('PRE.BY :',h.get('prep_by','')),
            ('Qty.:',payload.get('qty','')),
            ('ESD No.:',h.get('esd',''))
        ]

        for i,(label,value) in enumerate(left):
            canvas.drawString(left_x, y+(2.5-i)*line_step, (label+' '+str(value)).strip())

        # Keep the center footer inside its own zone so it cannot overlap the right block.
        from reportlab.pdfbase.pdfmetrics import stringWidth
        mid_max_x = right_edge - 5*mm
        for i,(label,value) in enumerate(mid):
            text_value = (label+' '+str(value)).strip()
            max_width = mid_max_x - mid_x
            while stringWidth(text_value, 'Helvetica', 6.5) > max_width and len(text_value) > len(label) + 4:
                text_value = text_value[:-2].rstrip() + '...'
            canvas.drawString(mid_x, y+(2.5-i)*line_step, text_value)

        for i,(label,value) in enumerate(right):
            canvas.drawString(right_x, y+(2.5-i)*line_step, (label+' '+str(value)).strip())

        canvas.restoreState()
    doc.build(story,onFirstPage=footer,onLaterPages=footer,canvasmaker=NumberedCanvas)
    out.seek(0)
    return out

@app.post('/api/bom/export-pdf')
async def export_pdf_endpoint(payload:dict, user:str=Depends(current_user)):
    out=export_pdf(payload)
    return StreamingResponse(out,media_type='application/pdf',headers={'Content-Disposition':f'attachment; filename=BOM_{re.sub(r"[^A-Za-z0-9._-]+", "_", str(payload.get("header", {}).get("wo", "WO"))).strip("_")}.pdf'})
@app.post('/api/bom/preview-multiple')
async def preview_multiple(
    client: str = Form(''), sales_ref: str = Form(''), drawing: str = Form(''),
    esd: str = Form(''), wo: str = Form(''), prep_by: str = Form(''),
    voltage: str = Form(''), mslds: list[UploadFile] = File(...),
    dis: UploadFile = File(...), user: str = Depends(current_user)
):
    if not mslds:
        raise HTTPException(status_code=400, detail='Upload at least one MSLD PDF.')
    dis_name = dis.filename or 'DIS.pdf'
    dis_bytes = await dis.read()
    items = []
    filenames = []
    for upload in mslds:
        filename = upload.filename or 'MSLD.pdf'
        items.append((await upload.read(), filename))
        filenames.append(filename)
    result = extract_multiple_from_files(items, dis_bytes, dis_name, client, sales_ref, drawing, esd, wo, prep_by, voltage)
    import json
    with db() as conn:
        conn.execute(
            """INSERT INTO bom_history (username, customer_name, work_order_no, voltage, msld_files, dis_filename, row_count)
               VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)""",
            (user, str(client or result.get('header', {}).get('client', '')), str(wo or result.get('header', {}).get('wo', '')),
             str(voltage or ''), json.dumps(filenames), dis_name, len(result.get('rows') or []))
        )
        conn.commit()
    return result




class SiemensLookupRequest(BaseModel):
    mlfb: str


@app.post('/api/siemens/product-lookup')
def siemens_product_lookup(payload: SiemensLookupRequest, user: str = Depends(current_user)):
    """POC: live lookup of a supplied 7SJ66 MLFB on Siemens Industry Mall."""
    return lookup_siemens_product(payload.mlfb)

@app.get('/api/admin/bom-history')
def admin_bom_history(user: str = Depends(require_admin)):
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) AS total FROM bom_history").fetchone()['total']
        rows = conn.execute(
            """SELECT id, username, customer_name, work_order_no, voltage, msld_files,
                      dis_filename, row_count, created_at
               FROM bom_history ORDER BY created_at DESC LIMIT 500"""
        ).fetchall()
    return {'total_generated': total, 'history': rows}

def export_combined_book(payload):
    wb = Workbook()
    ws = wb.active
    ws.title = 'BOM'
    thin = Side(style='thin')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    header_data = payload.get('header', {})
    feeders = payload.get('feeders') or []
    rows = payload.get('rows') or []
    n = max(1, len(feeders))
    qty_col = 4
    feeder_start_col = 5
    total_col = feeder_start_col + n
    mpd_col, amd_col = total_col + 1, total_col + 2
    last_col = amd_col
    ws.page_setup.orientation = 'landscape'
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 1
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_margins.left = .25
    ws.page_margins.right = .25
    ws.page_margins.top = .45
    ws.page_margins.bottom = .4
    ws.sheet_view.showGridLines = False
    spec_width = max(36, min(78, max([max((len(line) for line in str(r.get('specification','')).splitlines()), default=0) for r in rows] + [36]) * 0.75))
    designation_width = max(14, min(26, max([len(str(r.get('designation',''))) for r in rows] + [14]) * 0.9))
    feeder_width = max(10, min(18, max([len(str(f.get('name',''))) for f in feeders] + [8]) * 0.9))
    widths = [9, spec_width, designation_width, 9] + [feeder_width] * n + [13, 7, 7]
    for col, width in enumerate(widths, 1):
        ws.column_dimensions[__import__('openpyxl').utils.get_column_letter(col)].width = width
    ws.merge_cells(start_row=1, start_column=1, end_row=2, end_column=min(3, last_col))
    ws.cell(1, 1, '-: Equipment List :-')
    ws.cell(1, 1).font = Font(name='Courier New', size=10)
    ws.cell(1, 1).alignment = Alignment(horizontal='center', vertical='center')
    meta_col = max(4, last_col - 2)
    ws.merge_cells(start_row=1, start_column=meta_col, end_row=2, end_column=last_col)
    ws.cell(1, meta_col, f'Doc.No.: {payload.get("document_no","SI EA/CS/FR/EG/015")}\nRev.No.: {payload.get("revision","1.0")}, Eff.Dt: {payload.get("effective_date","17/07/2026")}\nCreated By: {payload.get("created_by","EA CS ENGG")}')
    ws.cell(1, meta_col).font = Font(name='Arial', size=7)
    ws.cell(1, meta_col).alignment = Alignment(horizontal='right', vertical='top', wrap_text=True)
    ws.row_dimensions[1].height = 34
    ws.row_dimensions[2].height = 16
    for col, label in [(1,'EQPT.NO'),(2,'SPECIFICATION'),(3,'DESIGNATION'),(total_col,'TOTAL EQPT QTY.'),(mpd_col,'MPD'),(amd_col,'AMD')]:
        ws.cell(4, col, label)
    for col in [1,2,3,total_col,mpd_col,amd_col]:
        ws.merge_cells(start_row=4, start_column=col, end_row=6, end_column=col)
    ws.merge_cells(start_row=5, start_column=qty_col, end_row=6, end_column=qty_col)
    ws.cell(5,qty_col,'QTY.')
    ws.merge_cells(start_row=4, start_column=feeder_start_col, end_row=4, end_column=feeder_start_col+n-1)
    ws.cell(4,feeder_start_col,'FEEDER TYPICAL')
    for idx, feeder in enumerate(feeders):
        col=feeder_start_col+idx
        ws.cell(5,col,feeder.get('name',''))
        ws.cell(6,col,feeder.get('quantity') or feeder.get('qty') or 1)
    for r in range(4,7):
        for c in range(1,last_col+1):
            cell=ws.cell(r,c)
            cell.border=border
            cell.alignment=Alignment(horizontal='center',vertical='center',wrap_text=True)
            cell.font=Font(name='Courier New',size=8,bold=True)
    ws.row_dimensions[4].height=23
    ws.row_dimensions[5].height=18
    ws.row_dimensions[6].height=28
    for i,row in enumerate(rows[:64],7):
        quantities = row.get('feeder_quantities') or []
        vals=[row.get('sr',''),row.get('specification',''),row.get('designation','')]
        vals.append('')
        vals.extend(quantities[j] if j < len(quantities) and quantities[j] not in (0, '0', None, '') else '' for j in range(n))
        total_value = row.get('total', '')
        vals.extend(['' if total_value in (0, '0') else total_value, '', ''])
        for col,val in enumerate(vals,1):
            cell=ws.cell(i,col,val)
            cell.border=border
            cell.font=Font(name='Courier New',size=7)
            cell.alignment=Alignment(horizontal='center' if col!=2 else 'left',vertical='center',wrap_text=col==2)
        spec_lines = str(row.get('specification', '')).splitlines() or ['']
        chars_per_line = max(12, int(spec_width * 1.35))
        wrapped_lines = sum(max(1, (len(line) + chars_per_line - 1) // chars_per_line) for line in spec_lines)
        ws.row_dimensions[i].height = max(18, min(240, 10 * wrapped_lines + 6))
    for i in range(7+len(rows[:64]),70):
        for c in range(1,last_col+1):
            ws.cell(i,c).border=border
            ws.cell(i,c).font=Font(name='Courier New',size=7)
        ws.row_dimensions[i].height=15
    # Keep worksheet view focused on the equipment list. Document metadata is
    # printed in the page header/footer when the user presses Ctrl+P.
    ws.oddHeader.center.text = "&B Equipment List &B"
    ws.oddHeader.right.text = f"Doc.No.: {payload.get('document_no','SI EA/CS/FR/EG/015')}\\nRev.No.: {payload.get('revision','1.0')}  Eff.Dt: {payload.get('effective_date','17/07/2026')}"
    ws.oddHeader.right.size = 7
    ws.oddFooter.left.text = f"Client: {header_data.get('client','')}  |  Sales Ref: {header_data.get('sales_ref','')}\\nDescription: {payload.get('description','')}  |  WO: {header_data.get('wo','')}  |  Drawing: {header_data.get('drawing','')}"
    ws.oddFooter.center.text = f"Prepared by: {header_data.get('prep_by','')}  |  Qty: {payload.get('qty','')}  |  ESD: {header_data.get('esd','')}"
    ws.oddFooter.right.text = "Page &P of &N"
    ws.oddFooter.left.size = 7
    ws.oddFooter.center.size = 7
    ws.oddFooter.right.size = 7
    ws.print_area=f'A1:{__import__("openpyxl").utils.get_column_letter(last_col)}76'
    return wb

def export_combined_pdf(payload):
    out=io.BytesIO()
    doc=SimpleDocTemplate(out,pagesize=A4,rightMargin=5*mm,leftMargin=5*mm,topMargin=6*mm,bottomMargin=20*mm)
    styles=getSampleStyleSheet()
    title=ParagraphStyle('combinedtitle',parent=styles['Normal'],fontName='Helvetica-Bold',fontSize=6,leading=7,alignment=TA_CENTER)
    cell=ParagraphStyle('combinedcell',parent=styles['Normal'],fontName='Helvetica',fontSize=4.2,leading=4.8,spaceAfter=0,spaceBefore=0,wordWrap='CJK')
    center=ParagraphStyle('combinedcenter',parent=cell,alignment=TA_CENTER)
    head=ParagraphStyle('combinedhead',parent=cell,fontName='Helvetica-Bold',alignment=TA_CENTER,fontSize=4.0,leading=4.5)
    feeders=payload.get('feeders') or []
    rows=payload.get('rows') or []
    n=max(1,len(feeders))
    data=[[Paragraph('EQPT.NO',head),Paragraph('SPECIFICATION',head),Paragraph('DESIGNATION',head),''] + [Paragraph('FEEDER<br/>TYPICAL',head) if i==0 else '' for i in range(n)] + [Paragraph('TOTAL<br/>EQPT QTY.',head),Paragraph('MPD',head),Paragraph('AMD',head)]]
    data.append(['','','',Paragraph('QTY.',head)]+[Paragraph(str(f.get('name','')),head) for f in feeders]+['','',''])
    data.append(['','','','']+[Paragraph(str(f.get('quantity') or f.get('qty') or 1),center) for f in feeders]+['','',''])
    for row in rows[:64]:
        spec=str(row.get('specification','') or '').replace('&','&amp;').replace('<','&lt;').replace('>','&gt;').replace('\n','<br/>')
        qs=row.get('feeder_quantities') or []
        data.append([Paragraph(str(row.get('sr','')),center),Paragraph(spec,cell),Paragraph(str(row.get('designation','') or ''),center),Paragraph('',center)]+[Paragraph(str(qs[i] if i<len(qs) and qs[i] not in (0, '0', None, '') else ''),center) for i in range(n)]+[Paragraph('' if row.get('total') in (0, '0') else str(row.get('total') or ''),center),'',''])
    available=200*mm
    fixed=[8*mm,66*mm,25*mm,9*mm,12*mm,10*mm,10*mm]
    # Allocate a little more space to long feeder labels, while keeping the
    # complete table inside landscape A4. Paragraph cells wrap and expand rows.
    remaining=max(8*mm, available-sum(fixed))
    feeder_labels=[len(str(f.get('name',''))) for f in feeders]
    weights=[max(1.0,min(2.0,length/10.0)) for length in feeder_labels]
    weight_sum=sum(weights) or n
    feeder_widths=[remaining*w/weight_sum for w in weights] if feeders else [remaining]
    widths=[fixed[0],fixed[1],fixed[2],fixed[3]]+feeder_widths+[fixed[4],fixed[5],fixed[6]]
    tbl=Table(data,colWidths=widths,rowHeights=[18,14,20]+[None]*(len(data)-3),repeatRows=3)
    spans=[('SPAN',(0,0),(0,2)),('SPAN',(1,0),(1,2)),('SPAN',(2,0),(2,2)),('SPAN',(3,1),(3,2)),('SPAN',(4,0),(3+n,0)),('SPAN',(4+n,0),(4+n,2)),('SPAN',(5+n,0),(5+n,2)),('SPAN',(6+n,0),(6+n,2))]
    # A compact first-row group label and feeder-specific second/third header rows.
    tbl.setStyle(TableStyle(spans+[('GRID',(0,0),(-1,-1),0.45,colors.black),('BACKGROUND',(0,0),(-1,2),colors.HexColor('#d9e7f0')),('VALIGN',(0,0),(-1,-1),'MIDDLE'),('ALIGN',(0,0),(-1,2),'CENTER'),('LEFTPADDING',(0,0),(-1,-1),2),('RIGHTPADDING',(0,0),(-1,-1),2),('TOPPADDING',(0,0),(-1,-1),1),('BOTTOMPADDING',(0,0),(-1,-1),1)]))
    story=[Paragraph('-: Equipment List :-',title),Spacer(1,3*mm),tbl]
    h=payload.get('header',{})
    def footer(canvas, doc_obj):
        canvas.saveState()
        w,hp=doc_obj.pagesize
        y=8*mm
        canvas.setFont('Helvetica',6.5)
        canvas.drawString(6*mm,y+7*mm,f"Client: {h.get('client','')}  Sales Ref: {h.get('sales_ref','')}")
        canvas.drawString(6*mm,y+4*mm,f"Description: {payload.get('description','')}  WO: {h.get('wo','')}  Drawing: {h.get('drawing','')}")
        canvas.drawString(6*mm,y+1*mm,f"Prepared by: {h.get('prep_by','')}  Qty: {payload.get('qty','')}  ESD: {h.get('esd','')}")
        canvas.restoreState()
    doc.build(story,onFirstPage=footer,onLaterPages=footer,canvasmaker=NumberedCanvas)
    out.seek(0)
    return out

@app.get('/')
def root():
    return {
        'service': 'ELEXORA 2 API',
        'status': 'running',
        'health': '/health',
        'docs': '/docs',
    }


@app.get('/health')
def health():
    return {'status':'ok'}

@app.on_event("startup")
def startup():
    init_auth_db()

@app.post('/api/auth/signup')
def signup(payload: AuthRequest):
    username = payload.username.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username):
        raise HTTPException(status_code=400, detail="Username must be 3-32 characters and use only letters, numbers, dot, underscore or hyphen.")
    errors = validate_password(payload.password)
    if errors:
        raise HTTPException(status_code=400, detail="Password must contain " + ", ".join(errors) + ".")
    try:
        with db() as conn:
            conn.execute(
                "INSERT INTO users(username,password_hash) VALUES(%s,%s)",
                (username.lower(), password_hash(payload.password))
            )
            conn.commit()
    except Exception as exc:
        if getattr(exc, "sqlstate", None) == "23505":
            raise HTTPException(status_code=409, detail="That username already exists. Please choose another.")
        raise
    return {"message":"Account created successfully. You can now sign in."}

@app.post('/api/auth/login')
def login(payload: AuthRequest):
    username = payload.username.strip().lower()
    with db() as conn:
        row = conn.execute(
            "SELECT id,password_hash,username FROM users WHERE username=%s",
            (username,)
        ).fetchone()
        if not row or not password_ok(payload.password, row["password_hash"]):
            raise HTTPException(status_code=401, detail="Invalid username or password.")
        token = secrets.token_urlsafe(32)
        expires = int(datetime.now().timestamp()) + TOKEN_TTL_SECONDS
        conn.execute(
            "INSERT INTO sessions(token,user_id,expires_at) VALUES(%s,%s,%s)",
            (token, row["id"], expires)
        )
        conn.commit()
    return {"token":token,"username":row["username"],"expires_at":expires}

@app.post('/api/auth/logout')
def logout(authorization: str = Header(default="")):
    token = authorization[7:].strip() if authorization.startswith("Bearer ") else ""
    if token:
        with db() as conn:
            conn.execute("DELETE FROM sessions WHERE token=%s", (token,))
            conn.commit()
    return {"message":"Signed out."}

@app.post('/api/bom/preview')
async def preview(client:str=Form(''),sales_ref:str=Form(''),drawing:str=Form(''),esd:str=Form(''),wo:str=Form(''),prep_by:str=Form(''),voltage:str=Form(''),msld:UploadFile=File(...),dis:UploadFile=File(...),user:str=Depends(current_user)):
 m,d=await msld.read(),await dis.read(); return extract_from_files(m,msld.filename,d,dis.filename,client,sales_ref,drawing,esd,wo,prep_by,voltage)

def export_book(payload):
 if payload.get('feeders'):
  return export_combined_book(payload)
 wb=Workbook(); ws=wb.active; ws.title='BOM'; thin=Side(style='thin'); border=Border(left=thin,right=thin,top=thin,bottom=thin); h=payload.get('header',{}); rows=payload.get('rows',[]); ws.page_setup.orientation='portrait'; ws.page_setup.paperSize=ws.PAPERSIZE_A4; ws.page_setup.fitToWidth=1; ws.page_setup.fitToHeight=1; ws.sheet_properties.pageSetUpPr.fitToPage=True; ws.page_margins.left=.58; ws.page_margins.right=.75; ws.page_margins.top=.60; ws.page_margins.bottom=.55; ws.sheet_view.showGridLines=False
 for col,width in {'A':12.43,'B':80.99,'C':18.43,'D':10.0,'E':11.87,'F':9.10,'G':5.87,'H':5.87}.items():ws.column_dimensions[col].width=width
 ws.merge_cells('A1:E2'); ws['A1']='-: Equipment List :-'; ws['A1'].font=Font(name='Courier New',size=10); ws['A1'].alignment=Alignment(horizontal='center',vertical='center'); ws['F1']=f'Doc.No.: {payload.get("document_no","SI EA/CS/FR/EG/015")}\nRev.No.: {payload.get("revision","1.0")}, Eff.Dt: {payload.get("effective_date","17/07/2026")}\nCreated By: {payload.get("created_by","EA CS ENGG")}'; ws['F1'].alignment=Alignment(horizontal='right',vertical='top',wrap_text=True); ws['F1'].font=Font(name='Arial',size=7); ws.merge_cells('F1:H2'); ws.row_dimensions[1].height=34; ws.row_dimensions[2].height=16
 for c,v in [('A4','EQPT.NO'),('B4','SPECIFICATION'),('C4','DESIGNATION'),('F4','TOTAL EQPT QTY.'),('G4','MPD'),('H4','AMD')]:ws[c]=v
 ws.merge_cells('A4:A6'); ws.merge_cells('B4:B6'); ws.merge_cells('C4:C6'); ws.merge_cells('D4:E4'); ws.merge_cells('F4:F6'); ws.merge_cells('G4:G6'); ws.merge_cells('H4:H6'); ws['D4']='FEEDER TYPICAL'; ws['D5']='QTY.'; ws['E5']=payload.get('feeder_name',''); ws['E6']=payload.get('feeder_qty','')
 for row in range(4,7):
  for col in range(1,9):ws.cell(row,col).border=border; ws.cell(row,col).alignment=Alignment(horizontal='center',vertical='center',wrap_text=True); ws.cell(row,col).font=Font(name='Courier New',size=10 if row==4 else 9,bold=True)
 ws.row_dimensions[4].height=23.25; ws.row_dimensions[5].height=18; ws.row_dimensions[6].height=18
 for i,row in enumerate(rows[:64],7):
  vals=[row.get('sr',''),row.get('specification',''),row.get('designation',''),' ',row.get('eqpt_qty') if row.get('eqpt_qty') is not None else '',row.get('total') if row.get('total') is not None else '','','']
  for col,val in enumerate(vals,1):ws.cell(i,col,val).border=border; ws.cell(i,col).font=Font(name='Courier New',size=7); ws.cell(i,col).alignment=Alignment(horizontal='center' if col!=2 else 'left',vertical='center',wrap_text=True if col==2 else False)
  ws.row_dimensions[i].height=max(15.6,min(180,15.6*max(1,len(row.get('specification_lines',[])))))
 for i in range(7+len(rows[:64]),70):
  for col in range(1,9):ws.cell(i,col).border=border; ws.cell(i,col).font=Font(name='Courier New',size=7)
  ws.row_dimensions[i].height=15.6
 fr=72; footer_left=[('Item No.','100'),('Client :',h.get('client','')),('Sales Ref No.:',h.get('sales_ref','')),('DATE :',datetime.now().strftime('%d.%m.%Y'))]; footer_mid=[('Description :',payload.get('description','')),('W.O. No.:',h.get('wo','')),('Drg. No.:',h.get('drawing',''))]; footer_right=[('PRE.BY :',h.get('prep_by','')),('Qty.:',payload.get('qty','')),('ESD No.:',h.get('esd','')),('','1 of 1')]
 for i,(label,value) in enumerate(footer_left):
  ws.cell(fr+i,1,label).font=Font(name='Arial',size=7); ws.cell(fr+i,2,value).font=Font(name='Arial',size=7)
  ws.cell(fr+i,1).alignment=Alignment(horizontal='left',vertical='center'); ws.cell(fr+i,2).alignment=Alignment(horizontal='left',vertical='center')
 for i,(label,value) in enumerate(footer_mid):
  ws.cell(fr+i,4,label).font=Font(name='Arial',size=7); ws.cell(fr+i,5,value).font=Font(name='Arial',size=7)
  ws.cell(fr+i,4).alignment=Alignment(horizontal='left',vertical='center'); ws.cell(fr+i,5).alignment=Alignment(horizontal='left',vertical='center')
 for i,(label,value) in enumerate(footer_right):
  ws.cell(fr+i,7,label).font=Font(name='Arial',size=7); ws.cell(fr+i,8,value).font=Font(name='Arial',size=7)
  ws.cell(fr+i,7).alignment=Alignment(horizontal='right',vertical='center'); ws.cell(fr+i,8).alignment=Alignment(horizontal='right',vertical='center')
 ws.print_area='A1:H76'; return wb
@app.post('/api/bom/export')
async def export(payload:dict, user:str=Depends(current_user)):
 wb=export_book(payload);out=io.BytesIO();wb.save(out);out.seek(0);return StreamingResponse(out,media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',headers={'Content-Disposition':'attachment; filename=ELEXORA_BOM.xlsx'})

class NumberedCanvas:
    def __init__(self, *args, **kwargs):
        from reportlab.pdfgen import canvas as pdfcanvas
        self._canvas = pdfcanvas.Canvas(*args, **kwargs)
        self._saved_page_states = []

    def __getattr__(self, name):
        return getattr(self._canvas, name)

    def showPage(self):
        self._saved_page_states.append(dict(self._canvas.__dict__))
        self._canvas._startPage()

    def save(self):
        total = len(self._saved_page_states)
        for state in self._saved_page_states:
            self._canvas.__dict__.update(state)
            self._draw_page_number(total)
            self._canvas.showPage()
        self._canvas.save()

    def _draw_page_number(self, total):
        # Page number occupies its own fourth line in the right footer block.
        w, hp = A4
        self._canvas.setFont('Helvetica', 6.5)
        self._canvas.drawRightString(
            w - 8*mm, 4*mm,
            f'{self._canvas.getPageNumber()} of {total}'
        )


def export_pdf(payload):
    if payload.get('feeders'):
        return export_combined_pdf(payload)
    out = io.BytesIO()
    doc = SimpleDocTemplate(
        out, pagesize=A4,
        rightMargin=8*mm, leftMargin=8*mm,
        topMargin=12*mm, bottomMargin=28*mm
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('bomtitle', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=9, leading=11, alignment=TA_CENTER)
    meta_style = ParagraphStyle('bommeta', parent=styles['Normal'], fontName='Helvetica', fontSize=6.5, leading=8, alignment=TA_RIGHT)
    cell_style = ParagraphStyle('cell', parent=styles['Normal'], fontName='Helvetica', fontSize=6.3, leading=7.5, spaceAfter=0, spaceBefore=0)
    cell_center = ParagraphStyle('cellcenter', parent=cell_style, alignment=TA_CENTER)
    head_style = ParagraphStyle('head', parent=cell_style, fontName='Helvetica-Bold', alignment=TA_CENTER, leading=8)
    h = payload.get('header', {})
    rows = payload.get('rows', [])
    story = [
        Paragraph('-: Equipment List :-', title_style),
        Spacer(1, 4*mm),
    ]
    data = [
        [Paragraph('EQPT.NO',head_style), Paragraph('SPECIFICATION',head_style), Paragraph('DESIGNATION',head_style),
         Paragraph('FEEDER TYPICAL',head_style), '', Paragraph('TOTAL<br/>EQPT<br/>QTY.',head_style), Paragraph('MPD',head_style), Paragraph('AMD',head_style)],
        ['', '', '', Paragraph('QTY.',head_style), Paragraph(str(payload.get('feeder_name','')),head_style), '', '', ''],
        ['', '', '', '', Paragraph(str(payload.get('feeder_qty','')),cell_center), '', '', '']
    ]
    for row in rows[:64]:
        spec = row.get('specification','') or ''
        data.append([
            Paragraph(str(row.get('sr','') or ''), cell_center),
            Paragraph(str(spec).replace('&','&amp;').replace('<','&lt;').replace('>','&gt;').replace('\n','<br/>'), cell_style),
            Paragraph(str(row.get('designation','') or ''), cell_center),
            '',
            Paragraph(str(row.get('eqpt_qty','') or ''), cell_center),
            Paragraph(str(row.get('total','') or ''), cell_center),
            '', ''
        ])
    col_widths=[16*mm, 81*mm, 24*mm, 17*mm, 20*mm, 18*mm, 9*mm, 9*mm]
    tbl=Table(data,colWidths=col_widths,repeatRows=3)
    tbl.setStyle(TableStyle([
        ('SPAN',(0,0),(0,2)),('SPAN',(1,0),(1,2)),('SPAN',(2,0),(2,2)),
        ('SPAN',(3,0),(4,0)),('SPAN',(5,0),(5,2)),('SPAN',(6,0),(6,2)),('SPAN',(7,0),(7,2)),
        ('GRID',(0,0),(-1,-1),0.45,colors.black),
        ('BACKGROUND',(0,0),(-1,2),colors.HexColor('#d9e7f0')),
        ('VALIGN',(0,0),(-1,-1),'MIDDLE'),
        ('ALIGN',(0,0),(-1,2),'CENTER'),
        ('LEFTPADDING',(0,0),(-1,-1),2),('RIGHTPADDING',(0,0),(-1,-1),2),
        ('TOPPADDING',(0,0),(-1,-1),2),('BOTTOMPADDING',(0,0),(-1,-1),2),
    ]))
    story.append(tbl)

    def footer(canvas, doc_obj):
        canvas.saveState()
        w, hp = A4
        y = 11*mm
        canvas.setFont('Helvetica', 6.5)

        # Fixed footer zones matching the supplied BOM: left / center / right.
        left_x = 8*mm
        mid_x = 82*mm
        right_x = 164*mm
        line_step = 3*mm

        left=[
            ('Item No.','100'),
            ('Client :',h.get('client','')),
            ('Sales Ref No.:',h.get('sales_ref','')),
            ('DATE :',datetime.now().strftime('%d.%m.%Y'))
        ]
        mid=[
            ('Description :',payload.get('description','')),
            ('W.O. No.:',h.get('wo','')),
            ('Drg. No.:',h.get('drawing',''))
        ]
        right=[
            ('PRE.BY :',h.get('prep_by','')),
            ('Qty.:',payload.get('qty','')),
            ('ESD No.:',h.get('esd',''))
        ]

        for i,(label,value) in enumerate(left):
            canvas.drawString(left_x, y+(2.5-i)*line_step, (label+' '+str(value)).strip())

        # Keep the center footer inside its own zone so it cannot overlap the right block.
        from reportlab.pdfbase.pdfmetrics import stringWidth
        mid_max_x = right_x - 5*mm
        for i,(label,value) in enumerate(mid):
            text_value = (label+' '+str(value)).strip()
            max_width = mid_max_x - mid_x
            while stringWidth(text_value, 'Helvetica', 6.5) > max_width and len(text_value) > len(label) + 4:
                text_value = text_value[:-2].rstrip() + '...'
            canvas.drawString(mid_x, y+(2.5-i)*line_step, text_value)

        for i,(label,value) in enumerate(right):
            canvas.drawString(right_x, y+(2.5-i)*line_step, (label+' '+str(value)).strip())

        canvas.restoreState()
    doc.build(story,onFirstPage=footer,onLaterPages=footer,canvasmaker=NumberedCanvas)
    out.seek(0)
    return out

@app.post('/api/bom/export-pdf')
async def export_pdf_endpoint(payload:dict, user:str=Depends(current_user)):
    out=export_pdf(payload)
    return StreamingResponse(out,media_type='application/pdf',headers={'Content-Disposition':f'attachment; filename=BOM_{re.sub(r"[^A-Za-z0-9._-]+", "_", str(payload.get("header", {}).get("wo", "WO"))).strip("_")}.pdf'})
def transform_word(page,word):
 pts=[fitz.Point(word[0],word[1])*page.rotation_matrix,fitz.Point(word[2],word[3])*page.rotation_matrix]
 return {'x':(pts[0].x+pts[1].x)/2,'y':(pts[0].y+pts[1].y)/2,'text':word[4]}
def page_words(page): return [transform_word(page,w) for w in page.get_text('words') if w[4].strip()]
def clean(s): return re.sub(r'\s+',' ',s).strip(' -')
def words_text(words): return ' '.join(w['text'] for w in sorted(words,key=lambda z:(round(z['y'],1),z['x'])))
def text_in_band(words,xmin,xmax,ymin,ymax): return clean(words_text([w for w in words if xmin<=w['x']<xmax and ymin<=w['y']<ymax]))
def rows_by_designation(words,xmin,xmax,ymin,ymax):
 vals=[]
 for w in words:
  if xmin<=w['x']<=xmax and ymin<=w['y']<=ymax:
   t=clean(w['text'])
   if t.upper() in {'DESIG.','QTY.','NO.'}: continue
   if re.fullmatch(r'[A-Z0-9]+(?:[-/,][A-Z0-9]+)*',t,re.I) and re.search(r'\d',t): vals.append((w['y'],t))
 vals.sort(); out=[]
 for y,t in vals:
  if not out or abs(y-out[-1][0])>5: out.append([y,t])
 return out
def designation_qty(des):
 total=0
 for part in re.split(r'[,/]',(des or '').upper()):
  part=part.strip()
  # Supports both letter-prefixed (F1-F3) and digit+letter (1H1-1H3)
  # designations used by the fixed MSLD template.
  m=re.fullmatch(r'(?:[A-Z]+|\d+[A-Z]+)(\d+)-(?:[A-Z]+|\d+[A-Z]+)?(\d+)',part)
  if m: total+=max(1,int(m.group(2))-int(m.group(1))+1)
  elif re.fullmatch(r'(?:[A-Z]+|\d+[A-Z]+)\d+',part): total+=1
 if total==0 and des:
  m=re.fullmatch(r'(?:[A-Z]+|\d+[A-Z]+)(\d+)-(?:[A-Z]+|\d+[A-Z]+)(\d+)',des.upper())
  if m: total=max(1,int(m.group(2))-int(m.group(1))+1)
 return total or None
def master_match(description,details,designation):
 # This final definition is active at runtime; include designation and details.
 s=(str(description or '')+' '+str(details or '')+' '+str(designation or '')).upper()
 des=re.sub(r'\s+','',str(designation or '')).upper()
 if des == 'E2':return 'E2_LED'
 if 'LED LAMP' in s or re.search(r'\bLED\b',s):return 'LED'
 if 'CURRENT TRANSFORMER' in s:return 'CT'
 if 'POTENTIAL TRANSFORMER' in s:return 'PT'
 if re.search(r'\b7SJ66\d{2}\b', s) or 'NUM. PROT. RELAY' in s or 'NUMERICAL PROTECTION RELAY' in s:return '7SJ6611'
 if 'VAJH13' in s or 'MASTER TRIP RELAY' in s:return 'VAJH13'
 if 'DIGITAL MF METER' in s or 'EM6400NG' in s:return 'EM6400NG'
 if 'DIGITAL VOLTMETER' in s:return 'VOLTMETER'
 if 'DIGITAL AMMETER' in s:return 'AMMETER'
 if 'MINIATURE CIRCUIT BREAKER' in s or re.search(r'\bMCB\b',s) or '5SL4206-7RC' in s:return 'MCB'
 return designation or 'UNMATCHED'

def extract_fixed_table(data,filename):
 pages=pdf_pages(data,filename); records=[]; feeders=[]; warnings=[]
 for page_no,page in enumerate(pages,1):
  if page_no!=1:continue
  words=page_words(page); feeder_type=text_in_band(words,440,520,410,430); feeder_designation=text_in_band(words,440,520,395,415); feeder_rating=text_in_band(words,440,520,430,450); wiring=text_in_band(words,440,520,450,465); feeder_quantity=text_in_band(words,440,520,360,380)
  if feeder_type or feeder_designation:feeders.append({'name':feeder_designation or feeder_type,'type':feeder_type,'designation':feeder_designation,'rating':feeder_rating,'wiring':wiring,'quantity':feeder_quantity or '1'})
  # Q10 VCB is above the standard equipment rows. This is the active
  # extractor; coordinates are from the rotated source MSLD after transform_word.
  q10_designation=text_in_band(words,275,320,460,480)
  if re.search(r'\\bQ10\\b',q10_designation,re.I):
   q10_desc=text_in_band(words,50,275,460,480)
   q10_details=text_in_band(words,320,615,465,515)
   records.append({'source_page':page_no,'description':q10_desc,'designation':'Q10','details':q10_details,'master_code':'VCB','quantity':1,'section':'MSLD equipment'})
  left=rows_by_designation(words,275,320,450,790)
  for i,(y,des) in enumerate(left):
   next_y=left[i+1][0] if i+1<len(left) else 790
   # T1-T3 is a two-line CT block in the fixed MSLD template. Capture the
   # complete technical-data block rather than only one visual row.
   row_top = y-18 if des.upper() == 'T1-T3' else y-9
   lo,hi=max(490,row_top),min(790,(y+next_y)/2)
   desc=text_in_band(words,50,275,lo,hi); details=text_in_band(words,320,615,lo,hi)
   if desc or details:records.append({'source_page':page_no,'description':desc,'designation':des,'details':details,'master_code':master_match(desc,details,des),'quantity':designation_qty(des),'section':'MSLD equipment'})
  for ymin,ymax in [(35,345),(350,725)]:
   right=rows_by_designation(words,840,880,ymin,ymax)
   for i,(y,des) in enumerate(right):
    next_y=right[i+1][0] if i+1<len(right) else ymax; lo,hi=max(ymin,y-8),min(ymax,(y+next_y)/2); desc=text_in_band(words,630,835,lo,hi); details=text_in_band(words,890,1175,lo,hi)
    if desc or details:records.append({'source_page':page_no,'description':desc,'designation':des,'details':details,'master_code':master_match(desc,details,des),'quantity':designation_qty(des),'section':'MSLD equipment'})
 return feeders,records,warnings

def extract_header_from_page(data,filename):
 pages=pdf_pages(data,filename)
 if not pages:return {}
 words=page_words(pages[0]); band=lambda a,b,c,d:text_in_band(words,a,b,c,d); drawing_band=band(980,1110,805,830); drawing=first(r'(G\d{4,}-[A-Z0-9]+-[A-Z0-9]+)',drawing_band,drawing_band); esd=first(r'\b(SED\d+)\b',drawing_band) or first(r'\b(SED\d+)\b',words_text(words))
 return {'client':band(345,440,782,792),'sales_ref':band(910,960,782,793),'drawing':drawing,'esd':esd,'wo':band(905,960,793,805),'description':band(690,835,775,795),'qty':band(1000,1040,790,805),'master_singleline':band(690,835,800,815)}
def header(msld,dis,client,sales,drawing,esd,wo,prep,voltage):
 s=msld+'\n'+dis
 return {'client':client.strip() or first(r'Client\s*:?\s*([^\n]+)',s),'sales_ref':sales.strip() or first(r'Sales\s*Ref\.?\s*:?\s*([^\n]+)',s),'drawing':drawing.strip() or first(r'Drg\.?\s*No\.?\s*:?\s*([^\n]+)',s),'esd':esd.strip() or first(r'\b(SED\d+)\b',s) or first(r'ESD\s*No\.?\s*:?\s*([^\n]+)',s),'wo':wo.strip() or first(r'W\.?O\.?\s*No\.?\s*:?\s*([^\n]+)',s),'prep_by':prep.strip(),'voltage':norm_voltage(voltage)}

def clean_value(value):
    if not value:
        return ''
    value = value.replace('\n', ' ')
    value = re.sub(r'\s+', ' ', value)
    return value.strip()

def first_match(pattern, text, flags=re.IGNORECASE):
    if not text:
        return ''
    m = re.search(pattern, text, flags)
    return clean_value(m.group(1)) if m else ''

def extract_ct_ratio(ct_text):
    m = re.search(r'\bCTR\s*:\s*([0-9]+(?:\.[0-9]+)?(?:-[0-9]+(?:\.[0-9]+)?)?)\s*/', ct_text or '', re.I)
    return m.group(1) if m else ''

def extract_ct_secondary_current(ct_text):
    m = re.search(r'\bCTR\s*:\s*[0-9]+(?:\.[0-9]+)?(?:-[0-9]+(?:\.[0-9]+)?)?\s*/\s*([0-9]+(?:\.[0-9]+)?)\s*A', ct_text or '', re.I)
    return f'{m.group(1)}A' if m else ''

def extract_ct_core_lines(ct_text):
    labeled = re.findall(r'CORE\s*(\d+)\s*:\s*(.*?)(?=,\s*CORE\s*\d+\s*:|$)', ct_text or '', re.I)
    if labeled:
        return [clean_value(v) for _, v in sorted(labeled, key=lambda x:int(x[0]))]
    values=[]
    # The fixed MSLD extraction normalizes line breaks, so multiple CTR
    # entries can arrive on the same line. Stop each core at the next CTR
    # (or STC) instead of consuming the whole technical-data string.
    for m in re.finditer(r'\bCTR\s*:\s*(.*?)(?=\s+CTR\s*:|\s+STC\s*:|\s+SHORT\s+TIME\s+CURRENT\s*:|$)', ct_text or '', re.I):
        value=clean_value(m.group(1))
        if value: values.append(value)
    return values

def extract_ct_core_count(ct_text):
    explicit=first_match(r'NO\.?\s*OF\s*CORES\s*[:\-]?\s*(\d+)',ct_text)
    if explicit: return int(explicit)
    return len(extract_ct_core_lines(ct_text)) or 1

def generate_ct_code(ct_text):
    ratio=extract_ct_ratio(ct_text)
    secondary=extract_ct_secondary_current(ct_text)
    count=extract_ct_core_count(ct_text)
    return f'CT{ratio}{count}C-{secondary}' if ratio and secondary else ''

def extract_ct_type(ct_text):
    for pattern in [r'(EPOXY\s+CAST\s+RESIN\s*\(\s*WOUND\s+TYPE\s*\))',r'(WOUND\s+TYPE)',r'(WINDOW\s+TYPE)']:
        value=first_match(pattern,ct_text)
        if value:return value.upper()
    return ''

def build_ct_description(ct_text):
    t=extract_ct_type(ct_text)
    # Fixed CT wording required by the BOM template.
    return f'CURRENT TRANSFORMER EPOXY CAST RESIN ({t})' if t else 'CURRENT TRANSFORMER EPOXY CAST RESIN (WOUND TYPE)'

def build_ctr_line(ct_text):
    n=extract_ct_core_count(ct_text)
    word={1:'SINGLE',2:'TWO',3:'THREE'}.get(n,str(n))
    return f'{word} CORE CT'

def dis_search_text(dis_text):
    return re.sub(r'\s+', ' ', dis_text or '').strip()

def dis_field(dis_text, code, label=''):
    # The DIS is a fixed template. Use the numbered field as the primary
    # anchor because PDF text extraction can change spacing or wording.
    text = dis_search_text(dis_text)
    pattern = rf'{re.escape(code)}\s*(?:{label}\s*)?:?\s*(.*?)(?=\s+\d{{1,2}}\.\d{{2}}\.\d{{2}}\s+|$)'
    m = re.search(pattern, text, re.IGNORECASE)
    return clean_value(m.group(1)) if m else ''

def extract_annexure_make(dis_text, equipment):
    """Return the common CT/PT make list from DIS Annexure 1."""
    text = dis_search_text(dis_text)
    section_match = re.search(
        r'ANNEXURE\s*[-:]?\s*1\b(.*?)(?=ANNEXURE\s*[-:]?\s*[2-9]\b|$)',
        text, re.IGNORECASE | re.DOTALL
    )
    section = section_match.group(1) if section_match else text
    # The approved description lists all three makes together for both CT and PT.
    required = ['ECS', 'NPT', 'PRAGATI']
    present = [name for name in required if re.search(rf'\b{re.escape(name)}\b', section, re.IGNORECASE)]
    if len(present) == 3:
        return 'ECS/NPT/PRAGATI'
    # Annexure text extraction may lose part of a table. Keep the requested
    # standard list consistent rather than outputting a misleading single make.
    return 'ECS/NPT/PRAGATI'



def extract_ct_make(dis_text):
    text = dis_search_text(dis_text)
    # Fixed DIS make may appear as "PRAGATI MAKE", "PRAGATI/ECS MAKE",
    # "CT MAKE: PRAGATI", or "MAKE OF CT: PRAGATI". Do not depend on one
    # exact PDF text layout.
    patterns = [
        r'\b(PRAGATI(?:\s*/\s*[A-Z][A-Z0-9.&\-]*)+)\s+MAKE\b',
        r'\b(PRAGATI(?:\s*/\s*[A-Z][A-Z0-9.&\-]*)+)\b',
        r'\b(PRAGATI)\s+MAKE\b',
        r'(?:CT\s*/?\s*MAKE|CT/PT\s+MAKE|MAKE\s+OF\s+CT)\s*[:\-]?\s*([A-Za-z][A-Za-z0-9.&/\-]*(?:\s*/\s*[A-Za-z][A-Za-z0-9.&/\-]*)*)',
        r'\bMAKE\s*[:\-]\s*([A-Za-z][A-Za-z0-9.&/\-]*(?:\s*/\s*[A-Za-z][A-Za-z0-9.&/\-]*)*)',
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            value = re.sub(r'\s*/\s*', '/', clean_value(m.group(1)))
            if value and value.upper() not in {'OF','CT','PT','MAKE'}:
                return value
    return ''

def extract_rated_voltage(dis_text):
    value = dis_field(dis_text, '1.03.00', r'(?:Rated\s+(?:operational\s+)?voltage)')
    m = re.search(r'([0-9]+(?:\.[0-9]+)?)\s*(KV|kV)', value or '', re.IGNORECASE)
    if m:
        return f'{m.group(1)}kV'
    text = dis_search_text(dis_text)
    m = re.search(r'Rated\s+(?:operational\s+)?voltage\s*:?\s*([0-9]+(?:\.[0-9]+)?)\s*(KV|kV)', text, re.IGNORECASE)
    return f'{m.group(1)}kV' if m else ''

def extract_frequency(dis_text):
    value = dis_field(dis_text, '1.02.00', r'Main\s+System')
    text = value or dis_search_text(dis_text)
    m = re.search(r'([0-9]+(?:\.[0-9]+)?)\s*Hz', text, re.IGNORECASE)
    return f'{m.group(1)}Hz' if m else ''

def extract_bil(dis_text):
    values = []
    for code in ('1.04.00','1.06.00','1.07.00'):
        value = dis_field(dis_text, code)
        m = re.search(r'([0-9]+(?:\.[0-9]+)?)', value)
        if not m:
            return ''
        values.append(m.group(1))
    return '/'.join(values) + 'KVp'

def extract_panel_suitability(dis_text):
    # Source is DIS 2.01.00 "Type of switchboard", not 2.02.01 Location.
    # Example fixed-template value:
    # 8BK80(1000mm)+3AH3 VCB
    value = dis_field(dis_text, '2.01.00', r'Type\s+of\s+switchboard')
    if value:
        m = re.search(r'\b(8BK80)\s*\(?\s*(\d{3,4})\s*mm\s*\)?', value, re.I)
        if m:
            return f'{m.group(1).upper()}-{m.group(2)}mm WIDTH PANEL'
    # Fallback: search the whole DIS only if the numbered field was not
    # extracted cleanly.
    text = dis_search_text(dis_text)
    m = re.search(r'\b(8BK80)\s*\(?\s*(\d{3,4})\s*mm\s*\)?', text, re.I)
    if m:
        return f'{m.group(1).upper()}-{m.group(2)}mm WIDTH PANEL'
    return ''

def extract_pt_core_lines(pt_text):
    """Extract only each PT core's ratio, burden and accuracy class; never absorb adjacent MSLD equipment."""
    text = clean_value(pt_text)
    # The PT core table is followed by other equipment rows in flattened PDF text.
    # Extract bounded engineering tokens instead of consuming everything until the next CORE label.
    core_matches = re.findall(
        r'(?:\b(?:CORE\s*\d+\s*:\s*)?)'
        r'((?:\d+(?:\.\d+)?\s*kV\s*/\s*(?:√|\bSQRT\b)?\s*3\s*/\s*'
        r'\d+(?:\.\d+)?\s*/\s*(?:√|\bSQRT\b)?\s*3\s*,?\s*)?'
        r'\d+(?:\.\d+)?\s*VA\s*,?\s*CL\.?\s*-?\s*[A-Z0-9.]+)',
        text, re.IGNORECASE
    )
    result = []
    for value in core_matches:
        value = clean_value(value).rstrip(' ,;')
        if value and value not in result:
            result.append(value)
    return result



def extract_pt_fuse_rating(msld_text, dis_text):
    text = clean_value((msld_text or '') + ' ' + (dis_text or ''))
    patterns = [
        r'(?:IN[- ]?BUILT\s+)?PRIMARY\s+FUSE(?:\s+OF|\s+RATING)?\s*[:=]?\s*(\d+(?:\.\d+)?)\s*A\b',
        r'FUSE\s+(?:RATING|CURRENT)\s*[:=]?\s*(\d+(?:\.\d+)?)\s*A\b',
    ]
    for pattern in patterns:
        value = first_match(pattern, text)
        if value:
            return f'{value}A'
    return ''


def extract_pt_panel_suitability(msld_text, dis_text=''):
    """Use the exact same panel extraction source/rule as CT: DIS 2.01.00."""
    return extract_panel_suitability(dis_text)



def pt_spec(pt_text, msld_text, dis_text):
    """Format PT description from MSLD/DIS using the approved fixed layout."""
    source = clean_value((pt_text or '') + ' ' + (msld_text or ''))
    rated_voltage = extract_rated_voltage(dis_text)
    if not rated_voltage:
        rated_voltage = first_match(r'\b([0-9]+(?:\.[0-9]+)?\s*kV)\b', source)
        rated_voltage = re.sub(r'\s+', '', rated_voltage)
    # PT make is sourced from DIS.
    make = extract_annexure_make(dis_text, 'PT') or extract_ct_make(dis_text)
    if not make:
        dis_source = dis_search_text(dis_text)
        make = first_match(r'(?:Manufacturer(?:\s+name)?|Make\s+of\s+(?:PT|potential\s+transformer))\s*[:=\-]?\s*([A-Z][A-Z0-9.&/\-]*(?:\s*/\s*[A-Z][A-Z0-9.&/\-]*)?)', dis_source)
        if not make:
            make = first_match(r'\b([A-Z][A-Z0-9.&/\-]*(?:\s*/\s*[A-Z][A-Z0-9.&/\-]*)?)\s+MAKE\b', dis_source)
        make = re.sub(r'\s*/\s*', '/', make)
    # Preserve transformer type from MSLD when it is present.
    transformer_type = first_match(
        r'(POTENTIAL\s+TRANSFORMERS?\s+IN\s+.+?(?:DRAWOUT\s+TYPE|TYPE))',
        source
    )
    if not transformer_type:
        transformer_type = 'POTENTIAL TRANSFORMERS IN CAST RESIN EPOXY CASE (DRAWOUT TYPE)'
    transformer_type = clean_value(transformer_type).upper()
    cores = extract_pt_core_lines(pt_text) or extract_pt_core_lines(msld_text)
    core_count = len(cores) or 3
    core_word = {1: 'SINGLE', 2: 'TWO', 3: 'THREE'}.get(core_count, str(core_count))
    frequency = extract_frequency(dis_text)
    bil = extract_bil(dis_text)
    if bil:
        bil = bil.upper()
    panel = extract_pt_panel_suitability(msld_text, dis_text)
    lines = [
        f'PT {rated_voltage}' if rated_voltage else 'PT',
        f'{make.upper()} MAKE' if make else 'MAKE',
        transformer_type,
        '- SINGLE PHASE WITH IN BUILT PRIMARY FUSE OF 3.15A',
        f'{core_word} CORE PT, AS PER IS/IEC STANDARD',
        'AS GIVEN BELOW:-',
    ]
    for index, value in enumerate(cores, 1):
        lines.append(f'- CORE {index}: {value}')
    lines.extend([
        f'- RATED FREQUENCY: {frequency}' if frequency else '- RATED FREQUENCY:',
        '- VF: 1.2COUNT/1.9 FOR 8 HOURS',
        f'- BIL: {bil}' if bil else '- BIL:',
        'INSULATION CLASS:- B',
        f'SUITABLE FOR {panel}' if panel else 'SUITABLE FOR',
    ])
    return lines

def ct_spec(ct_text, dis_text):
    ct_code=generate_ct_code(ct_text)
    ct_make=extract_annexure_make(dis_text, 'CT') or extract_ct_make(dis_text)
    # Retain the legacy default only if Annexure 1 and the DIS text expose no CT make.
    if not ct_make:
        ct_make = ''
    ct_description=build_ct_description(ct_text)
    rated_voltage=extract_rated_voltage(dis_text)
    frequency=extract_frequency(dis_text)
    stc=first_match(r'(?:SHORT\s+TIME\s+CURRENT|STC)\s*:?\s*(.*?)(?=\s+CTR\s*:|\s+CORE\s*\d+\s*:|\s+STC\s*:|\s*$)',ct_text or '')
    bil=extract_bil(dis_text)
    cores=extract_ct_core_lines(ct_text)
    panel=extract_panel_suitability(dis_text)
    lines=[
        ct_code or 'CT',
        f'{ct_make.upper()} MAKE' if ct_make else '',
        ct_description,
        '- SINGLE PHASE, AS PER IS/IEC STANDARD',
        f'- RATED VOLTAGE: {rated_voltage}' if rated_voltage else '- RATED VOLTAGE:',
        f'- RATED FREQUENCY: {frequency}' if frequency else '- RATED FREQUENCY:',
        f'- SHORT TIME CURRENT: {stc}' if stc else '- SHORT TIME CURRENT:',
        f'- BIL: {bil}' if bil else '- BIL:',
        'INSULATION CLASS:-B',
        'AS GIVEN BELOW:-',
        build_ctr_line(ct_text)
    ]
    for i,value in enumerate(cores,1):
        lines.append(f'- CORE {i}: {value}')
    lines.extend([
        f'SUITABLE FOR {panel}' if panel else 'SUITABLE FOR',
        'CT SECONDARY TERMINAL ON P2 SIDE'
    ])
    return lines


def build_rows(feeder_info,records,msld_text='',dis_text=''):
 feeder_name=feeder_info[0].get('designation') or feeder_info[0].get('name','') if feeder_info else ''; feeder_qty=feeder_info[0].get('quantity','1') if feeder_info else '1'; feeder_match=re.search(r'\d+',str(feeder_qty)); feeder_qty_num=int(feeder_match.group()) if feeder_match else 1; rows=[]
 for i,r in enumerate(records,1):
  relay_analysis = None
  auxiliary_relay_validation = None
  damping_resistor_validation = None
  if r['master_code']=='CT':
   ct_row_text = ' '.join([r.get('designation',''), r.get('description',''), r.get('details','')])
   spec_lines=ct_spec(ct_row_text,dis_text)
   spec='\n'.join(spec_lines)
   editable_fields=['INSULATION CLASS:-B','CT SECONDARY TERMINAL ON P2 SIDE']
   # Core count changes the position of the SUITABLE FOR line. Resolve
   # editable positions from their actual labels so 3-core CTs never mark
   # the SUITABLE FOR row as editable by mistake.
   editable_indices = []
   for editable_label in editable_fields:
    target = editable_label.split(' ON P2 SIDE')[0]
    line_index = next((idx for idx, line in enumerate(spec_lines)
                       if target in line), None)
    if line_index is not None:
     editable_indices.append(line_index)
  elif r['master_code']=='PT':
   pt_row_text = ' '.join([r.get('designation',''), r.get('description',''), r.get('details','')])
   spec_lines=pt_spec(pt_row_text, msld_text, dis_text)
   spec='\n'.join(spec_lines)
   editable_fields=['INSULATION CLASS:- B']
   editable_indices=[12]
  elif r['master_code']=='MCB':
   mcb_item = mcb_master_match(r.get('description',''), r.get('details',''), r.get('designation',''))
   if mcb_item:
    spec, spec_lines = mcb_spec(mcb_item)
   else:
    spec = r['description']+((' | '+r['details']) if r['details'] else '')
    spec_lines = [clean(spec)] if spec else []
   editable_fields=[]
   editable_indices=[]
  elif r['master_code']=='7SJ6611':
   # Relay validation must use only this equipment row. Passing the full MSLD
   # could borrow an MLFB from another section/row and create a relay BOM line
   # that is not actually specified for this designation.
   relay_source = ' '.join([r.get('designation',''), r.get('description',''), r.get('details','')])
   relay_analysis = analyze_7sj66(relay_source)
   # Never drop a detected relay row just because its MLFB is missing or
   # its chart-coded options conflict with the MSLD details. Preserve the
   # source text in the BOM so engineering details are not lost, and attach
   # validation feedback for the UI warning list.
   source_relay_text = clean_value(' | '.join(
    part for part in [r.get('description',''), r.get('details','')] if part
   ))
   relay_validation = relay_analysis.get('validation') or {}
   has_mlfb = bool(relay_analysis.get('fields', {}).get('mlfb', {}).get('value'))
   # Only validate/report a missing MLFB when this MSLD row explicitly
   # identifies a 7SJ66 relay. A generic numerical-relay label is not enough.
   has_7sj66_evidence = bool(re.search(r'\b7SJ66\w*\b', relay_source, re.IGNORECASE))
   if not has_mlfb and not has_7sj66_evidence:
    continue
   # When an MLFB is extracted, always show the structured description.
   # Validation errors/warnings stay separate and do not replace BOM details.
   if has_mlfb:
    spec = relay_analysis['description']
    spec_lines = spec.splitlines()
   else:
    # Keep source text only when the MSLD actually identifies a relay.
    spec = source_relay_text
    spec_lines = [spec] if spec else []
   editable_fields=[]
   editable_indices=[]
  elif r['master_code']=='E2_LED':
   spec_lines=[
    'LED',
    'PHILIPS/REPUTED MAKE LED 5W- TUBE FIXTURE',
    'SUITABLE FOR 240V AC, 50Hz AC SUPPLY',
    'SUITABLE FOR SURFACE MOUNTING'
   ]
   spec='\n'.join(spec_lines)
   editable_fields=[]
   editable_indices=[]
  elif r['master_code']=='LED':
   h789 = led_specs_for_h7_h8_h9(r.get('description',''), r.get('details',''), r.get('designation',''))
   if h789:
    # Split the source quantity across the colour-specific H7/H8/H9 rows.
    # Example: source qty 3 and three colours -> 1 each, total remains 3 per feeder.
    group_qty = int(r.get('quantity') or 1)
    split_count = len(h789)
    base_qty, remainder = divmod(group_qty, split_count)
    for part_index, part in enumerate(h789):
     part_lines=part['specification_lines']
     part_qty = base_qty + (1 if part_index < remainder else 0)
     rows.append({'sr':len(rows)+1,'specification':'\n'.join(part_lines),'specification_lines':part_lines,'editable_fields':[],'editable_indices':[],'designation':part['designation'],'feeder_name':feeder_name,'feeder_qty':feeder_qty,'total':part_qty*feeder_qty_num,'eqpt_qty':part_qty,'mpd':'','amd':'','master_code':'LED'})
    continue
   spec_lines, led_item = led_spec(r.get('description',''), r.get('details',''), r.get('designation',''))
   spec='\n'.join(spec_lines) if spec_lines else r['description']+((' | '+r['details']) if r['details'] else '')
   spec_lines=spec_lines or ([clean(spec)] if spec else [])
   editable_fields=[]
   editable_indices=[]
  else:
   spec=r['description']+((' | '+r['details']) if r['details'] else '')
   spec_lines=[clean(spec)] if spec else []
   editable_fields=[]
   editable_indices=[]
  rows.append({'sr':len(rows)+1,'specification':spec,'specification_lines':spec_lines,'editable_fields':editable_fields,'editable_indices':editable_indices,'designation':r['designation'],'feeder_name':feeder_name,'feeder_qty':feeder_qty,'total':(r['quantity'] or 1)*feeder_qty_num,'eqpt_qty':(r['quantity'] or 1),'mpd':'','amd':'','master_code':r['master_code'],'relay_validation':relay_analysis.get('validation') if relay_analysis else None})
  if r['master_code']=='MCB':
   mcb_source_text = clean_value(' '.join([r.get('description',''), r.get('details','')])).upper()
   has_aux_1no_1nc = bool(re.search(r'\b1\s*NO\s*\+\s*1\s*NC\b', mcb_source_text) and re.search(r'\bAUX(?:ILIARY)?\b|\bCONTACTS?\b', mcb_source_text))
   if has_aux_1no_1nc:
    mcb_qty = (r['quantity'] or 1)
    addon_item = load_mcb_addon_master()
    if addon_item and addon_item.get('discription'):
     addon_spec = addon_item['discription']
     addon_lines = [part.strip() for part in addon_spec.split(',') if part.strip()]
     rows.append({'sr':len(rows)+1,'specification':'\n'.join(addon_lines),'specification_lines':addon_lines,'editable_fields':[],'editable_indices':[],'designation':str(r.get('designation',''))+'A','feeder_name':feeder_name,'feeder_qty':feeder_qty,'total':mcb_qty*feeder_qty_num,'eqpt_qty':mcb_qty,'mpd':'','amd':'','master_code':'MCB ADD-ON'})
 return rows,feeder_name,feeder_qty

def extract_from_files(msld_bytes,msld_name,dis_bytes,dis_name,client,sales,drawing,esd,wo,prep,voltage):
 msld_text=pdf_text(msld_bytes,msld_name); dis_text=pdf_text(dis_bytes,dis_name); same=bool(msld_text.strip()) and re.sub(r'\s+','',msld_text)==re.sub(r'\s+','',dis_text); feeder_info,records,warnings=extract_fixed_table(msld_bytes,msld_name)
 if same:warnings.append('The same PDF was supplied in both MSLD and DIS slots. It was processed once; DIS was used as the validation copy.')
 elif dis_text.strip():warnings.append('DIS supplied: used as the fixed specification source; MSLD remains the primary project-data source.')
 rows,feeder_name,feeder_qty=build_rows(feeder_info,records,msld_text,dis_text)
 for row in rows:
  validation=row.get('relay_validation')
  if validation:
   relay_label='7SJ66 relay '+str(row.get('designation',''))
   warnings.extend([relay_label+': '+message for message in validation.get('errors',[])])
   warnings.extend([relay_label+': '+message for message in validation.get('warnings',[])])
 h=header(msld_text,dis_text,client,sales,drawing,esd,wo,prep,voltage); ph=extract_header_from_page(msld_bytes,msld_name)
 for key in ('client','sales_ref','drawing','wo'):
  if ph.get(key):h[key]=ph[key]
 if ph.get('esd'):h['esd']=ph['esd']
 source=msld_text+'\n'+dis_text; q=first(r'\bQty\.\s*:\s*(\d+x?)\b',source) or first(r'\bQTY\.\s*:\s*(\d+x?)\b',source) or ph.get('qty',''); v=norm_voltage(voltage) or first(r'\b(6\.6|11|33)\s*KV\b',source); description=f'{v}kV SWITCHBOARD' if v else 'SWITCHBOARD'
 if not rows:warnings.append('No equipment rows were confidently extracted from the fixed MSLD table.')
 unmatched=[r['designation'] for r in records if r['master_code']=='UNMATCHED']
 if unmatched:warnings.append('Master Data match not available for: '+', '.join(unmatched)+'. Source description was preserved; no engineering item was guessed.')
 unmatched_mcb=[r['designation'] for r in records if r['master_code']=='MCB' and not mcb_master_match(r.get('description',''),r.get('details',''),r.get('designation',''))]
 if unmatched_mcb:warnings.append('MCB specifications could not be fully verified against the master DISCRIPTION for: '+', '.join(unmatched_mcb)+'. Source text was preserved; verify before issuing the BOM.')
 return {'header':h,'document_no':'SI EA/CS/FR/EG/015','revision':'1.0','effective_date':'17/07/2026','created_by':'EA CS ENGG','description':description,'rows':rows,'feeder_name':feeder_name,'feeder_qty':feeder_qty,'qty':q,'warnings':warnings,'feeder_details':feeder_info,'same_input_file':same}


def extract_multiple_from_files(msld_files, dis_bytes, dis_name, client, sales, drawing, esd, wo, prep, voltage):
    """Build one BOM across many MSLDs, using one common DIS as the specification source."""
    dis_text = pdf_text(dis_bytes, dis_name)
    all_rows = []
    feeders = []
    warnings = []
    header_source = ""
    first_ph = {}
    qty_text = ""
    for idx, item in enumerate(msld_files):
        msld_bytes, msld_name = item
        msld_text = pdf_text(msld_bytes, msld_name)
        if idx == 0:
            header_source = msld_text
            first_ph = extract_header_from_page(msld_bytes, msld_name)
            source = msld_text + "\n" + dis_text
            qty_text = first(r'\bQty\.\s*:\s*(\d+x?)\b', source) or first(r'\bQTY\.\s*:\s*(\d+x?)\b', source) or first_ph.get('qty', '')
        feeder_info, records, file_warnings = extract_fixed_table(msld_bytes, msld_name)
        if not feeder_info:
            warnings.append(f"{msld_name}: feeder information could not be confidently extracted.")
            feeder_info = [{'name': os.path.splitext(os.path.basename(msld_name))[0], 'designation': os.path.splitext(os.path.basename(msld_name))[0], 'quantity': '1'}]
        feeder_name = feeder_info[0].get('designation') or feeder_info[0].get('name') or f"MSLD {idx + 1}"
        feeder_qty_text = feeder_info[0].get('quantity', '1')
        qty_match = re.search(r'\d+', str(feeder_qty_text))
        feeder_qty_num = int(qty_match.group()) if qty_match else 1
        feeders.append({'name': str(feeder_name), 'qty': str(feeder_qty_text), 'quantity': feeder_qty_num, 'source_file': msld_name})
        built, _, _ = build_rows(feeder_info, records, msld_text, dis_text)
        for row in built:
            validation = row.get('relay_validation')
            if validation:
                relay_label = str(msld_name) + ': 7SJ66 relay ' + str(row.get('designation', ''))
                warnings.extend([relay_label + ': ' + message for message in validation.get('errors', [])])
                warnings.extend([relay_label + ': ' + message for message in validation.get('warnings', [])])
        for row in built:
            row['_feeder_index'] = idx
            all_rows.append(row)
        warnings.extend([f"{msld_name}: {w}" for w in file_warnings])
        unmatched = [r['designation'] for r in records if r['master_code'] == 'UNMATCHED']
        if unmatched:
            warnings.append(f"{msld_name}: Master Data match not available for: " + ', '.join(unmatched) + ". Source description was preserved; no engineering item was guessed.")
        unmatched_mcb = [r['designation'] for r in records if r['master_code'] == 'MCB' and not mcb_master_match(r.get('description',''), r.get('details',''), r.get('designation',''))]
        if unmatched_mcb:
            warnings.append(f"{msld_name}: MCB specifications could not be fully verified against the master DISCRIPTION for: " + ', '.join(unmatched_mcb) + ". Source text was preserved; verify before issuing the BOM.")
    # Merge identical equipment only when its normalized specification and category match.
    merged = {}
    for row in all_rows:
        idx = row.pop('_feeder_index')
        spec_lines = row.get('specification_lines') or []
        identity_text = row.get('specification') or '\n'.join(spec_lines)
        key = (str(row.get('master_code', '')).strip().upper(), re.sub(r'\s+', ' ', identity_text).strip().casefold())
        if key not in merged:
            entry = dict(row)
            entry['sr'] = len(merged) + 1
            entry['feeder_quantities'] = [0] * len(feeders)
            entry['designation_by_feeder'] = [''] * len(feeders)
            entry['total'] = 0
            entry['eqpt_qty'] = 0
            merged[key] = entry
        entry = merged[key]
        qty = row.get('eqpt_qty')
        if qty is None:
            qty = 1
        try:
            qty = int(qty)
        except (ValueError, TypeError):
            qty = 1
        # Feeder columns show equipment quantity per typical panel. Total is
        # calculated only after merging, using that feeder's panel multiplier.
        entry['feeder_quantities'][idx] += qty
        designation = str(row.get('designation', '') or '').strip()
        if designation:
            existing = entry['designation_by_feeder'][idx]
            if isinstance(existing, list):
                if designation not in existing:
                    existing.append(designation)
            elif existing and existing != designation:
                entry['designation_by_feeder'][idx] = [existing, designation]
            else:
                entry['designation_by_feeder'][idx] = [designation]
        entry['eqpt_qty'] += qty
    rows = list(merged.values())
    for row in rows:
        quantities = row.get('feeder_quantities') or []
        per_feeder_quantities = []
        total = 0
        for i, feeder in enumerate(feeders):
            try:
                equipment_qty = int(quantities[i] or 0) if i < len(quantities) else 0
            except (TypeError, ValueError):
                equipment_qty = 0
            try:
                feeder_qty = int(feeder.get('quantity') or 1)
            except (TypeError, ValueError):
                feeder_qty = 1
            per_feeder_quantities.append(equipment_qty)
            total += equipment_qty * max(1, feeder_qty)
        row['feeder_quantities'] = per_feeder_quantities
        row['total'] = total
    def equipment_order(row):
        code = str(row.get('master_code', '')).strip().upper()
        spec = str(row.get('specification', '')).upper()
        text = ' '.join([code, spec, str(row.get('designation', '')).upper()])
        # Keep corresponding equipment from all MSLDs together in a stable,
        # engineering-oriented category order.
        if code == 'CT' or re.search(r'\bCURRENT TRANSFORMER\b|\bC\.T\.\b', text):
            rank = 0
        elif code in ('PT', 'VT') or re.search(r'\b(POTENTIAL|VOLTAGE) TRANSFORMER\b|\bP\.T\.\b', text):
            rank = 1
        elif code in ('LA', 'SA') or 'SURGE ARRESTER' in text:
            rank = 2
        elif code in ('CB', 'VCB', 'BREAKER') or 'CIRCUIT BREAKER' in text:
            rank = 3
        elif code.startswith('MCB') or re.search(r'\bMCB\b', text):
            rank = 4
        elif code in ('LED', 'LAMP') or 'INDICATION LAMP' in text or re.search(r'\bLED\b', text):
            rank = 5
        else:
            rank = 6
        return (rank, code, re.sub(r'\s+', ' ', str(row.get('specification', ''))).casefold(), str(row.get('designation', '')).casefold())
    rows.sort(key=equipment_order)
    for i, row in enumerate(rows, 1):
        row['sr'] = i
        designations = []
        for values in row.pop('designation_by_feeder', []):
            if isinstance(values, str):
                values = [values]
            for value in values or []:
                value = str(value).strip()
                if value and value not in designations:
                    designations.append(value)
        row['designation'] = ', '.join(designations)
        row['feeder_name'] = ''
        row['feeder_qty'] = ''
    h = header(header_source, dis_text, client, sales, drawing, esd, wo, prep, voltage)
    for key in ('client', 'sales_ref', 'drawing', 'wo'):
        if first_ph.get(key):
            h[key] = first_ph[key]
    if first_ph.get('esd'):
        h['esd'] = first_ph['esd']
    source = header_source + '\n' + dis_text
    v = norm_voltage(voltage) or first(r'\b(6\.6|11|33)\s*KV\b', source)
    description = f'{v}kV SWITCHBOARD' if v else 'SWITCHBOARD'
    if not rows:
        warnings.append('No equipment rows were confidently extracted from the supplied MSLD files.')
    if dis_text.strip():
        warnings.append('One common DIS supplied: used as the specification source for all MSLDs.')
    return {
        'header': h, 'document_no': 'SI EA/CS/FR/EG/015', 'revision': '1.0',
        'effective_date': '17/07/2026', 'created_by': 'EA CS ENGG',
        'description': description, 'rows': rows, 'feeders': feeders,
        'feeder_name': feeders[0]['name'] if feeders else '',
        'feeder_qty': feeders[0]['qty'] if feeders else '',
        'qty': qty_text, 'warnings': list(dict.fromkeys(warnings)),
        'feeder_details': feeders, 'same_input_file': False
    }

@app.post('/api/bom/check-customers')
async def check_msld_customers(
    mslds: list[UploadFile] = File(...),
    dis: UploadFile = File(...),
    client: str = Form(''),
    wo: str = Form(''),
    voltage: str = Form(''),
    user: str = Depends(current_user)
):
    """Check MSLD and common DIS header values before BOM generation."""
    results = []
    for upload in mslds:
        data = await upload.read()
        filename = upload.filename or 'MSLD.pdf'
        extracted = extract_header_from_page(data, filename)
        source_text = pdf_text(data, filename)
        customer = clean_value(extracted.get('client', '')).strip(' :-')
        if not customer:
            customer = first(r'Client\s*[:\-]*\s*([^\n]+)', source_text).strip(' :-') or first(r'Customer\s*Name\s*[:\-]*\s*([^\n]+)', source_text).strip(' :-')
        work_order = clean_value(extracted.get('wo', '')).strip(' :-')
        if not work_order:
            work_order = first(r'W\.?\s*O\.?\s*(?:No\.?)?\s*[:\-]\s*([^\n]+)', source_text).strip(' :-')
        voltage_match = first(r'\b(6\.6|11|33)\s*k?V\b', source_text)
        results.append({'filename': filename, 'customer': clean_value(customer), 'work_order': clean_value(work_order), 'voltage': clean_value(voltage_match)})
    dis_bytes = await dis.read()
    dis_name = dis.filename or 'DIS.pdf'
    dis_header = extract_header_from_page(dis_bytes, dis_name)
    dis_text = pdf_text(dis_bytes, dis_name)
    # DIS is a two-column footer table. Parse the Client/W.O. values from
    # words on the same visual row, stopping before the right-hand columns.
    dis_words = page_words(pdf_pages(dis_bytes, dis_name)[0]) if pdf_pages(dis_bytes, dis_name) else []
    def footer_value(label_pattern):
        # Read only words to the right of the requested label on the same visual row.
        labels = [
            w for w in dis_words
            if re.fullmatch(label_pattern, w['text'].strip(' :.-'), re.IGNORECASE)
        ]
        for label_word in labels:
            row = sorted(
                [w for w in dis_words if abs(w['y'] - label_word['y']) <= 8 and w['x'] > label_word['x']],
                key=lambda w: w['x']
            )
            picked = []
            for word in row:
                token = word['text'].strip(' :.-')
                if not token:
                    continue
                if re.match(
                    r'^(?:Sales|Project|W\.?O\.?|Item|Qty|Description|Sh\.?|Work|Details|Drg\.?|Date|Prep|Ckd|Client)\b',
                    token,
                    re.IGNORECASE
                ):
                    break
                picked.append(token)
            value = clean_value(' '.join(picked)).strip(' :-')
            if value:
                return value
        return ''

    dis_customer = footer_value(r'^Client$')
    dis_wo = footer_value(r'^(?:W\.?O\.?|Work\s*Order)$')
    if not dis_customer:
        dis_customer = clean_value(dis_header.get('client', '')).strip(' :-')
    if not dis_wo:
        dis_wo = clean_value(dis_header.get('wo', '')).strip(' :-')
    dis_voltage = first(r'\b(6\.6|11|33)\s*k?V\b', dis_text)
    def distinct(values):
        return list(dict.fromkeys(re.sub(r'\s+', ' ', str(v)).strip() for v in values if str(v or '').strip()))
    customers = distinct([x['customer'] for x in results])
    work_orders = distinct([x['work_order'] for x in results])
    voltages = distinct([x['voltage'] for x in results])
    mismatches = []
    if len({x.casefold() for x in customers}) > 1:
        mismatches.append('The uploaded MSLD PDFs contain different customer names.')
    if len({x.casefold() for x in work_orders}) > 1:
        mismatches.append('The uploaded MSLD PDFs contain different work order numbers.')
    if len({x.casefold() for x in voltages}) > 1:
        mismatches.append('The uploaded MSLD PDFs contain different voltage values.')
    entered = {'customer': clean_value(client), 'work_order': clean_value(wo), 'voltage': clean_value(voltage)}
    for label, values, dis_value, entered_value in [
        ('Customer name', customers, dis_customer, entered['customer']),
        ('Work order number', work_orders, dis_wo, entered['work_order']),
        ('Voltage', voltages, dis_voltage, entered['voltage'])
    ]:
        all_values = distinct(values + ([dis_value] if dis_value else []) + ([entered_value] if entered_value else []))
        if label == 'Voltage':
            all_values = distinct([norm_voltage(value) or value for value in all_values])
        if len({x.casefold() for x in all_values}) > 1:
            mismatches.append(f'{label} differs between entered details, MSLD and/or DIS.')

    # Do not silently proceed when a PDF header field could not be extracted.
    # The existing verification modal displays these messages and lets the user
    # review the extracted values before choosing whether to continue.
    for item in results:
        if not item['work_order']:
            mismatches.append(f"Work order number could not be read from MSLD: {item['filename']}.")
        if not item['voltage']:
            mismatches.append(f"Voltage could not be read from MSLD: {item['filename']}.")
    if not dis_wo:
        mismatches.append(f"Work order number could not be read from DIS: {dis_name}.")
    if not dis_voltage:
        mismatches.append(f"Voltage could not be read from DIS: {dis_name}.")
    return {
        'mslds': results, 'customers': customers, 'different_customers': len({x.casefold() for x in customers}) > 1,
        'unknown_customer_files': [x['filename'] for x in results if not x['customer']],
        'mismatches': list(dict.fromkeys(mismatches)),
        'dis': {'filename': dis_name, 'customer': dis_customer, 'work_order': dis_wo, 'voltage': clean_value(dis_voltage)}
    }

@app.post('/api/bom/preview-multiple')
async def preview_multiple(
    client: str = Form(''), sales_ref: str = Form(''), drawing: str = Form(''),
    esd: str = Form(''), wo: str = Form(''), prep_by: str = Form(''),
    voltage: str = Form(''), mslds: list[UploadFile] = File(...),
    dis: UploadFile = File(...), user: str = Depends(current_user)
):
    if not mslds:
        raise HTTPException(status_code=400, detail='Upload at least one MSLD PDF.')
    dis_name = dis.filename or 'DIS.pdf'
    dis_bytes = await dis.read()
    items = []
    filenames = []
    for upload in mslds:
        filename = upload.filename or 'MSLD.pdf'
        items.append((await upload.read(), filename))
        filenames.append(filename)
    result = extract_multiple_from_files(items, dis_bytes, dis_name, client, sales_ref, drawing, esd, wo, prep_by, voltage)
    import json
    with db() as conn:
        conn.execute(
            """INSERT INTO bom_history (username, customer_name, work_order_no, voltage, msld_files, dis_filename, row_count)
               VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)""",
            (user, str(client or result.get('header', {}).get('client', '')), str(wo or result.get('header', {}).get('wo', '')),
             str(voltage or ''), json.dumps(filenames), dis_name, len(result.get('rows') or []))
        )
        conn.commit()
    return result




class SiemensLookupRequest(BaseModel):
    mlfb: str


@app.post('/api/siemens/product-lookup')
def siemens_product_lookup(payload: SiemensLookupRequest, user: str = Depends(current_user)):
    """POC: live lookup of a supplied 7SJ66 MLFB on Siemens Industry Mall."""
    return lookup_siemens_product(payload.mlfb)

@app.get('/api/admin/bom-history')
def admin_bom_history(user: str = Depends(require_admin)):
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) AS total FROM bom_history").fetchone()['total']
        rows = conn.execute(
            """SELECT id, username, customer_name, work_order_no, voltage, msld_files,
                      dis_filename, row_count, created_at
               FROM bom_history ORDER BY created_at DESC LIMIT 500"""
        ).fetchall()
    return {'total_generated': total, 'history': rows}

def export_combined_book(payload):
    wb = Workbook()
    ws = wb.active
    ws.title = 'BOM'
    thin = Side(style='thin')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    header_data = payload.get('header', {})
    feeders = payload.get('feeders') or []
    rows = payload.get('rows') or []
    n = max(1, len(feeders))
    qty_col = 4
    feeder_start_col = 5
    total_col = feeder_start_col + n
    mpd_col, amd_col = total_col + 1, total_col + 2
    last_col = amd_col
    ws.page_setup.orientation = 'landscape'
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 1
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_margins.left = .25
    ws.page_margins.right = .25
    ws.page_margins.top = .45
    ws.page_margins.bottom = .4
    ws.sheet_view.showGridLines = False
    spec_width = max(36, min(78, max([max((len(line) for line in str(r.get('specification','')).splitlines()), default=0) for r in rows] + [36]) * 0.75))
    designation_width = max(14, min(26, max([len(str(r.get('designation',''))) for r in rows] + [14]) * 0.9))
    feeder_width = max(10, min(18, max([len(str(f.get('name',''))) for f in feeders] + [8]) * 0.9))
    widths = [9, spec_width, designation_width, 9] + [feeder_width] * n + [13, 7, 7]
    for col, width in enumerate(widths, 1):
        ws.column_dimensions[__import__('openpyxl').utils.get_column_letter(col)].width = width
    ws.merge_cells(start_row=1, start_column=1, end_row=2, end_column=min(3, last_col))
    ws.cell(1, 1, '-: Equipment List :-')
    ws.cell(1, 1).font = Font(name='Courier New', size=10)
    ws.cell(1, 1).alignment = Alignment(horizontal='center', vertical='center')
    meta_col = max(4, last_col - 2)
    ws.merge_cells(start_row=1, start_column=meta_col, end_row=2, end_column=last_col)
    ws.cell(1, meta_col, f'Doc.No.: {payload.get("document_no","SI EA/CS/FR/EG/015")}\nRev.No.: {payload.get("revision","1.0")}, Eff.Dt: {payload.get("effective_date","17/07/2026")}\nCreated By: {payload.get("created_by","EA CS ENGG")}')
    ws.cell(1, meta_col).font = Font(name='Arial', size=7)
    ws.cell(1, meta_col).alignment = Alignment(horizontal='right', vertical='top', wrap_text=True)
    ws.row_dimensions[1].height = 34
    ws.row_dimensions[2].height = 16
    for col, label in [(1,'EQPT.NO'),(2,'SPECIFICATION'),(3,'DESIGNATION'),(total_col,'TOTAL EQPT QTY.'),(mpd_col,'MPD'),(amd_col,'AMD')]:
        ws.cell(4, col, label)
    for col in [1,2,3,total_col,mpd_col,amd_col]:
        ws.merge_cells(start_row=4, start_column=col, end_row=6, end_column=col)
    ws.merge_cells(start_row=5, start_column=qty_col, end_row=6, end_column=qty_col)
    ws.cell(5,qty_col,'QTY.')
    ws.merge_cells(start_row=4, start_column=feeder_start_col, end_row=4, end_column=feeder_start_col+n-1)
    ws.cell(4,feeder_start_col,'FEEDER TYPICAL')
    for idx, feeder in enumerate(feeders):
        col=feeder_start_col+idx
        ws.cell(5,col,feeder.get('name',''))
        ws.cell(6,col,feeder.get('quantity') or feeder.get('qty') or 1)
    for r in range(4,7):
        for c in range(1,last_col+1):
            cell=ws.cell(r,c)
            cell.border=border
            cell.alignment=Alignment(horizontal='center',vertical='center',wrap_text=True)
            cell.font=Font(name='Courier New',size=8,bold=True)
    ws.row_dimensions[4].height=23
    ws.row_dimensions[5].height=18
    ws.row_dimensions[6].height=28
    for i,row in enumerate(rows[:64],7):
        quantities = row.get('feeder_quantities') or []
        vals=[row.get('sr',''),row.get('specification',''),row.get('designation','')]
        vals.append('')
        vals.extend(quantities[j] if j < len(quantities) and quantities[j] not in (0, '0', None, '') else '' for j in range(n))
        total_value = row.get('total', '')
        vals.extend(['' if total_value in (0, '0') else total_value, '', ''])
        for col,val in enumerate(vals,1):
            cell=ws.cell(i,col,val)
            cell.border=border
            cell.font=Font(name='Courier New',size=7)
            cell.alignment=Alignment(horizontal='center' if col!=2 else 'left',vertical='center',wrap_text=col==2)
        spec_lines = str(row.get('specification', '')).splitlines() or ['']
        chars_per_line = max(12, int(spec_width * 1.35))
        wrapped_lines = sum(max(1, (len(line) + chars_per_line - 1) // chars_per_line) for line in spec_lines)
        ws.row_dimensions[i].height = max(18, min(240, 10 * wrapped_lines + 6))
    for i in range(7+len(rows[:64]),70):
        for c in range(1,last_col+1):
            ws.cell(i,c).border=border
            ws.cell(i,c).font=Font(name='Courier New',size=7)
        ws.row_dimensions[i].height=15
    # Keep worksheet view focused on the equipment list. Document metadata is
    # printed in the page header/footer when the user presses Ctrl+P.
    ws.oddHeader.center.text = "&B Equipment List &B"
    ws.oddHeader.right.text = f"Doc.No.: {payload.get('document_no','SI EA/CS/FR/EG/015')}\\nRev.No.: {payload.get('revision','1.0')}  Eff.Dt: {payload.get('effective_date','17/07/2026')}"
    ws.oddHeader.right.size = 7
    ws.oddFooter.left.text = f"Client: {header_data.get('client','')}  |  Sales Ref: {header_data.get('sales_ref','')}\\nDescription: {payload.get('description','')}  |  WO: {header_data.get('wo','')}  |  Drawing: {header_data.get('drawing','')}"
    ws.oddFooter.center.text = f"Prepared by: {header_data.get('prep_by','')}  |  Qty: {payload.get('qty','')}  |  ESD: {header_data.get('esd','')}"
    ws.oddFooter.right.text = "Page &P of &N"
    ws.oddFooter.left.size = 7
    ws.oddFooter.center.size = 7
    ws.oddFooter.right.size = 7
    ws.print_area=f'A1:{__import__("openpyxl").utils.get_column_letter(last_col)}76'
    return wb

def export_combined_pdf(payload):
    out=io.BytesIO()
    doc=SimpleDocTemplate(out,pagesize=A4,rightMargin=5*mm,leftMargin=5*mm,topMargin=6*mm,bottomMargin=20*mm)
    styles=getSampleStyleSheet()
    title=ParagraphStyle('combinedtitle',parent=styles['Normal'],fontName='Helvetica-Bold',fontSize=6,leading=7,alignment=TA_CENTER)
    cell=ParagraphStyle('combinedcell',parent=styles['Normal'],fontName='Helvetica',fontSize=4.2,leading=4.8,spaceAfter=0,spaceBefore=0,wordWrap='CJK')
    center=ParagraphStyle('combinedcenter',parent=cell,alignment=TA_CENTER)
    head=ParagraphStyle('combinedhead',parent=cell,fontName='Helvetica-Bold',alignment=TA_CENTER,fontSize=4.0,leading=4.5)
    feeders=payload.get('feeders') or []
    rows=payload.get('rows') or []
    n=max(1,len(feeders))
    data=[[Paragraph('EQPT.NO',head),Paragraph('SPECIFICATION',head),Paragraph('DESIGNATION',head),''] + [Paragraph('FEEDER<br/>TYPICAL',head) if i==0 else '' for i in range(n)] + [Paragraph('TOTAL<br/>EQPT QTY.',head),Paragraph('MPD',head),Paragraph('AMD',head)]]
    data.append(['','','',Paragraph('QTY.',head)]+[Paragraph(str(f.get('name','')),head) for f in feeders]+['','',''])
    data.append(['','','','']+[Paragraph(str(f.get('quantity') or f.get('qty') or 1),center) for f in feeders]+['','',''])
    for row in rows[:64]:
        spec=str(row.get('specification','') or '').replace('&','&amp;').replace('<','&lt;').replace('>','&gt;').replace('\n','<br/>')
        qs=row.get('feeder_quantities') or []
        data.append([Paragraph(str(row.get('sr','')),center),Paragraph(spec,cell),Paragraph(str(row.get('designation','') or ''),center),Paragraph('',center)]+[Paragraph(str(qs[i] if i<len(qs) and qs[i] not in (0, '0', None, '') else ''),center) for i in range(n)]+[Paragraph('' if row.get('total') in (0, '0') else str(row.get('total') or ''),center),'',''])
    available=200*mm
    fixed=[8*mm,66*mm,25*mm,9*mm,12*mm,10*mm,10*mm]
    # Allocate a little more space to long feeder labels, while keeping the
    # complete table inside landscape A4. Paragraph cells wrap and expand rows.
    remaining=max(8*mm, available-sum(fixed))
    feeder_labels=[len(str(f.get('name',''))) for f in feeders]
    weights=[max(1.0,min(2.0,length/10.0)) for length in feeder_labels]
    weight_sum=sum(weights) or n
    feeder_widths=[remaining*w/weight_sum for w in weights] if feeders else [remaining]
    widths=[fixed[0],fixed[1],fixed[2],fixed[3]]+feeder_widths+[fixed[4],fixed[5],fixed[6]]
    tbl=Table(data,colWidths=widths,rowHeights=[18,14,20]+[None]*(len(data)-3),repeatRows=3)
    spans=[('SPAN',(0,0),(0,2)),('SPAN',(1,0),(1,2)),('SPAN',(2,0),(2,2)),('SPAN',(3,1),(3,2)),('SPAN',(4,0),(3+n,0)),('SPAN',(4+n,0),(4+n,2)),('SPAN',(5+n,0),(5+n,2)),('SPAN',(6+n,0),(6+n,2))]
    # A compact first-row group label and feeder-specific second/third header rows.
    tbl.setStyle(TableStyle(spans+[('GRID',(0,0),(-1,-1),0.45,colors.black),('BACKGROUND',(0,0),(-1,2),colors.HexColor('#d9e7f0')),('VALIGN',(0,0),(-1,-1),'MIDDLE'),('ALIGN',(0,0),(-1,2),'CENTER'),('LEFTPADDING',(0,0),(-1,-1),2),('RIGHTPADDING',(0,0),(-1,-1),2),('TOPPADDING',(0,0),(-1,-1),1),('BOTTOMPADDING',(0,0),(-1,-1),1)]))
    story=[Paragraph('-: Equipment List :-',title),Spacer(1,3*mm),tbl]
    h=payload.get('header',{})
    def footer(canvas, doc_obj):
        canvas.saveState()
        w,hp=doc_obj.pagesize
        y=8*mm
        canvas.setFont('Helvetica',6.5)
        canvas.drawString(6*mm,y+7*mm,f"Client: {h.get('client','')}  Sales Ref: {h.get('sales_ref','')}")
        canvas.drawString(6*mm,y+4*mm,f"Description: {payload.get('description','')}  WO: {h.get('wo','')}  Drawing: {h.get('drawing','')}")
        canvas.drawString(6*mm,y+1*mm,f"Prepared by: {h.get('prep_by','')}  Qty: {payload.get('qty','')}  ESD: {h.get('esd','')}")
        canvas.restoreState()
    doc.build(story,onFirstPage=footer,onLaterPages=footer,canvasmaker=NumberedCanvas)
    out.seek(0)
    return out

@app.get('/')
def root():
    return {
        'service': 'ELEXORA 2 API',
        'status': 'running',
        'health': '/health',
        'docs': '/docs',
    }


@app.get('/health')
def health():
    return {'status':'ok'}

@app.on_event("startup")
def startup():
    init_auth_db()

@app.post('/api/auth/signup')
def signup(payload: AuthRequest):
    username = payload.username.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username):
        raise HTTPException(status_code=400, detail="Username must be 3-32 characters and use only letters, numbers, dot, underscore or hyphen.")
    errors = validate_password(payload.password)
    if errors:
        raise HTTPException(status_code=400, detail="Password must contain " + ", ".join(errors) + ".")
    try:
        with db() as conn:
            conn.execute(
                "INSERT INTO users(username,password_hash) VALUES(%s,%s)",
                (username.lower(), password_hash(payload.password))
            )
            conn.commit()
    except Exception as exc:
        if getattr(exc, "sqlstate", None) == "23505":
            raise HTTPException(status_code=409, detail="That username already exists. Please choose another.")
        raise
    return {"message":"Account created successfully. You can now sign in."}

@app.post('/api/auth/login')
def login(payload: AuthRequest):
    username = payload.username.strip().lower()
    with db() as conn:
        row = conn.execute(
            "SELECT id,password_hash,username FROM users WHERE username=%s",
            (username,)
        ).fetchone()
        if not row or not password_ok(payload.password, row["password_hash"]):
            raise HTTPException(status_code=401, detail="Invalid username or password.")
        token = secrets.token_urlsafe(32)
        expires = int(datetime.now().timestamp()) + TOKEN_TTL_SECONDS
        conn.execute(
            "INSERT INTO sessions(token,user_id,expires_at) VALUES(%s,%s,%s)",
            (token, row["id"], expires)
        )
        conn.commit()
    return {"token":token,"username":row["username"],"expires_at":expires}

@app.post('/api/auth/logout')
def logout(authorization: str = Header(default="")):
    token = authorization[7:].strip() if authorization.startswith("Bearer ") else ""
    if token:
        with db() as conn:
            conn.execute("DELETE FROM sessions WHERE token=%s", (token,))
            conn.commit()
    return {"message":"Signed out."}

@app.post('/api/bom/preview')
async def preview(client:str=Form(''),sales_ref:str=Form(''),drawing:str=Form(''),esd:str=Form(''),wo:str=Form(''),prep_by:str=Form(''),voltage:str=Form(''),msld:UploadFile=File(...),dis:UploadFile=File(...),user:str=Depends(current_user)):
 m,d=await msld.read(),await dis.read(); return extract_from_files(m,msld.filename,d,dis.filename,client,sales_ref,drawing,esd,wo,prep_by,voltage)

def export_book(payload):
 if payload.get('feeders'):
  return export_combined_book(payload)
 wb=Workbook(); ws=wb.active; ws.title='BOM'; thin=Side(style='thin'); border=Border(left=thin,right=thin,top=thin,bottom=thin); h=payload.get('header',{}); rows=payload.get('rows',[]); ws.page_setup.orientation='portrait'; ws.page_setup.paperSize=ws.PAPERSIZE_A4; ws.page_setup.fitToWidth=1; ws.page_setup.fitToHeight=1; ws.sheet_properties.pageSetUpPr.fitToPage=True; ws.page_margins.left=.58; ws.page_margins.right=.75; ws.page_margins.top=.60; ws.page_margins.bottom=.55; ws.sheet_view.showGridLines=False
 for col,width in {'A':12.43,'B':80.99,'C':18.43,'D':10.0,'E':11.87,'F':9.10,'G':5.87,'H':5.87}.items():ws.column_dimensions[col].width=width
 ws.merge_cells('A1:E2'); ws['A1']='-: Equipment List :-'; ws['A1'].font=Font(name='Courier New',size=10); ws['A1'].alignment=Alignment(horizontal='center',vertical='center'); ws['F1']=f'Doc.No.: {payload.get("document_no","SI EA/CS/FR/EG/015")}\nRev.No.: {payload.get("revision","1.0")}, Eff.Dt: {payload.get("effective_date","17/07/2026")}\nCreated By: {payload.get("created_by","EA CS ENGG")}'; ws['F1'].alignment=Alignment(horizontal='right',vertical='top',wrap_text=True); ws['F1'].font=Font(name='Arial',size=7); ws.merge_cells('F1:H2'); ws.row_dimensions[1].height=34; ws.row_dimensions[2].height=16
 for c,v in [('A4','EQPT.NO'),('B4','SPECIFICATION'),('C4','DESIGNATION'),('F4','TOTAL EQPT QTY.'),('G4','MPD'),('H4','AMD')]:ws[c]=v
 ws.merge_cells('A4:A6'); ws.merge_cells('B4:B6'); ws.merge_cells('C4:C6'); ws.merge_cells('D4:E4'); ws.merge_cells('F4:F6'); ws.merge_cells('G4:G6'); ws.merge_cells('H4:H6'); ws['D4']='FEEDER TYPICAL'; ws['D5']='QTY.'; ws['E5']=payload.get('feeder_name',''); ws['E6']=payload.get('feeder_qty','')
 for row in range(4,7):
  for col in range(1,9):ws.cell(row,col).border=border; ws.cell(row,col).alignment=Alignment(horizontal='center',vertical='center',wrap_text=True); ws.cell(row,col).font=Font(name='Courier New',size=10 if row==4 else 9,bold=True)
 ws.row_dimensions[4].height=23.25; ws.row_dimensions[5].height=18; ws.row_dimensions[6].height=18
 for i,row in enumerate(rows[:64],7):
  vals=[row.get('sr',''),row.get('specification',''),row.get('designation',''),' ',row.get('eqpt_qty') if row.get('eqpt_qty') is not None else '',row.get('total') if row.get('total') is not None else '','','']
  for col,val in enumerate(vals,1):ws.cell(i,col,val).border=border; ws.cell(i,col).font=Font(name='Courier New',size=7); ws.cell(i,col).alignment=Alignment(horizontal='center' if col!=2 else 'left',vertical='center',wrap_text=True if col==2 else False)
  ws.row_dimensions[i].height=max(15.6,min(180,15.6*max(1,len(row.get('specification_lines',[])))))
 for i in range(7+len(rows[:64]),70):
  for col in range(1,9):ws.cell(i,col).border=border; ws.cell(i,col).font=Font(name='Courier New',size=7)
  ws.row_dimensions[i].height=15.6
 fr=72; footer_left=[('Item No.','100'),('Client :',h.get('client','')),('Sales Ref No.:',h.get('sales_ref','')),('DATE :',datetime.now().strftime('%d.%m.%Y'))]; footer_mid=[('Description :',payload.get('description','')),('W.O. No.:',h.get('wo','')),('Drg. No.:',h.get('drawing',''))]; footer_right=[('PRE.BY :',h.get('prep_by','')),('Qty.:',payload.get('qty','')),('ESD No.:',h.get('esd','')),('','1 of 1')]
 for i,(label,value) in enumerate(footer_left):
  ws.cell(fr+i,1,label).font=Font(name='Arial',size=7); ws.cell(fr+i,2,value).font=Font(name='Arial',size=7)
  ws.cell(fr+i,1).alignment=Alignment(horizontal='left',vertical='center'); ws.cell(fr+i,2).alignment=Alignment(horizontal='left',vertical='center')
 for i,(label,value) in enumerate(footer_mid):
  ws.cell(fr+i,4,label).font=Font(name='Arial',size=7); ws.cell(fr+i,5,value).font=Font(name='Arial',size=7)
  ws.cell(fr+i,4).alignment=Alignment(horizontal='left',vertical='center'); ws.cell(fr+i,5).alignment=Alignment(horizontal='left',vertical='center')
 for i,(label,value) in enumerate(footer_right):
  ws.cell(fr+i,7,label).font=Font(name='Arial',size=7); ws.cell(fr+i,8,value).font=Font(name='Arial',size=7)
  ws.cell(fr+i,7).alignment=Alignment(horizontal='right',vertical='center'); ws.cell(fr+i,8).alignment=Alignment(horizontal='right',vertical='center')
 ws.print_area='A1:H76'; return wb
@app.post('/api/bom/export')
async def export(payload:dict, user:str=Depends(current_user)):
 wb=export_book(payload);out=io.BytesIO();wb.save(out);out.seek(0);return StreamingResponse(out,media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',headers={'Content-Disposition':'attachment; filename=ELEXORA_BOM.xlsx'})

class NumberedCanvas:
    def __init__(self, *args, **kwargs):
        from reportlab.pdfgen import canvas as pdfcanvas
        self._canvas = pdfcanvas.Canvas(*args, **kwargs)
        self._saved_page_states = []

    def __getattr__(self, name):
        return getattr(self._canvas, name)

    def showPage(self):
        self._saved_page_states.append(dict(self._canvas.__dict__))
        self._canvas._startPage()

    def save(self):
        total = len(self._saved_page_states)
        for state in self._saved_page_states:
            self._canvas.__dict__.update(state)
            self._draw_page_number(total)
            self._canvas.showPage()
        self._canvas.save()

    def _draw_page_number(self, total):
        # Page number occupies its own fourth line in the right footer block.
        w, hp = A4
        self._canvas.setFont('Helvetica', 6.5)
        self._canvas.drawRightString(
            w - 8*mm, 4*mm,
            f'{self._canvas.getPageNumber()} of {total}'
        )


def export_pdf(payload):
    if payload.get('feeders'):
        return export_combined_pdf(payload)
    out = io.BytesIO()
    doc = SimpleDocTemplate(
        out, pagesize=A4,
        rightMargin=8*mm, leftMargin=8*mm,
        topMargin=12*mm, bottomMargin=28*mm
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('bomtitle', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=9, leading=11, alignment=TA_CENTER)
    meta_style = ParagraphStyle('bommeta', parent=styles['Normal'], fontName='Helvetica', fontSize=6.5, leading=8, alignment=TA_RIGHT)
    cell_style = ParagraphStyle('cell', parent=styles['Normal'], fontName='Helvetica', fontSize=6.3, leading=7.5, spaceAfter=0, spaceBefore=0)
    cell_center = ParagraphStyle('cellcenter', parent=cell_style, alignment=TA_CENTER)
    head_style = ParagraphStyle('head', parent=cell_style, fontName='Helvetica-Bold', alignment=TA_CENTER, leading=8)
    h = payload.get('header', {})
    rows = payload.get('rows', [])
    story = [
        Paragraph('-: Equipment List :-', title_style),
        Spacer(1, 4*mm),
    ]
    data = [
        [Paragraph('EQPT.NO',head_style), Paragraph('SPECIFICATION',head_style), Paragraph('DESIGNATION',head_style),
         Paragraph('FEEDER TYPICAL',head_style), '', Paragraph('TOTAL<br/>EQPT<br/>QTY.',head_style), Paragraph('MPD',head_style), Paragraph('AMD',head_style)],
        ['', '', '', Paragraph('QTY.',head_style), Paragraph(str(payload.get('feeder_name','')),head_style), '', '', ''],
        ['', '', '', '', Paragraph(str(payload.get('feeder_qty','')),cell_center), '', '', '']
    ]
    for row in rows[:64]:
        spec = row.get('specification','') or ''
        data.append([
            Paragraph(str(row.get('sr','') or ''), cell_center),
            Paragraph(str(spec).replace('&','&amp;').replace('<','&lt;').replace('>','&gt;').replace('\n','<br/>'), cell_style),
            Paragraph(str(row.get('designation','') or ''), cell_center),
            '',
            Paragraph(str(row.get('eqpt_qty','') or ''), cell_center),
            Paragraph(str(row.get('total','') or ''), cell_center),
            '', ''
        ])
    col_widths=[16*mm, 81*mm, 24*mm, 17*mm, 20*mm, 18*mm, 9*mm, 9*mm]
    tbl=Table(data,colWidths=col_widths,repeatRows=3)
    tbl.setStyle(TableStyle([
        ('SPAN',(0,0),(0,2)),('SPAN',(1,0),(1,2)),('SPAN',(2,0),(2,2)),
        ('SPAN',(3,0),(4,0)),('SPAN',(5,0),(5,2)),('SPAN',(6,0),(6,2)),('SPAN',(7,0),(7,2)),
        ('GRID',(0,0),(-1,-1),0.45,colors.black),
        ('BACKGROUND',(0,0),(-1,2),colors.HexColor('#d9e7f0')),
        ('VALIGN',(0,0),(-1,-1),'MIDDLE'),
        ('ALIGN',(0,0),(-1,2),'CENTER'),
        ('LEFTPADDING',(0,0),(-1,-1),2),('RIGHTPADDING',(0,0),(-1,-1),2),
        ('TOPPADDING',(0,0),(-1,-1),2),('BOTTOMPADDING',(0,0),(-1,-1),2),
    ]))
    story.append(tbl)

    def footer(canvas, doc_obj):
        canvas.saveState()
        w, hp = A4
        y = 11*mm
        canvas.setFont('Helvetica', 6.5)

        # Fixed footer zones matching the supplied BOM: left / center / right.
        left_x = 8*mm
        mid_x = 82*mm
        right_x = 164*mm
        line_step = 3*mm

        left=[
            ('Item No.','100'),
            ('Client :',h.get('client','')),
            ('Sales Ref No.:',h.get('sales_ref','')),
            ('DATE :',datetime.now().strftime('%d.%m.%Y'))
        ]
        mid=[
            ('Description :',payload.get('description','')),
            ('W.O. No.:',h.get('wo','')),
            ('Drg. No.:',h.get('drawing',''))
        ]
        right=[
            ('PRE.BY :',h.get('prep_by','')),
            ('Qty.:',payload.get('qty','')),
            ('ESD No.:',h.get('esd',''))
        ]

        for i,(label,value) in enumerate(left):
            canvas.drawString(left_x, y+(2.5-i)*line_step, (label+' '+str(value)).strip())

        # Keep the center footer inside its own zone so it cannot overlap the right block.
        from reportlab.pdfbase.pdfmetrics import stringWidth
        mid_max_x = right_x - 5*mm
        for i,(label,value) in enumerate(mid):
            text_value = (label+' '+str(value)).strip()
            max_width = mid_max_x - mid_x
            while stringWidth(text_value, 'Helvetica', 6.5) > max_width and len(text_value) > len(label) + 4:
                text_value = text_value[:-2].rstrip() + '...'
            canvas.drawString(mid_x, y+(2.5-i)*line_step, text_value)

        for i,(label,value) in enumerate(right):
            canvas.drawString(right_x, y+(2.5-i)*line_step, (label+' '+str(value)).strip())

        canvas.restoreState()
    doc.build(story,onFirstPage=footer,onLaterPages=footer,canvasmaker=NumberedCanvas)
    out.seek(0)
    return out

@app.post('/api/bom/export-pdf')
async def export_pdf_endpoint(payload:dict, user:str=Depends(current_user)):
    out=export_pdf(payload)
    return StreamingResponse(out,media_type='application/pdf',headers={'Content-Disposition':f'attachment; filename=BOM_{re.sub(r"[^A-Za-z0-9._-]+", "_", str(payload.get("header", {}).get("wo", "WO"))).strip("_")}.pdf'})
@app.post('/api/bom/preview-multiple')
async def preview_multiple(
    client: str = Form(''), sales_ref: str = Form(''), drawing: str = Form(''),
    esd: str = Form(''), wo: str = Form(''), prep_by: str = Form(''),
    voltage: str = Form(''), mslds: list[UploadFile] = File(...),
    dis: UploadFile = File(...), user: str = Depends(current_user)
):
    if not mslds:
        raise HTTPException(status_code=400, detail='Upload at least one MSLD PDF.')
    dis_name = dis.filename or 'DIS.pdf'
    dis_bytes = await dis.read()
    items = []
    filenames = []
    for upload in mslds:
        filename = upload.filename or 'MSLD.pdf'
        items.append((await upload.read(), filename))
        filenames.append(filename)
    result = extract_multiple_from_files(items, dis_bytes, dis_name, client, sales_ref, drawing, esd, wo, prep_by, voltage)
    import json
    with db() as conn:
        conn.execute(
            """INSERT INTO bom_history (username, customer_name, work_order_no, voltage, msld_files, dis_filename, row_count)
               VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)""",
            (user, str(client or result.get('header', {}).get('client', '')), str(wo or result.get('header', {}).get('wo', '')),
             str(voltage or ''), json.dumps(filenames), dis_name, len(result.get('rows') or []))
        )
        conn.commit()
    return result




class SiemensLookupRequest(BaseModel):
    mlfb: str


@app.post('/api/siemens/product-lookup')
def siemens_product_lookup(payload: SiemensLookupRequest, user: str = Depends(current_user)):
    """POC: live lookup of a supplied 7SJ66 MLFB on Siemens Industry Mall."""
    return lookup_siemens_product(payload.mlfb)

@app.get('/api/admin/bom-history')
def admin_bom_history(user: str = Depends(require_admin)):
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) AS total FROM bom_history").fetchone()['total']
        rows = conn.execute(
            """SELECT id, username, customer_name, work_order_no, voltage, msld_files,
                      dis_filename, row_count, created_at
               FROM bom_history ORDER BY created_at DESC LIMIT 500"""
        ).fetchall()
    return {'total_generated': total, 'history': rows}

def export_combined_book(payload):
    wb = Workbook()
    ws = wb.active
    ws.title = 'BOM'
    thin = Side(style='thin')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    header_data = payload.get('header', {})
    feeders = payload.get('feeders') or []
    rows = payload.get('rows') or []
    n = max(1, len(feeders))
    qty_col = 4
    feeder_start_col = 5
    total_col = feeder_start_col + n
    mpd_col, amd_col = total_col + 1, total_col + 2
    last_col = amd_col
    ws.page_setup.orientation = 'landscape'
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 1
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_margins.left = .25
    ws.page_margins.right = .25
    ws.page_margins.top = .45
    ws.page_margins.bottom = .4
    ws.sheet_view.showGridLines = False
    spec_width = max(36, min(78, max([max((len(line) for line in str(r.get('specification','')).splitlines()), default=0) for r in rows] + [36]) * 0.75))
    designation_width = max(14, min(26, max([len(str(r.get('designation',''))) for r in rows] + [14]) * 0.9))
    feeder_width = max(10, min(18, max([len(str(f.get('name',''))) for f in feeders] + [8]) * 0.9))
    widths = [9, spec_width, designation_width, 9] + [feeder_width] * n + [13, 7, 7]
    for col, width in enumerate(widths, 1):
        ws.column_dimensions[__import__('openpyxl').utils.get_column_letter(col)].width = width
    ws.merge_cells(start_row=1, start_column=1, end_row=2, end_column=min(3, last_col))
    ws.cell(1, 1, '-: Equipment List :-')
    ws.cell(1, 1).font = Font(name='Courier New', size=10)
    ws.cell(1, 1).alignment = Alignment(horizontal='center', vertical='center')
    meta_col = max(4, last_col - 2)
    ws.merge_cells(start_row=1, start_column=meta_col, end_row=2, end_column=last_col)
    ws.cell(1, meta_col, f'Doc.No.: {payload.get("document_no","SI EA/CS/FR/EG/015")}\nRev.No.: {payload.get("revision","1.0")}, Eff.Dt: {payload.get("effective_date","17/07/2026")}\nCreated By: {payload.get("created_by","EA CS ENGG")}')
    ws.cell(1, meta_col).font = Font(name='Arial', size=7)
    ws.cell(1, meta_col).alignment = Alignment(horizontal='right', vertical='top', wrap_text=True)
    ws.row_dimensions[1].height = 34
    ws.row_dimensions[2].height = 16
    for col, label in [(1,'EQPT.NO'),(2,'SPECIFICATION'),(3,'DESIGNATION'),(total_col,'TOTAL EQPT QTY.'),(mpd_col,'MPD'),(amd_col,'AMD')]:
        ws.cell(4, col, label)
    for col in [1,2,3,total_col,mpd_col,amd_col]:
        ws.merge_cells(start_row=4, start_column=col, end_row=6, end_column=col)
    ws.merge_cells(start_row=5, start_column=qty_col, end_row=6, end_column=qty_col)
    ws.cell(5,qty_col,'QTY.')
    ws.merge_cells(start_row=4, start_column=feeder_start_col, end_row=4, end_column=feeder_start_col+n-1)
    ws.cell(4,feeder_start_col,'FEEDER TYPICAL')
    for idx, feeder in enumerate(feeders):
        col=feeder_start_col+idx
        ws.cell(5,col,feeder.get('name',''))
        ws.cell(6,col,feeder.get('quantity') or feeder.get('qty') or 1)
    for r in range(4,7):
        for c in range(1,last_col+1):
            cell=ws.cell(r,c)
            cell.border=border
            cell.alignment=Alignment(horizontal='center',vertical='center',wrap_text=True)
            cell.font=Font(name='Courier New',size=8,bold=True)
    ws.row_dimensions[4].height=23
    ws.row_dimensions[5].height=18
    ws.row_dimensions[6].height=28
    for i,row in enumerate(rows[:64],7):
        quantities = row.get('feeder_quantities') or []
        vals=[row.get('sr',''),row.get('specification',''),row.get('designation','')]
        vals.append('')
        vals.extend(quantities[j] if j < len(quantities) and quantities[j] not in (0, '0', None, '') else '' for j in range(n))
        total_value = row.get('total', '')
        vals.extend(['' if total_value in (0, '0') else total_value, '', ''])
        for col,val in enumerate(vals,1):
            cell=ws.cell(i,col,val)
            cell.border=border
            cell.font=Font(name='Courier New',size=7)
            cell.alignment=Alignment(horizontal='center' if col!=2 else 'left',vertical='center',wrap_text=col==2)
        spec_lines = str(row.get('specification', '')).splitlines() or ['']
        chars_per_line = max(12, int(spec_width * 1.35))
        wrapped_lines = sum(max(1, (len(line) + chars_per_line - 1) // chars_per_line) for line in spec_lines)
        ws.row_dimensions[i].height = max(18, min(240, 10 * wrapped_lines + 6))
    for i in range(7+len(rows[:64]),70):
        for c in range(1,last_col+1):
            ws.cell(i,c).border=border
            ws.cell(i,c).font=Font(name='Courier New',size=7)
        ws.row_dimensions[i].height=15
    # Keep worksheet view focused on the equipment list. Document metadata is
    # printed in the page header/footer when the user presses Ctrl+P.
    ws.oddHeader.center.text = "&B Equipment List &B"
    ws.oddHeader.right.text = f"Doc.No.: {payload.get('document_no','SI EA/CS/FR/EG/015')}\\nRev.No.: {payload.get('revision','1.0')}  Eff.Dt: {payload.get('effective_date','17/07/2026')}"
    ws.oddHeader.right.size = 7
    ws.oddFooter.left.text = f"Client: {header_data.get('client','')}  |  Sales Ref: {header_data.get('sales_ref','')}\\nDescription: {payload.get('description','')}  |  WO: {header_data.get('wo','')}  |  Drawing: {header_data.get('drawing','')}"
    ws.oddFooter.center.text = f"Prepared by: {header_data.get('prep_by','')}  |  Qty: {payload.get('qty','')}  |  ESD: {header_data.get('esd','')}"
    ws.oddFooter.right.text = "Page &P of &N"
    ws.oddFooter.left.size = 7
    ws.oddFooter.center.size = 7
    ws.oddFooter.right.size = 7
    ws.print_area=f'A1:{__import__("openpyxl").utils.get_column_letter(last_col)}76'
    return wb

def export_combined_pdf(payload):
    out=io.BytesIO()
    doc=SimpleDocTemplate(out,pagesize=A4,rightMargin=5*mm,leftMargin=5*mm,topMargin=6*mm,bottomMargin=20*mm)
    styles=getSampleStyleSheet()
    title=ParagraphStyle('combinedtitle',parent=styles['Normal'],fontName='Helvetica-Bold',fontSize=6,leading=7,alignment=TA_CENTER)
    cell=ParagraphStyle('combinedcell',parent=styles['Normal'],fontName='Helvetica',fontSize=4.2,leading=4.8,spaceAfter=0,spaceBefore=0,wordWrap='CJK')
    center=ParagraphStyle('combinedcenter',parent=cell,alignment=TA_CENTER)
    head=ParagraphStyle('combinedhead',parent=cell,fontName='Helvetica-Bold',alignment=TA_CENTER,fontSize=4.0,leading=4.5)
    feeders=payload.get('feeders') or []
    rows=payload.get('rows') or []
    n=max(1,len(feeders))
    data=[[Paragraph('EQPT.NO',head),Paragraph('SPECIFICATION',head),Paragraph('DESIGNATION',head),''] + [Paragraph('FEEDER<br/>TYPICAL',head) if i==0 else '' for i in range(n)] + [Paragraph('TOTAL<br/>EQPT QTY.',head),Paragraph('MPD',head),Paragraph('AMD',head)]]
    data.append(['','','',Paragraph('QTY.',head)]+[Paragraph(str(f.get('name','')),head) for f in feeders]+['','',''])
    data.append(['','','','']+[Paragraph(str(f.get('quantity') or f.get('qty') or 1),center) for f in feeders]+['','',''])
    for row in rows[:64]:
        spec=str(row.get('specification','') or '').replace('&','&amp;').replace('<','&lt;').replace('>','&gt;').replace('\n','<br/>')
        qs=row.get('feeder_quantities') or []
        data.append([Paragraph(str(row.get('sr','')),center),Paragraph(spec,cell),Paragraph(str(row.get('designation','') or ''),center),Paragraph('',center)]+[Paragraph(str(qs[i] if i<len(qs) and qs[i] not in (0, '0', None, '') else ''),center) for i in range(n)]+[Paragraph('' if row.get('total') in (0, '0') else str(row.get('total') or ''),center),'',''])
    available=200*mm
    fixed=[8*mm,66*mm,25*mm,9*mm,12*mm,10*mm,10*mm]
    # Allocate a little more space to long feeder labels, while keeping the
    # complete table inside landscape A4. Paragraph cells wrap and expand rows.
    remaining=max(8*mm, available-sum(fixed))
    feeder_labels=[len(str(f.get('name',''))) for f in feeders]
    weights=[max(1.0,min(2.0,length/10.0)) for length in feeder_labels]
    weight_sum=sum(weights) or n
    feeder_widths=[remaining*w/weight_sum for w in weights] if feeders else [remaining]
    widths=[fixed[0],fixed[1],fixed[2],fixed[3]]+feeder_widths+[fixed[4],fixed[5],fixed[6]]
    tbl=Table(data,colWidths=widths,rowHeights=[18,14,20]+[None]*(len(data)-3),repeatRows=3)
    spans=[('SPAN',(0,0),(0,2)),('SPAN',(1,0),(1,2)),('SPAN',(2,0),(2,2)),('SPAN',(3,1),(3,2)),('SPAN',(4,0),(3+n,0)),('SPAN',(4+n,0),(4+n,2)),('SPAN',(5+n,0),(5+n,2)),('SPAN',(6+n,0),(6+n,2))]
    # A compact first-row group label and feeder-specific second/third header rows.
    tbl.setStyle(TableStyle(spans+[('GRID',(0,0),(-1,-1),0.45,colors.black),('BACKGROUND',(0,0),(-1,2),colors.HexColor('#d9e7f0')),('VALIGN',(0,0),(-1,-1),'MIDDLE'),('ALIGN',(0,0),(-1,2),'CENTER'),('LEFTPADDING',(0,0),(-1,-1),2),('RIGHTPADDING',(0,0),(-1,-1),2),('TOPPADDING',(0,0),(-1,-1),1),('BOTTOMPADDING',(0,0),(-1,-1),1)]))
    story=[Paragraph('-: Equipment List :-',title),Spacer(1,3*mm),tbl]
    h=payload.get('header',{})
    def footer(canvas, doc_obj):
        canvas.saveState()
        w,hp=doc_obj.pagesize
        y=8*mm
        canvas.setFont('Helvetica',6.5)
        canvas.drawString(6*mm,y+7*mm,f"Client: {h.get('client','')}  Sales Ref: {h.get('sales_ref','')}")
        canvas.drawString(6*mm,y+4*mm,f"Description: {payload.get('description','')}  WO: {h.get('wo','')}  Drawing: {h.get('drawing','')}")
        canvas.drawString(6*mm,y+1*mm,f"Prepared by: {h.get('prep_by','')}  Qty: {payload.get('qty','')}  ESD: {h.get('esd','')}")
        canvas.restoreState()
    doc.build(story,onFirstPage=footer,onLaterPages=footer,canvasmaker=NumberedCanvas)
    out.seek(0)
    return out

@app.get('/')
def root():
    return {
        'service': 'ELEXORA 2 API',
        'status': 'running',
        'health': '/health',
        'docs': '/docs',
    }


@app.get('/health')
def health():
    return {'status':'ok'}

@app.on_event("startup")
def startup():
    init_auth_db()

@app.post('/api/auth/signup')
def signup(payload: AuthRequest):
    username = payload.username.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username):
        raise HTTPException(status_code=400, detail="Username must be 3-32 characters and use only letters, numbers, dot, underscore or hyphen.")
    errors = validate_password(payload.password)
    if errors:
        raise HTTPException(status_code=400, detail="Password must contain " + ", ".join(errors) + ".")
    try:
        with db() as conn:
            conn.execute(
                "INSERT INTO users(username,password_hash) VALUES(%s,%s)",
                (username.lower(), password_hash(payload.password))
            )
            conn.commit()
    except Exception as exc:
        if getattr(exc, "sqlstate", None) == "23505":
            raise HTTPException(status_code=409, detail="That username already exists. Please choose another.")
        raise
    return {"message":"Account created successfully. You can now sign in."}

@app.post('/api/auth/login')
def login(payload: AuthRequest):
    username = payload.username.strip().lower()
    with db() as conn:
        row = conn.execute(
            "SELECT id,password_hash,username FROM users WHERE username=%s",
            (username,)
        ).fetchone()
        if not row or not password_ok(payload.password, row["password_hash"]):
            raise HTTPException(status_code=401, detail="Invalid username or password.")
        token = secrets.token_urlsafe(32)
        expires = int(datetime.now().timestamp()) + TOKEN_TTL_SECONDS
        conn.execute(
            "INSERT INTO sessions(token,user_id,expires_at) VALUES(%s,%s,%s)",
            (token, row["id"], expires)
        )
        conn.commit()
    return {"token":token,"username":row["username"],"expires_at":expires}

@app.post('/api/auth/logout')
def logout(authorization: str = Header(default="")):
    token = authorization[7:].strip() if authorization.startswith("Bearer ") else ""
    if token:
        with db() as conn:
            conn.execute("DELETE FROM sessions WHERE token=%s", (token,))
            conn.commit()
    return {"message":"Signed out."}

@app.post('/api/bom/preview')
async def preview(client:str=Form(''),sales_ref:str=Form(''),drawing:str=Form(''),esd:str=Form(''),wo:str=Form(''),prep_by:str=Form(''),voltage:str=Form(''),msld:UploadFile=File(...),dis:UploadFile=File(...),user:str=Depends(current_user)):
 m,d=await msld.read(),await dis.read(); return extract_from_files(m,msld.filename,d,dis.filename,client,sales_ref,drawing,esd,wo,prep_by,voltage)

def export_book(payload):
 if payload.get('feeders'):
  return export_combined_book(payload)
 wb=Workbook(); ws=wb.active; ws.title='BOM'; thin=Side(style='thin'); border=Border(left=thin,right=thin,top=thin,bottom=thin); h=payload.get('header',{}); rows=payload.get('rows',[]); ws.page_setup.orientation='portrait'; ws.page_setup.paperSize=ws.PAPERSIZE_A4; ws.page_setup.fitToWidth=1; ws.page_setup.fitToHeight=1; ws.sheet_properties.pageSetUpPr.fitToPage=True; ws.page_margins.left=.58; ws.page_margins.right=.75; ws.page_margins.top=.60; ws.page_margins.bottom=.55; ws.sheet_view.showGridLines=False
 for col,width in {'A':12.43,'B':80.99,'C':18.43,'D':10.0,'E':11.87,'F':9.10,'G':5.87,'H':5.87}.items():ws.column_dimensions[col].width=width
 ws.merge_cells('A1:E2'); ws['A1']='-: Equipment List :-'; ws['A1'].font=Font(name='Courier New',size=10); ws['A1'].alignment=Alignment(horizontal='center',vertical='center'); ws['F1']=f'Doc.No.: {payload.get("document_no","SI EA/CS/FR/EG/015")}\nRev.No.: {payload.get("revision","1.0")}, Eff.Dt: {payload.get("effective_date","17/07/2026")}\nCreated By: {payload.get("created_by","EA CS ENGG")}'; ws['F1'].alignment=Alignment(horizontal='right',vertical='top',wrap_text=True); ws['F1'].font=Font(name='Arial',size=7); ws.merge_cells('F1:H2'); ws.row_dimensions[1].height=34; ws.row_dimensions[2].height=16
 for c,v in [('A4','EQPT.NO'),('B4','SPECIFICATION'),('C4','DESIGNATION'),('F4','TOTAL EQPT QTY.'),('G4','MPD'),('H4','AMD')]:ws[c]=v
 ws.merge_cells('A4:A6'); ws.merge_cells('B4:B6'); ws.merge_cells('C4:C6'); ws.merge_cells('D4:E4'); ws.merge_cells('F4:F6'); ws.merge_cells('G4:G6'); ws.merge_cells('H4:H6'); ws['D4']='FEEDER TYPICAL'; ws['D5']='QTY.'; ws['E5']=payload.get('feeder_name',''); ws['E6']=payload.get('feeder_qty','')
 for row in range(4,7):
  for col in range(1,9):ws.cell(row,col).border=border; ws.cell(row,col).alignment=Alignment(horizontal='center',vertical='center',wrap_text=True); ws.cell(row,col).font=Font(name='Courier New',size=10 if row==4 else 9,bold=True)
 ws.row_dimensions[4].height=23.25; ws.row_dimensions[5].height=18; ws.row_dimensions[6].height=18
 for i,row in enumerate(rows[:64],7):
  vals=[row.get('sr',''),row.get('specification',''),row.get('designation',''),' ',row.get('eqpt_qty') if row.get('eqpt_qty') is not None else '',row.get('total') if row.get('total') is not None else '','','']
  for col,val in enumerate(vals,1):ws.cell(i,col,val).border=border; ws.cell(i,col).font=Font(name='Courier New',size=7); ws.cell(i,col).alignment=Alignment(horizontal='center' if col!=2 else 'left',vertical='center',wrap_text=True if col==2 else False)
  ws.row_dimensions[i].height=max(15.6,min(180,15.6*max(1,len(row.get('specification_lines',[])))))
 for i in range(7+len(rows[:64]),70):
  for col in range(1,9):ws.cell(i,col).border=border; ws.cell(i,col).font=Font(name='Courier New',size=7)
  ws.row_dimensions[i].height=15.6
 fr=72; footer_left=[('Item No.','100'),('Client :',h.get('client','')),('Sales Ref No.:',h.get('sales_ref','')),('DATE :',datetime.now().strftime('%d.%m.%Y'))]; footer_mid=[('Description :',payload.get('description','')),('W.O. No.:',h.get('wo','')),('Drg. No.:',h.get('drawing',''))]; footer_right=[('PRE.BY :',h.get('prep_by','')),('Qty.:',payload.get('qty','')),('ESD No.:',h.get('esd','')),('','1 of 1')]
 for i,(label,value) in enumerate(footer_left):
  ws.cell(fr+i,1,label).font=Font(name='Arial',size=7); ws.cell(fr+i,2,value).font=Font(name='Arial',size=7)
  ws.cell(fr+i,1).alignment=Alignment(horizontal='left',vertical='center'); ws.cell(fr+i,2).alignment=Alignment(horizontal='left',vertical='center')
 for i,(label,value) in enumerate(footer_mid):
  ws.cell(fr+i,4,label).font=Font(name='Arial',size=7); ws.cell(fr+i,5,value).font=Font(name='Arial',size=7)
  ws.cell(fr+i,4).alignment=Alignment(horizontal='left',vertical='center'); ws.cell(fr+i,5).alignment=Alignment(horizontal='left',vertical='center')
 for i,(label,value) in enumerate(footer_right):
  ws.cell(fr+i,7,label).font=Font(name='Arial',size=7); ws.cell(fr+i,8,value).font=Font(name='Arial',size=7)
  ws.cell(fr+i,7).alignment=Alignment(horizontal='right',vertical='center'); ws.cell(fr+i,8).alignment=Alignment(horizontal='right',vertical='center')
 ws.print_area='A1:H76'; return wb
@app.post('/api/bom/export')
async def export(payload:dict, user:str=Depends(current_user)):
 wb=export_book(payload);out=io.BytesIO();wb.save(out);out.seek(0);return StreamingResponse(out,media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',headers={'Content-Disposition':'attachment; filename=ELEXORA_BOM.xlsx'})

class NumberedCanvas:
    def __init__(self, *args, **kwargs):
        from reportlab.pdfgen import canvas as pdfcanvas
        self._canvas = pdfcanvas.Canvas(*args, **kwargs)
        self._saved_page_states = []

    def __getattr__(self, name):
        return getattr(self._canvas, name)

    def showPage(self):
        self._saved_page_states.append(dict(self._canvas.__dict__))
        self._canvas._startPage()

    def save(self):
        total = len(self._saved_page_states)
        for state in self._saved_page_states:
            self._canvas.__dict__.update(state)
            self._draw_page_number(total)
            self._canvas.showPage()
        self._canvas.save()

    def _draw_page_number(self, total):
        # Page number occupies its own fourth line in the right footer block.
        w, hp = A4
        self._canvas.setFont('Helvetica', 6.5)
        self._canvas.drawRightString(
            w - 8*mm, 4*mm,
            f'{self._canvas.getPageNumber()} of {total}'
        )


def export_pdf(payload):
    if payload.get('feeders'):
        return export_combined_pdf(payload)
    out = io.BytesIO()
    doc = SimpleDocTemplate(
        out, pagesize=A4,
        rightMargin=8*mm, leftMargin=8*mm,
        topMargin=12*mm, bottomMargin=28*mm
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('bomtitle', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=9, leading=11, alignment=TA_CENTER)
    meta_style = ParagraphStyle('bommeta', parent=styles['Normal'], fontName='Helvetica', fontSize=6.5, leading=8, alignment=TA_RIGHT)
    cell_style = ParagraphStyle('cell', parent=styles['Normal'], fontName='Helvetica', fontSize=6.3, leading=7.5, spaceAfter=0, spaceBefore=0)
    cell_center = ParagraphStyle('cellcenter', parent=cell_style, alignment=TA_CENTER)
    head_style = ParagraphStyle('head', parent=cell_style, fontName='Helvetica-Bold', alignment=TA_CENTER, leading=8)
    h = payload.get('header', {})
    rows = payload.get('rows', [])
    story = [
        Paragraph('-: Equipment List :-', title_style),
        Spacer(1, 4*mm),
    ]
    data = [
        [Paragraph('EQPT.NO',head_style), Paragraph('SPECIFICATION',head_style), Paragraph('DESIGNATION',head_style),
         Paragraph('FEEDER TYPICAL',head_style), '', Paragraph('TOTAL<br/>EQPT<br/>QTY.',head_style), Paragraph('MPD',head_style), Paragraph('AMD',head_style)],
        ['', '', '', Paragraph('QTY.',head_style), Paragraph(str(payload.get('feeder_name','')),head_style), '', '', ''],
        ['', '', '', '', Paragraph(str(payload.get('feeder_qty','')),cell_center), '', '', '']
    ]
    for row in rows[:64]:
        spec = row.get('specification','') or ''
        data.append([
            Paragraph(str(row.get('sr','') or ''), cell_center),
            Paragraph(str(spec).replace('&','&amp;').replace('<','&lt;').replace('>','&gt;').replace('\n','<br/>'), cell_style),
            Paragraph(str(row.get('designation','') or ''), cell_center),
            '',
            Paragraph(str(row.get('eqpt_qty','') or ''), cell_center),
            Paragraph(str(row.get('total','') or ''), cell_center),
            '', ''
        ])
    col_widths=[16*mm, 81*mm, 24*mm, 17*mm, 20*mm, 18*mm, 9*mm, 9*mm]
    tbl=Table(data,colWidths=col_widths,repeatRows=3)
    tbl.setStyle(TableStyle([
        ('SPAN',(0,0),(0,2)),('SPAN',(1,0),(1,2)),('SPAN',(2,0),(2,2)),
        ('SPAN',(3,0),(4,0)),('SPAN',(5,0),(5,2)),('SPAN',(6,0),(6,2)),('SPAN',(7,0),(7,2)),
        ('GRID',(0,0),(-1,-1),0.45,colors.black),
        ('BACKGROUND',(0,0),(-1,2),colors.HexColor('#d9e7f0')),
        ('VALIGN',(0,0),(-1,-1),'MIDDLE'),
        ('ALIGN',(0,0),(-1,2),'CENTER'),
        ('LEFTPADDING',(0,0),(-1,-1),2),('RIGHTPADDING',(0,0),(-1,-1),2),
        ('TOPPADDING',(0,0),(-1,-1),2),('BOTTOMPADDING',(0,0),(-1,-1),2),
    ]))
    story.append(tbl)

    def footer(canvas, doc_obj):
        canvas.saveState()
        w, hp = A4
        y = 11*mm
        canvas.setFont('Helvetica', 6.5)

        # Fixed footer zones matching the supplied BOM: left / center / right.
        left_x = 8*mm
        mid_x = 82*mm
        right_x = 164*mm
        line_step = 3*mm

        left=[
            ('Item No.','100'),
            ('Client :',h.get('client','')),
            ('Sales Ref No.:',h.get('sales_ref','')),
            ('DATE :',datetime.now().strftime('%d.%m.%Y'))
        ]
        mid=[
            ('Description :',payload.get('description','')),
            ('W.O. No.:',h.get('wo','')),
            ('Drg. No.:',h.get('drawing',''))
        ]
        right=[
            ('PRE.BY :',h.get('prep_by','')),
            ('Qty.:',payload.get('qty','')),
            ('ESD No.:',h.get('esd',''))
        ]

        for i,(label,value) in enumerate(left):
            canvas.drawString(left_x, y+(2.5-i)*line_step, (label+' '+str(value)).strip())

        # Keep the center footer inside its own zone so it cannot overlap the right block.
        from reportlab.pdfbase.pdfmetrics import stringWidth
        mid_max_x = right_x - 5*mm
        for i,(label,value) in enumerate(mid):
            text_value = (label+' '+str(value)).strip()
            max_width = mid_max_x - mid_x
            while stringWidth(text_value, 'Helvetica', 6.5) > max_width and len(text_value) > len(label) + 4:
                text_value = text_value[:-2].rstrip() + '...'
            canvas.drawString(mid_x, y+(2.5-i)*line_step, text_value)

        for i,(label,value) in enumerate(right):
            canvas.drawString(right_x, y+(2.5-i)*line_step, (label+' '+str(value)).strip())

        canvas.restoreState()
    doc.build(story,onFirstPage=footer,onLaterPages=footer,canvasmaker=NumberedCanvas)
    out.seek(0)
    return out

@app.post('/api/bom/export-pdf')
async def export_pdf_endpoint(payload:dict, user:str=Depends(current_user)):
    out=export_pdf(payload)
    return StreamingResponse(out,media_type='application/pdf',headers={'Content-Disposition':f'attachment; filename=BOM_{re.sub(r"[^A-Za-z0-9._-]+", "_", str(payload.get("header", {}).get("wo", "WO"))).strip("_")}.pdf'})

#
def transform_word(page,word):
 pts=[fitz.Point(word[0],word[1])*page.rotation_matrix,fitz.Point(word[2],word[3])*page.rotation_matrix]
 return {'x':(pts[0].x+pts[1].x)/2,'y':(pts[0].y+pts[1].y)/2,'text':word[4]}
def page_words(page): return [transform_word(page,w) for w in page.get_text('words') if w[4].strip()]
def clean(s): return re.sub(r'\s+',' ',s).strip(' -')
def words_text(words): return ' '.join(w['text'] for w in sorted(words,key=lambda z:(round(z['y'],1),z['x'])))
def text_in_band(words,xmin,xmax,ymin,ymax): return clean(words_text([w for w in words if xmin<=w['x']<xmax and ymin<=w['y']<ymax]))
def rows_by_designation(words,xmin,xmax,ymin,ymax):
 vals=[]
 for w in words:
  if xmin<=w['x']<=xmax and ymin<=w['y']<=ymax:
   t=clean(w['text'])
   if t.upper() in {'DESIG.','QTY.','NO.'}: continue
   if re.fullmatch(r'[A-Z0-9]+(?:[-/,][A-Z0-9]+)*',t,re.I) and re.search(r'\d',t): vals.append((w['y'],t))
 vals.sort(); out=[]
 for y,t in vals:
  if not out or abs(y-out[-1][0])>5: out.append([y,t])
 return out
def designation_qty(des):
 total=0
 for part in re.split(r'[,/]',(des or '').upper()):
  part=part.strip()
  # Supports both letter-prefixed (F1-F3) and digit+letter (1H1-1H3)
  # designations used by the fixed MSLD template.
  m=re.fullmatch(r'(?:[A-Z]+|\d+[A-Z]+)(\d+)-(?:[A-Z]+|\d+[A-Z]+)?(\d+)',part)
  if m: total+=max(1,int(m.group(2))-int(m.group(1))+1)
  elif re.fullmatch(r'(?:[A-Z]+|\d+[A-Z]+)\d+',part): total+=1
 if total==0 and des:
  m=re.fullmatch(r'(?:[A-Z]+|\d+[A-Z]+)(\d+)-(?:[A-Z]+|\d+[A-Z]+)(\d+)',des.upper())
  if m: total=max(1,int(m.group(2))-int(m.group(1))+1)
 return total or None
def master_match(description,details,designation):
 s=(description+' '+details).upper()
 des=re.sub(r'\s+','',str(designation or '')).upper()
 if des == 'E2':return 'E2_LED'
 if 'LED LAMP' in s or re.search(r'\bLED\b',s):return 'LED'
 if 'CURRENT TRANSFORMER' in s:return 'CT'
 if 'POTENTIAL TRANSFORMER' in s:return 'PT'
 if re.search(r'\b7SJ66\d{2}\b', s) or 'NUM. PROT. RELAY' in s or 'NUMERICAL PROTECTION RELAY' in s:return '7SJ6611'
 if 'DIGITAL MF METER' in s or 'EM6400NG' in s:return 'EM6400NG'
 if 'DIGITAL VOLTMETER' in s:return 'VOLTMETER'
 if 'DIGITAL AMMETER' in s:return 'AMMETER'
 if 'MINIATURE CIRCUIT BREAKER' in s or re.search(r'\bMCB\b',s) or '5SL4206-7RC' in s:return 'MCB'
 return designation or 'UNMATCHED'

def extract_fixed_table(data,filename):
 pages=pdf_pages(data,filename); records=[]; feeders=[]; warnings=[]
 for page_no,page in enumerate(pages,1):
  if page_no!=1:continue
  words=page_words(page); feeder_type=text_in_band(words,440,520,410,430); feeder_designation=text_in_band(words,440,520,395,415); feeder_rating=text_in_band(words,440,520,430,450); wiring=text_in_band(words,440,520,450,465); feeder_quantity=text_in_band(words,440,520,360,380)
  if feeder_type or feeder_designation:feeders.append({'name':feeder_designation or feeder_type,'type':feeder_type,'designation':feeder_designation,'rating':feeder_rating,'wiring':wiring,'quantity':feeder_quantity or '1'})
  left=rows_by_designation(words,275,320,450,790)
  for i,(y,des) in enumerate(left):
   next_y=left[i+1][0] if i+1<len(left) else 790
   # T1-T3 is a two-line CT block in the fixed MSLD template. Capture the
   # complete technical-data block rather than only one visual row.
   row_top = y-18 if des.upper() == 'T1-T3' else y-9
   lo,hi=max(490,row_top),min(790,(y+next_y)/2)
   desc=text_in_band(words,50,275,lo,hi); details=text_in_band(words,320,615,lo,hi)
   if desc or details:records.append({'source_page':page_no,'description':desc,'designation':des,'details':details,'master_code':master_match(desc,details,des),'quantity':designation_qty(des),'section':'MSLD equipment'})
  for ymin,ymax in [(35,345),(350,725)]:
   right=rows_by_designation(words,840,880,ymin,ymax)
   for i,(y,des) in enumerate(right):
    next_y=right[i+1][0] if i+1<len(right) else ymax; lo,hi=max(ymin,y-8),min(ymax,(y+next_y)/2); desc=text_in_band(words,630,835,lo,hi); details=text_in_band(words,890,1175,lo,hi)
    if desc or details or des.upper() in ('PS','PS+S28'):records.append({'source_page':page_no,'description':desc,'designation':des,'details':details,'master_code':master_match(desc,details,des),'quantity':designation_qty(des) or 1,'section':'MSLD equipment'})
 return feeders,records,warnings

def extract_header_from_page(data,filename):
 pages=pdf_pages(data,filename)
 if not pages:return {}
 words=page_words(pages[0]); band=lambda a,b,c,d:text_in_band(words,a,b,c,d); drawing_band=band(980,1110,805,830); drawing=first(r'(G\d{4,}-[A-Z0-9]+-[A-Z0-9]+)',drawing_band,drawing_band); esd=first(r'\b(SED\d+)\b',drawing_band) or first(r'\b(SED\d+)\b',words_text(words))
 return {'client':band(345,440,782,792),'sales_ref':band(910,960,782,793),'drawing':drawing,'esd':esd,'wo':band(905,960,793,805),'description':band(690,835,775,795),'qty':band(1000,1040,790,805),'master_singleline':band(690,835,800,815)}
def header(msld,dis,client,sales,drawing,esd,wo,prep,voltage):
 s=msld+'\n'+dis
 return {'client':client.strip() or first(r'Client\s*:?\s*([^\n]+)',s),'sales_ref':sales.strip() or first(r'Sales\s*Ref\.?\s*:?\s*([^\n]+)',s),'drawing':drawing.strip() or first(r'Drg\.?\s*No\.?\s*:?\s*([^\n]+)',s),'esd':esd.strip() or first(r'\b(SED\d+)\b',s) or first(r'ESD\s*No\.?\s*:?\s*([^\n]+)',s),'wo':wo.strip() or first(r'W\.?O\.?\s*No\.?\s*:?\s*([^\n]+)',s),'prep_by':prep.strip(),'voltage':norm_voltage(voltage)}

def clean_value(value):
    if not value:
        return ''
    value = value.replace('\n', ' ')
    value = re.sub(r'\s+', ' ', value)
    return value.strip()

def first_match(pattern, text, flags=re.IGNORECASE):
    if not text:
        return ''
    m = re.search(pattern, text, flags)
    return clean_value(m.group(1)) if m else ''

def extract_ct_ratio(ct_text):
    m = re.search(r'\bCTR\s*:\s*([0-9]+(?:\.[0-9]+)?(?:-[0-9]+(?:\.[0-9]+)?)?)\s*/', ct_text or '', re.I)
    return m.group(1) if m else ''

def extract_ct_secondary_current(ct_text):
    m = re.search(r'\bCTR\s*:\s*[0-9]+(?:\.[0-9]+)?(?:-[0-9]+(?:\.[0-9]+)?)?\s*/\s*([0-9]+(?:\.[0-9]+)?)\s*A', ct_text or '', re.I)
    return f'{m.group(1)}A' if m else ''

def extract_ct_core_lines(ct_text):
    labeled = re.findall(r'CORE\s*(\d+)\s*:\s*(.*?)(?=,\s*CORE\s*\d+\s*:|$)', ct_text or '', re.I)
    if labeled:
        return [clean_value(v) for _, v in sorted(labeled, key=lambda x:int(x[0]))]
    values=[]
    # The fixed MSLD extraction normalizes line breaks, so multiple CTR
    # entries can arrive on the same line. Stop each core at the next CTR
    # (or STC) instead of consuming the whole technical-data string.
    for m in re.finditer(r'\bCTR\s*:\s*(.*?)(?=\s+CTR\s*:|\s+STC\s*:|\s+SHORT\s+TIME\s+CURRENT\s*:|$)', ct_text or '', re.I):
        value=clean_value(m.group(1))
        if value: values.append(value)
    return values

def extract_ct_core_count(ct_text):
    explicit=first_match(r'NO\.?\s*OF\s*CORES\s*[:\-]?\s*(\d+)',ct_text)
    if explicit: return int(explicit)
    return len(extract_ct_core_lines(ct_text)) or 1

def generate_ct_code(ct_text):
    ratio=extract_ct_ratio(ct_text)
    secondary=extract_ct_secondary_current(ct_text)
    count=extract_ct_core_count(ct_text)
    return f'CT{ratio}{count}C-{secondary}' if ratio and secondary else ''

def extract_ct_type(ct_text):
    for pattern in [r'(EPOXY\s+CAST\s+RESIN\s*\(\s*WOUND\s+TYPE\s*\))',r'(WOUND\s+TYPE)',r'(WINDOW\s+TYPE)']:
        value=first_match(pattern,ct_text)
        if value:return value.upper()
    return ''

def build_ct_description(ct_text):
    t=extract_ct_type(ct_text)
    # Fixed CT wording required by the BOM template.
    return f'CURRENT TRANSFORMER EPOXY CAST RESIN ({t})' if t else 'CURRENT TRANSFORMER EPOXY CAST RESIN (WOUND TYPE)'

def build_ctr_line(ct_text):
    n=extract_ct_core_count(ct_text)
    word={1:'SINGLE',2:'TWO',3:'THREE'}.get(n,str(n))
    return f'{word} CORE CT'

def dis_search_text(dis_text):
    return re.sub(r'\s+', ' ', dis_text or '').strip()

def dis_field(dis_text, code, label=''):
    # The DIS is a fixed template. Use the numbered field as the primary
    # anchor because PDF text extraction can change spacing or wording.
    text = dis_search_text(dis_text)
    pattern = rf'{re.escape(code)}\s*(?:{label}\s*)?:?\s*(.*?)(?=\s+\d{{1,2}}\.\d{{2}}\.\d{{2}}\s+|$)'
    m = re.search(pattern, text, re.IGNORECASE)
    return clean_value(m.group(1)) if m else ''

def extract_annexure_make(dis_text, equipment):
    """Return the common CT/PT make list from DIS Annexure 1."""
    text = dis_search_text(dis_text)
    section_match = re.search(
        r'ANNEXURE\s*[-:]?\s*1\b(.*?)(?=ANNEXURE\s*[-:]?\s*[2-9]\b|$)',
        text, re.IGNORECASE | re.DOTALL
    )
    section = section_match.group(1) if section_match else text
    # The approved description lists all three makes together for both CT and PT.
    required = ['ECS', 'NPT', 'PRAGATI']
    present = [name for name in required if re.search(rf'\b{re.escape(name)}\b', section, re.IGNORECASE)]
    if len(present) == 3:
        return 'ECS/NPT/PRAGATI'
    # Annexure text extraction may lose part of a table. Keep the requested
    # standard list consistent rather than outputting a misleading single make.
    return 'ECS/NPT/PRAGATI'



def extract_ct_make(dis_text):
    text = dis_search_text(dis_text)
    # Fixed DIS make may appear as "PRAGATI MAKE", "PRAGATI/ECS MAKE",
    # "CT MAKE: PRAGATI", or "MAKE OF CT: PRAGATI". Do not depend on one
    # exact PDF text layout.
    patterns = [
        r'\b(PRAGATI(?:\s*/\s*[A-Z][A-Z0-9.&\-]*)+)\s+MAKE\b',
        r'\b(PRAGATI(?:\s*/\s*[A-Z][A-Z0-9.&\-]*)+)\b',
        r'\b(PRAGATI)\s+MAKE\b',
        r'(?:CT\s*/?\s*MAKE|CT/PT\s+MAKE|MAKE\s+OF\s+CT)\s*[:\-]?\s*([A-Za-z][A-Za-z0-9.&/\-]*(?:\s*/\s*[A-Za-z][A-Za-z0-9.&/\-]*)*)',
        r'\bMAKE\s*[:\-]\s*([A-Za-z][A-Za-z0-9.&/\-]*(?:\s*/\s*[A-Za-z][A-Za-z0-9.&/\-]*)*)',
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            value = re.sub(r'\s*/\s*', '/', clean_value(m.group(1)))
            if value and value.upper() not in {'OF','CT','PT','MAKE'}:
                return value
    return ''

def extract_rated_voltage(dis_text):
    value = dis_field(dis_text, '1.03.00', r'(?:Rated\s+(?:operational\s+)?voltage)')
    m = re.search(r'([0-9]+(?:\.[0-9]+)?)\s*(KV|kV)', value or '', re.IGNORECASE)
    if m:
        return f'{m.group(1)}kV'
    text = dis_search_text(dis_text)
    m = re.search(r'Rated\s+(?:operational\s+)?voltage\s*:?\s*([0-9]+(?:\.[0-9]+)?)\s*(KV|kV)', text, re.IGNORECASE)
    return f'{m.group(1)}kV' if m else ''

def extract_frequency(dis_text):
    value = dis_field(dis_text, '1.02.00', r'Main\s+System')
    text = value or dis_search_text(dis_text)
    m = re.search(r'([0-9]+(?:\.[0-9]+)?)\s*Hz', text, re.IGNORECASE)
    return f'{m.group(1)}Hz' if m else ''

def extract_bil(dis_text):
    values = []
    for code in ('1.04.00','1.06.00','1.07.00'):
        value = dis_field(dis_text, code)
        m = re.search(r'([0-9]+(?:\.[0-9]+)?)', value)
        if not m:
            return ''
        values.append(m.group(1))
    return '/'.join(values) + 'KVp'

def extract_panel_suitability(dis_text):
    # Source is DIS 2.01.00 "Type of switchboard", not 2.02.01 Location.
    # Example fixed-template value:
    # 8BK80(1000mm)+3AH3 VCB
    value = dis_field(dis_text, '2.01.00', r'Type\s+of\s+switchboard')
    if value:
        m = re.search(r'\b(8BK80)\s*\(?\s*(\d{3,4})\s*mm\s*\)?', value, re.I)
        if m:
            return f'{m.group(1).upper()}-{m.group(2)}mm WIDTH PANEL'
    # Fallback: search the whole DIS only if the numbered field was not
    # extracted cleanly.
    text = dis_search_text(dis_text)
    m = re.search(r'\b(8BK80)\s*\(?\s*(\d{3,4})\s*mm\s*\)?', text, re.I)
    if m:
        return f'{m.group(1).upper()}-{m.group(2)}mm WIDTH PANEL'
    return ''

def extract_pt_core_lines(pt_text):
    """Extract only each PT core's ratio, burden and accuracy class; never absorb adjacent MSLD equipment."""
    text = clean_value(pt_text)
    # The PT core table is followed by other equipment rows in flattened PDF text.
    # Extract bounded engineering tokens instead of consuming everything until the next CORE label.
    core_matches = re.findall(
        r'(?:\b(?:CORE\s*\d+\s*:\s*)?)'
        r'((?:\d+(?:\.\d+)?\s*kV\s*/\s*(?:√|\bSQRT\b)?\s*3\s*/\s*'
        r'\d+(?:\.\d+)?\s*/\s*(?:√|\bSQRT\b)?\s*3\s*,?\s*)?'
        r'\d+(?:\.\d+)?\s*VA\s*,?\s*CL\.?\s*-?\s*[A-Z0-9.]+)',
        text, re.IGNORECASE
    )
    result = []
    for value in core_matches:
        value = clean_value(value).rstrip(' ,;')
        if value and value not in result:
            result.append(value)
    return result



def extract_pt_fuse_rating(msld_text, dis_text):
    text = clean_value((msld_text or '') + ' ' + (dis_text or ''))
    patterns = [
        r'(?:IN[- ]?BUILT\s+)?PRIMARY\s+FUSE(?:\s+OF|\s+RATING)?\s*[:=]?\s*(\d+(?:\.\d+)?)\s*A\b',
        r'FUSE\s+(?:RATING|CURRENT)\s*[:=]?\s*(\d+(?:\.\d+)?)\s*A\b',
    ]
    for pattern in patterns:
        value = first_match(pattern, text)
        if value:
            return f'{value}A'
    return ''


def extract_pt_panel_suitability(msld_text, dis_text=''):
    """Use the exact same panel extraction source/rule as CT: DIS 2.01.00."""
    return extract_panel_suitability(dis_text)



def pt_spec(pt_text, msld_text, dis_text):
    """Format PT description from MSLD/DIS using the approved fixed layout."""
    source = clean_value((pt_text or '') + ' ' + (msld_text or ''))
    rated_voltage = extract_rated_voltage(dis_text)
    if not rated_voltage:
        rated_voltage = first_match(r'\b([0-9]+(?:\.[0-9]+)?\s*kV)\b', source)
        rated_voltage = re.sub(r'\s+', '', rated_voltage)
    # PT make is sourced from DIS.
    make = extract_annexure_make(dis_text, 'PT') or extract_ct_make(dis_text)
    if not make:
        dis_source = dis_search_text(dis_text)
        make = first_match(r'(?:Manufacturer(?:\s+name)?|Make\s+of\s+(?:PT|potential\s+transformer))\s*[:=\-]?\s*([A-Z][A-Z0-9.&/\-]*(?:\s*/\s*[A-Z][A-Z0-9.&/\-]*)?)', dis_source)
        if not make:
            make = first_match(r'\b([A-Z][A-Z0-9.&/\-]*(?:\s*/\s*[A-Z][A-Z0-9.&/\-]*)?)\s+MAKE\b', dis_source)
        make = re.sub(r'\s*/\s*', '/', make)
    # Preserve transformer type from MSLD when it is present.
    transformer_type = first_match(
        r'(POTENTIAL\s+TRANSFORMERS?\s+IN\s+.+?(?:DRAWOUT\s+TYPE|TYPE))',
        source
    )
    if not transformer_type:
        transformer_type = 'POTENTIAL TRANSFORMERS IN CAST RESIN EPOXY CASE (DRAWOUT TYPE)'
    transformer_type = clean_value(transformer_type).upper()
    cores = extract_pt_core_lines(pt_text) or extract_pt_core_lines(msld_text)
    core_count = len(cores) or 3
    core_word = {1: 'SINGLE', 2: 'TWO', 3: 'THREE'}.get(core_count, str(core_count))
    frequency = extract_frequency(dis_text)
    bil = extract_bil(dis_text)
    if bil:
        bil = bil.upper()
    panel = extract_pt_panel_suitability(msld_text, dis_text)
    lines = [
        f'PT {rated_voltage}' if rated_voltage else 'PT',
        f'{make.upper()} MAKE' if make else 'MAKE',
        transformer_type,
        '- SINGLE PHASE WITH IN BUILT PRIMARY FUSE OF 3.15A',
        f'{core_word} CORE PT, AS PER IS/IEC STANDARD',
        'AS GIVEN BELOW:-',
    ]
    for index, value in enumerate(cores, 1):
        lines.append(f'- CORE {index}: {value}')
    lines.extend([
        f'- RATED FREQUENCY: {frequency}' if frequency else '- RATED FREQUENCY:',
        '- VF: 1.2COUNT/1.9 FOR 8 HOURS',
        f'- BIL: {bil}' if bil else '- BIL:',
        'INSULATION CLASS:- B',
        f'SUITABLE FOR {panel}' if panel else 'SUITABLE FOR',
    ])
    return lines

def ct_spec(ct_text, dis_text):
    ct_code=generate_ct_code(ct_text)
    ct_make=extract_annexure_make(dis_text, 'CT') or extract_ct_make(dis_text)
    # Retain the legacy default only if Annexure 1 and the DIS text expose no CT make.
    if not ct_make:
        ct_make = ''
    ct_description=build_ct_description(ct_text)
    rated_voltage=extract_rated_voltage(dis_text)
    frequency=extract_frequency(dis_text)
    stc=first_match(r'(?:SHORT\s+TIME\s+CURRENT|STC)\s*:?\s*(.*?)(?=\s+CTR\s*:|\s+CORE\s*\d+\s*:|\s+STC\s*:|\s*$)',ct_text or '')
    bil=extract_bil(dis_text)
    cores=extract_ct_core_lines(ct_text)
    panel=extract_panel_suitability(dis_text)
    lines=[
        ct_code or 'CT',
        f'{ct_make.upper()} MAKE' if ct_make else '',
        ct_description,
        '- SINGLE PHASE, AS PER IS/IEC STANDARD',
        f'- RATED VOLTAGE: {rated_voltage}' if rated_voltage else '- RATED VOLTAGE:',
        f'- RATED FREQUENCY: {frequency}' if frequency else '- RATED FREQUENCY:',
        f'- SHORT TIME CURRENT: {stc}' if stc else '- SHORT TIME CURRENT:',
        f'- BIL: {bil}' if bil else '- BIL:',
        'INSULATION CLASS:-B',
        'AS GIVEN BELOW:-',
        build_ctr_line(ct_text)
    ]
    for i,value in enumerate(cores,1):
        lines.append(f'- CORE {i}: {value}')
    lines.extend([
        f'SUITABLE FOR {panel}' if panel else 'SUITABLE FOR',
        'CT SECONDARY TERMINAL ON P2 SIDE'
    ])
    return lines


def build_rows(feeder_info,records,msld_text='',dis_text=''):
 feeder_name=feeder_info[0].get('designation') or feeder_info[0].get('name','') if feeder_info else ''; feeder_qty=feeder_info[0].get('quantity','1') if feeder_info else '1'; feeder_match=re.search(r'\d+',str(feeder_qty)); feeder_qty_num=int(feeder_match.group()) if feeder_match else 1; rows=[]
 for i,r in enumerate(records,1):
  relay_analysis = None
  if r['master_code']=='CT':
   ct_row_text = ' '.join([r.get('designation',''), r.get('description',''), r.get('details','')])
   spec_lines=ct_spec(ct_row_text,dis_text)
   spec='\n'.join(spec_lines)
   editable_fields=['INSULATION CLASS:-B','CT SECONDARY TERMINAL ON P2 SIDE']
   # Core count changes the position of the SUITABLE FOR line. Resolve
   # editable positions from their actual labels so 3-core CTs never mark
   # the SUITABLE FOR row as editable by mistake.
   editable_indices = []
   for editable_label in editable_fields:
    target = editable_label.split(' ON P2 SIDE')[0]
    line_index = next((idx for idx, line in enumerate(spec_lines)
                       if target in line), None)
    if line_index is not None:
     editable_indices.append(line_index)
  elif r['master_code']=='PT':
   pt_row_text = ' '.join([r.get('designation',''), r.get('description',''), r.get('details','')])
   spec_lines=pt_spec(pt_row_text, msld_text, dis_text)
   spec='\n'.join(spec_lines)
   editable_fields=['INSULATION CLASS:- B']
   editable_indices=[12]
  elif r['master_code']=='MCB':
   mcb_item = mcb_master_match(r.get('description',''), r.get('details',''), r.get('designation',''))
   if mcb_item:
    spec, spec_lines = mcb_spec(mcb_item)
   else:
    spec = r['description']+((' | '+r['details']) if r['details'] else '')
    spec_lines = [clean(spec)] if spec else []
   editable_fields=[]
   editable_indices=[]
  elif r['master_code']=='7SJ6611':
   # Relay validation must use only this equipment row. Passing the full MSLD
   # could borrow an MLFB from another section/row and create a relay BOM line
   # that is not actually specified for this designation.
   relay_source = ' '.join([r.get('designation',''), r.get('description',''), r.get('details','')])
   relay_analysis = analyze_7sj66(relay_source)
   # Never drop a detected relay row just because its MLFB is missing or
   # its chart-coded options conflict with the MSLD details. Preserve the
   # source text in the BOM so engineering details are not lost, and attach
   # validation feedback for the UI warning list.
   source_relay_text = clean_value(' | '.join(
    part for part in [r.get('description',''), r.get('details','')] if part
   ))
   relay_validation = relay_analysis.get('validation') or {}
   has_mlfb = bool(relay_analysis.get('fields', {}).get('mlfb', {}).get('value'))
   # Only validate/report a missing MLFB when this MSLD row explicitly
   # identifies a 7SJ66 relay. A generic numerical-relay label is not enough.
   has_7sj66_evidence = bool(re.search(r'\b7SJ66\w*\b', relay_source, re.IGNORECASE))
   if not has_mlfb and not has_7sj66_evidence:
    continue
   # When an MLFB is extracted, always show the structured description.
   # Validation errors/warnings stay separate and do not replace BOM details.
   if has_mlfb:
    spec = relay_analysis['description']
    spec_lines = spec.splitlines()
   else:
    # Keep source text only when the MSLD actually identifies a relay.
    spec = source_relay_text
    spec_lines = [spec] if spec else []
   editable_fields=[]
   editable_indices=[]
  elif r['master_code']=='E2_LED':
   spec_lines=[
    'LED',
    'PHILIPS/REPUTED MAKE LED 5W- TUBE FIXTURE',
    'SUITABLE FOR 240V AC, 50Hz AC SUPPLY',
    'SUITABLE FOR SURFACE MOUNTING'
   ]
   spec='\n'.join(spec_lines)
   editable_fields=[]
   editable_indices=[]
  elif r['master_code']=='LED':
   h789 = led_specs_for_h7_h8_h9(r.get('description',''), r.get('details',''), r.get('designation',''))
   if h789:
    # Split the source quantity across the colour-specific H7/H8/H9 rows.
    # Example: source qty 3 and three colours -> 1 each, total remains 3 per feeder.
    group_qty = int(r.get('quantity') or 1)
    split_count = len(h789)
    base_qty, remainder = divmod(group_qty, split_count)
    for part_index, part in enumerate(h789):
     part_lines=part['specification_lines']
     part_qty = base_qty + (1 if part_index < remainder else 0)
     rows.append({'sr':len(rows)+1,'specification':'\n'.join(part_lines),'specification_lines':part_lines,'editable_fields':[],'editable_indices':[],'designation':part['designation'],'feeder_name':feeder_name,'feeder_qty':feeder_qty,'total':part_qty*feeder_qty_num,'eqpt_qty':part_qty,'mpd':'','amd':'','master_code':'LED'})
    continue
   spec_lines, led_item = led_spec(r.get('description',''), r.get('details',''), r.get('designation',''))
   spec='\n'.join(spec_lines) if spec_lines else r['description']+((' | '+r['details']) if r['details'] else '')
   spec_lines=spec_lines or ([clean(spec)] if spec else [])
   editable_fields=[]
   editable_indices=[]
  else:
   spec=r['description']+((' | '+r['details']) if r['details'] else '')
   spec_lines=[clean(spec)] if spec else []
   editable_fields=[]
   editable_indices=[]
  rows.append({'sr':len(rows)+1,'specification':spec,'specification_lines':spec_lines,'editable_fields':editable_fields,'editable_indices':editable_indices,'designation':r['designation'],'feeder_name':feeder_name,'feeder_qty':feeder_qty,'total':(r['quantity'] or 1)*feeder_qty_num,'eqpt_qty':(r['quantity'] or 1),'mpd':'','amd':'','master_code':r['master_code'],'relay_validation':relay_analysis.get('validation') if relay_analysis else None})
  if r['master_code']=='MCB':
   mcb_source_text = clean_value(' '.join([r.get('description',''), r.get('details','')])).upper()
   has_aux_1no_1nc = bool(re.search(r'\b1\s*NO\s*\+\s*1\s*NC\b', mcb_source_text) and re.search(r'\bAUX(?:ILIARY)?\b|\bCONTACTS?\b', mcb_source_text))
   if has_aux_1no_1nc:
    mcb_qty = (r['quantity'] or 1)
    addon_item = load_mcb_addon_master()
    if addon_item and addon_item.get('discription'):
     addon_spec = addon_item['discription']
     addon_lines = [part.strip() for part in addon_spec.split(',') if part.strip()]
     rows.append({'sr':len(rows)+1,'specification':'\n'.join(addon_lines),'specification_lines':addon_lines,'editable_fields':[],'editable_indices':[],'designation':str(r.get('designation',''))+'A','feeder_name':feeder_name,'feeder_qty':feeder_qty,'total':mcb_qty*feeder_qty_num,'eqpt_qty':mcb_qty,'mpd':'','amd':'','master_code':'MCB ADD-ON'})
 return rows,feeder_name,feeder_qty

def extract_from_files(msld_bytes,msld_name,dis_bytes,dis_name,client,sales,drawing,esd,wo,prep,voltage):
 msld_text=pdf_text(msld_bytes,msld_name); dis_text=pdf_text(dis_bytes,dis_name); same=bool(msld_text.strip()) and re.sub(r'\s+','',msld_text)==re.sub(r'\s+','',dis_text); feeder_info,records,warnings=extract_fixed_table(msld_bytes,msld_name)
 if same:warnings.append('The same PDF was supplied in both MSLD and DIS slots. It was processed once; DIS was used as the validation copy.')
 elif dis_text.strip():warnings.append('DIS supplied: used as the fixed specification source; MSLD remains the primary project-data source.')
 rows,feeder_name,feeder_qty=build_rows(feeder_info,records,msld_text,dis_text)
 for row in rows:
  validation=row.get('relay_validation')
  if validation:
   relay_label='7SJ66 relay '+str(row.get('designation',''))
   warnings.extend([relay_label+': '+message for message in validation.get('errors',[])])
   warnings.extend([relay_label+': '+message for message in validation.get('warnings',[])])
 h=header(msld_text,dis_text,client,sales,drawing,esd,wo,prep,voltage); ph=extract_header_from_page(msld_bytes,msld_name)
 for key in ('client','sales_ref','drawing','wo'):
  if ph.get(key):h[key]=ph[key]
 if ph.get('esd'):h['esd']=ph['esd']
 source=msld_text+'\n'+dis_text; q=first(r'\bQty\.\s*:\s*(\d+x?)\b',source) or first(r'\bQTY\.\s*:\s*(\d+x?)\b',source) or ph.get('qty',''); v=norm_voltage(voltage) or first(r'\b(6\.6|11|33)\s*KV\b',source); description=f'{v}kV SWITCHBOARD' if v else 'SWITCHBOARD'
 if not rows:warnings.append('No equipment rows were confidently extracted from the fixed MSLD table.')
 unmatched=[r['designation'] for r in records if r['master_code']=='UNMATCHED']
 if unmatched:warnings.append('Master Data match not available for: '+', '.join(unmatched)+'. Source description was preserved; no engineering item was guessed.')
 unmatched_mcb=[r['designation'] for r in records if r['master_code']=='MCB' and not mcb_master_match(r.get('description',''),r.get('details',''),r.get('designation',''))]
 if unmatched_mcb:warnings.append('MCB specifications could not be fully verified against the master DISCRIPTION for: '+', '.join(unmatched_mcb)+'. Source text was preserved; verify before issuing the BOM.')
 return {'header':h,'document_no':'SI EA/CS/FR/EG/015','revision':'1.0','effective_date':'17/07/2026','created_by':'EA CS ENGG','description':description,'rows':rows,'feeder_name':feeder_name,'feeder_qty':feeder_qty,'qty':q,'warnings':warnings,'feeder_details':feeder_info,'same_input_file':same}


def extract_multiple_from_files(msld_files, dis_bytes, dis_name, client, sales, drawing, esd, wo, prep, voltage):
    """Build one BOM across many MSLDs, using one common DIS as the specification source."""
    dis_text = pdf_text(dis_bytes, dis_name)
    all_rows = []
    feeders = []
    warnings = []
    header_source = ""
    first_ph = {}
    qty_text = ""
    for idx, item in enumerate(msld_files):
        msld_bytes, msld_name = item
        msld_text = pdf_text(msld_bytes, msld_name)
        if idx == 0:
            header_source = msld_text
            first_ph = extract_header_from_page(msld_bytes, msld_name)
            source = msld_text + "\n" + dis_text
            qty_text = first(r'\bQty\.\s*:\s*(\d+x?)\b', source) or first(r'\bQTY\.\s*:\s*(\d+x?)\b', source) or first_ph.get('qty', '')
        feeder_info, records, file_warnings = extract_fixed_table(msld_bytes, msld_name)
        if not feeder_info:
            warnings.append(f"{msld_name}: feeder information could not be confidently extracted.")
            feeder_info = [{'name': os.path.splitext(os.path.basename(msld_name))[0], 'designation': os.path.splitext(os.path.basename(msld_name))[0], 'quantity': '1'}]
        feeder_name = feeder_info[0].get('designation') or feeder_info[0].get('name') or f"MSLD {idx + 1}"
        feeder_qty_text = feeder_info[0].get('quantity', '1')
        qty_match = re.search(r'\d+', str(feeder_qty_text))
        feeder_qty_num = int(qty_match.group()) if qty_match else 1
        feeders.append({'name': str(feeder_name), 'qty': str(feeder_qty_text), 'quantity': feeder_qty_num, 'source_file': msld_name})
        built, _, _ = build_rows(feeder_info, records, msld_text, dis_text)
        for row in built:
            validation = row.get('relay_validation')
            if validation:
                relay_label = str(msld_name) + ': 7SJ66 relay ' + str(row.get('designation', ''))
                warnings.extend([relay_label + ': ' + message for message in validation.get('errors', [])])
                warnings.extend([relay_label + ': ' + message for message in validation.get('warnings', [])])
        for row in built:
            row['_feeder_index'] = idx
            all_rows.append(row)
        warnings.extend([f"{msld_name}: {w}" for w in file_warnings])
        unmatched = [r['designation'] for r in records if r['master_code'] == 'UNMATCHED']
        if unmatched:
            warnings.append(f"{msld_name}: Master Data match not available for: " + ', '.join(unmatched) + ". Source description was preserved; no engineering item was guessed.")
        unmatched_mcb = [r['designation'] for r in records if r['master_code'] == 'MCB' and not mcb_master_match(r.get('description',''), r.get('details',''), r.get('designation',''))]
        if unmatched_mcb:
            warnings.append(f"{msld_name}: MCB specifications could not be fully verified against the master DISCRIPTION for: " + ', '.join(unmatched_mcb) + ". Source text was preserved; verify before issuing the BOM.")
    # Merge identical equipment only when its normalized specification and category match.
    merged = {}
    for row in all_rows:
        idx = row.pop('_feeder_index')
        spec_lines = row.get('specification_lines') or []
        identity_text = row.get('specification') or '\n'.join(spec_lines)
        key = (str(row.get('master_code', '')).strip().upper(), re.sub(r'\s+', ' ', identity_text).strip().casefold())
        if key not in merged:
            entry = dict(row)
            entry['sr'] = len(merged) + 1
            entry['feeder_quantities'] = [0] * len(feeders)
            entry['designation_by_feeder'] = [''] * len(feeders)
            entry['total'] = 0
            entry['eqpt_qty'] = 0
            merged[key] = entry
        entry = merged[key]
        qty = row.get('eqpt_qty')
        if qty is None:
            qty = 1
        try:
            qty = int(qty)
        except (ValueError, TypeError):
            qty = 1
        # Feeder columns show equipment quantity per typical panel. Total is
        # calculated only after merging, using that feeder's panel multiplier.
        entry['feeder_quantities'][idx] += qty
        designation = str(row.get('designation', '') or '').strip()
        if designation:
            existing = entry['designation_by_feeder'][idx]
            if isinstance(existing, list):
                if designation not in existing:
                    existing.append(designation)
            elif existing and existing != designation:
                entry['designation_by_feeder'][idx] = [existing, designation]
            else:
                entry['designation_by_feeder'][idx] = [designation]
        entry['eqpt_qty'] += qty
    rows = list(merged.values())
    for row in rows:
        quantities = row.get('feeder_quantities') or []
        per_feeder_quantities = []
        total = 0
        for i, feeder in enumerate(feeders):
            try:
                equipment_qty = int(quantities[i] or 0) if i < len(quantities) else 0
            except (TypeError, ValueError):
                equipment_qty = 0
            try:
                feeder_qty = int(feeder.get('quantity') or 1)
            except (TypeError, ValueError):
                feeder_qty = 1
            per_feeder_quantities.append(equipment_qty)
            total += equipment_qty * max(1, feeder_qty)
        row['feeder_quantities'] = per_feeder_quantities
        row['total'] = total
    def equipment_order(row):
        code = str(row.get('master_code', '')).strip().upper()
        spec = str(row.get('specification', '')).upper()
        text = ' '.join([code, spec, str(row.get('designation', '')).upper()])
        # Keep corresponding equipment from all MSLDs together in a stable,
        # engineering-oriented category order.
        if code == 'CT' or re.search(r'\bCURRENT TRANSFORMER\b|\bC\.T\.\b', text):
            rank = 0
        elif code in ('PT', 'VT') or re.search(r'\b(POTENTIAL|VOLTAGE) TRANSFORMER\b|\bP\.T\.\b', text):
            rank = 1
        elif code in ('LA', 'SA') or 'SURGE ARRESTER' in text:
            rank = 2
        elif code in ('CB', 'VCB', 'BREAKER') or 'CIRCUIT BREAKER' in text:
            rank = 3
        elif code.startswith('MCB') or re.search(r'\bMCB\b', text):
            rank = 4
        elif code in ('LED', 'LAMP') or 'INDICATION LAMP' in text or re.search(r'\bLED\b', text):
            rank = 5
        else:
            rank = 6
        return (rank, code, re.sub(r'\s+', ' ', str(row.get('specification', ''))).casefold(), str(row.get('designation', '')).casefold())
    rows.sort(key=equipment_order)
    for i, row in enumerate(rows, 1):
        row['sr'] = i
        designations = []
        for values in row.pop('designation_by_feeder', []):
            if isinstance(values, str):
                values = [values]
            for value in values or []:
                value = str(value).strip()
                if value and value not in designations:
                    designations.append(value)
        row['designation'] = ', '.join(designations)
        row['feeder_name'] = ''
        row['feeder_qty'] = ''
    h = header(header_source, dis_text, client, sales, drawing, esd, wo, prep, voltage)
    for key in ('client', 'sales_ref', 'drawing', 'wo'):
        if first_ph.get(key):
            h[key] = first_ph[key]
    if first_ph.get('esd'):
        h['esd'] = first_ph['esd']
    source = header_source + '\n' + dis_text
    v = norm_voltage(voltage) or first(r'\b(6\.6|11|33)\s*KV\b', source)
    description = f'{v}kV SWITCHBOARD' if v else 'SWITCHBOARD'
    if not rows:
        warnings.append('No equipment rows were confidently extracted from the supplied MSLD files.')
    if dis_text.strip():
        warnings.append('One common DIS supplied: used as the specification source for all MSLDs.')
    return {
        'header': h, 'document_no': 'SI EA/CS/FR/EG/015', 'revision': '1.0',
        'effective_date': '17/07/2026', 'created_by': 'EA CS ENGG',
        'description': description, 'rows': rows, 'feeders': feeders,
        'feeder_name': feeders[0]['name'] if feeders else '',
        'feeder_qty': feeders[0]['qty'] if feeders else '',
        'qty': qty_text, 'warnings': list(dict.fromkeys(warnings)),
        'feeder_details': feeders, 'same_input_file': False
    }

@app.post('/api/bom/check-customers')
async def check_msld_customers(
    mslds: list[UploadFile] = File(...),
    dis: UploadFile = File(...),
    client: str = Form(''),
    wo: str = Form(''),
    voltage: str = Form(''),
    user: str = Depends(current_user)
):
    """Check MSLD and common DIS header values before BOM generation."""
    results = []
    for upload in mslds:
        data = await upload.read()
        filename = upload.filename or 'MSLD.pdf'
        extracted = extract_header_from_page(data, filename)
        source_text = pdf_text(data, filename)
        customer = clean_value(extracted.get('client', '')).strip(' :-')
        if not customer:
            customer = first(r'Client\s*[:\-]*\s*([^\n]+)', source_text).strip(' :-') or first(r'Customer\s*Name\s*[:\-]*\s*([^\n]+)', source_text).strip(' :-')
        work_order = clean_value(extracted.get('wo', '')).strip(' :-')
        if not work_order:
            work_order = first(r'W\.?\s*O\.?\s*(?:No\.?)?\s*[:\-]\s*([^\n]+)', source_text).strip(' :-')
        voltage_match = first(r'\b(6\.6|11|33)\s*k?V\b', source_text)
        results.append({'filename': filename, 'customer': clean_value(customer), 'work_order': clean_value(work_order), 'voltage': clean_value(voltage_match)})
    dis_bytes = await dis.read()
    dis_name = dis.filename or 'DIS.pdf'
    dis_header = extract_header_from_page(dis_bytes, dis_name)
    dis_text = pdf_text(dis_bytes, dis_name)
    # DIS is a two-column footer table. Parse the Client/W.O. values from
    # words on the same visual row, stopping before the right-hand columns.
    dis_words = page_words(pdf_pages(dis_bytes, dis_name)[0]) if pdf_pages(dis_bytes, dis_name) else []
    def footer_value(label_pattern):
        # Read only words to the right of the requested label on the same visual row.
        labels = [
            w for w in dis_words
            if re.fullmatch(label_pattern, w['text'].strip(' :.-'), re.IGNORECASE)
        ]
        for label_word in labels:
            row = sorted(
                [w for w in dis_words if abs(w['y'] - label_word['y']) <= 8 and w['x'] > label_word['x']],
                key=lambda w: w['x']
            )
            picked = []
            for word in row:
                token = word['text'].strip(' :.-')
                if not token:
                    continue
                if re.match(
                    r'^(?:Sales|Project|W\.?O\.?|Item|Qty|Description|Sh\.?|Work|Details|Drg\.?|Date|Prep|Ckd|Client)\b',
                    token,
                    re.IGNORECASE
                ):
                    break
                picked.append(token)
            value = clean_value(' '.join(picked)).strip(' :-')
            if value:
                return value
        return ''

    dis_customer = footer_value(r'^Client$')
    dis_wo = footer_value(r'^(?:W\.?O\.?|Work\s*Order)$')
    if not dis_customer:
        dis_customer = clean_value(dis_header.get('client', '')).strip(' :-')
    if not dis_wo:
        dis_wo = clean_value(dis_header.get('wo', '')).strip(' :-')
    dis_voltage = first(r'\b(6\.6|11|33)\s*k?V\b', dis_text)
    def distinct(values):
        return list(dict.fromkeys(re.sub(r'\s+', ' ', str(v)).strip() for v in values if str(v or '').strip()))
    customers = distinct([x['customer'] for x in results])
    work_orders = distinct([x['work_order'] for x in results])
    voltages = distinct([x['voltage'] for x in results])
    mismatches = []
    if len({x.casefold() for x in customers}) > 1:
        mismatches.append('The uploaded MSLD PDFs contain different customer names.')
    if len({x.casefold() for x in work_orders}) > 1:
        mismatches.append('The uploaded MSLD PDFs contain different work order numbers.')
    if len({x.casefold() for x in voltages}) > 1:
        mismatches.append('The uploaded MSLD PDFs contain different voltage values.')
    entered = {'customer': clean_value(client), 'work_order': clean_value(wo), 'voltage': clean_value(voltage)}
    for label, values, dis_value, entered_value in [
        ('Customer name', customers, dis_customer, entered['customer']),
        ('Work order number', work_orders, dis_wo, entered['work_order']),
        ('Voltage', voltages, dis_voltage, entered['voltage'])
    ]:
        all_values = distinct(values + ([dis_value] if dis_value else []) + ([entered_value] if entered_value else []))
        if label == 'Voltage':
            all_values = distinct([norm_voltage(value) or value for value in all_values])
        if len({x.casefold() for x in all_values}) > 1:
            mismatches.append(f'{label} differs between entered details, MSLD and/or DIS.')

    # Do not silently proceed when a PDF header field could not be extracted.
    # The existing verification modal displays these messages and lets the user
    # review the extracted values before choosing whether to continue.
    for item in results:
        if not item['work_order']:
            mismatches.append(f"Work order number could not be read from MSLD: {item['filename']}.")
        if not item['voltage']:
            mismatches.append(f"Voltage could not be read from MSLD: {item['filename']}.")
    if not dis_wo:
        mismatches.append(f"Work order number could not be read from DIS: {dis_name}.")
    if not dis_voltage:
        mismatches.append(f"Voltage could not be read from DIS: {dis_name}.")
    return {
        'mslds': results, 'customers': customers, 'different_customers': len({x.casefold() for x in customers}) > 1,
        'unknown_customer_files': [x['filename'] for x in results if not x['customer']],
        'mismatches': list(dict.fromkeys(mismatches)),
        'dis': {'filename': dis_name, 'customer': dis_customer, 'work_order': dis_wo, 'voltage': clean_value(dis_voltage)}
    }

@app.post('/api/bom/preview-multiple')
async def preview_multiple(
    client: str = Form(''), sales_ref: str = Form(''), drawing: str = Form(''),
    esd: str = Form(''), wo: str = Form(''), prep_by: str = Form(''),
    voltage: str = Form(''), mslds: list[UploadFile] = File(...),
    dis: UploadFile = File(...), user: str = Depends(current_user)
):
    if not mslds:
        raise HTTPException(status_code=400, detail='Upload at least one MSLD PDF.')
    dis_name = dis.filename or 'DIS.pdf'
    dis_bytes = await dis.read()
    items = []
    filenames = []
    for upload in mslds:
        filename = upload.filename or 'MSLD.pdf'
        items.append((await upload.read(), filename))
        filenames.append(filename)
    result = extract_multiple_from_files(items, dis_bytes, dis_name, client, sales_ref, drawing, esd, wo, prep_by, voltage)
    import json
    with db() as conn:
        conn.execute(
            """INSERT INTO bom_history (username, customer_name, work_order_no, voltage, msld_files, dis_filename, row_count)
               VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)""",
            (user, str(client or result.get('header', {}).get('client', '')), str(wo or result.get('header', {}).get('wo', '')),
             str(voltage or ''), json.dumps(filenames), dis_name, len(result.get('rows') or []))
        )
        conn.commit()
    return result




class SiemensLookupRequest(BaseModel):
    mlfb: str


@app.post('/api/siemens/product-lookup')
def siemens_product_lookup(payload: SiemensLookupRequest, user: str = Depends(current_user)):
    """POC: live lookup of a supplied 7SJ66 MLFB on Siemens Industry Mall."""
    return lookup_siemens_product(payload.mlfb)

@app.get('/api/admin/bom-history')
def admin_bom_history(user: str = Depends(require_admin)):
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) AS total FROM bom_history").fetchone()['total']
        rows = conn.execute(
            """SELECT id, username, customer_name, work_order_no, voltage, msld_files,
                      dis_filename, row_count, created_at
               FROM bom_history ORDER BY created_at DESC LIMIT 500"""
        ).fetchall()
    return {'total_generated': total, 'history': rows}

def export_combined_book(payload):
    wb = Workbook()
    ws = wb.active
    ws.title = 'BOM'
    thin = Side(style='thin')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    header_data = payload.get('header', {})
    feeders = payload.get('feeders') or []
    rows = payload.get('rows') or []
    n = max(1, len(feeders))
    qty_col = 4
    feeder_start_col = 5
    total_col = feeder_start_col + n
    mpd_col, amd_col = total_col + 1, total_col + 2
    last_col = amd_col
    ws.page_setup.orientation = 'landscape'
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 1
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_margins.left = .25
    ws.page_margins.right = .25
    ws.page_margins.top = .45
    ws.page_margins.bottom = .4
    ws.sheet_view.showGridLines = False
    spec_width = max(36, min(78, max([max((len(line) for line in str(r.get('specification','')).splitlines()), default=0) for r in rows] + [36]) * 0.75))
    designation_width = max(14, min(26, max([len(str(r.get('designation',''))) for r in rows] + [14]) * 0.9))
    feeder_width = max(10, min(18, max([len(str(f.get('name',''))) for f in feeders] + [8]) * 0.9))
    widths = [9, spec_width, designation_width, 9] + [feeder_width] * n + [13, 7, 7]
    for col, width in enumerate(widths, 1):
        ws.column_dimensions[__import__('openpyxl').utils.get_column_letter(col)].width = width
    ws.merge_cells(start_row=1, start_column=1, end_row=2, end_column=min(3, last_col))
    ws.cell(1, 1, '-: Equipment List :-')
    ws.cell(1, 1).font = Font(name='Courier New', size=10)
    ws.cell(1, 1).alignment = Alignment(horizontal='center', vertical='center')
    meta_col = max(4, last_col - 2)
    ws.merge_cells(start_row=1, start_column=meta_col, end_row=2, end_column=last_col)
    ws.cell(1, meta_col, f'Doc.No.: {payload.get("document_no","SI EA/CS/FR/EG/015")}\nRev.No.: {payload.get("revision","1.0")}, Eff.Dt: {payload.get("effective_date","17/07/2026")}\nCreated By: {payload.get("created_by","EA CS ENGG")}')
    ws.cell(1, meta_col).font = Font(name='Arial', size=7)
    ws.cell(1, meta_col).alignment = Alignment(horizontal='right', vertical='top', wrap_text=True)
    ws.row_dimensions[1].height = 34
    ws.row_dimensions[2].height = 16
    for col, label in [(1,'EQPT.NO'),(2,'SPECIFICATION'),(3,'DESIGNATION'),(total_col,'TOTAL EQPT QTY.'),(mpd_col,'MPD'),(amd_col,'AMD')]:
        ws.cell(4, col, label)
    for col in [1,2,3,total_col,mpd_col,amd_col]:
        ws.merge_cells(start_row=4, start_column=col, end_row=6, end_column=col)
    ws.merge_cells(start_row=5, start_column=qty_col, end_row=6, end_column=qty_col)
    ws.cell(5,qty_col,'QTY.')
    ws.merge_cells(start_row=4, start_column=feeder_start_col, end_row=4, end_column=feeder_start_col+n-1)
    ws.cell(4,feeder_start_col,'FEEDER TYPICAL')
    for idx, feeder in enumerate(feeders):
        col=feeder_start_col+idx
        ws.cell(5,col,feeder.get('name',''))
        ws.cell(6,col,feeder.get('quantity') or feeder.get('qty') or 1)
    for r in range(4,7):
        for c in range(1,last_col+1):
            cell=ws.cell(r,c)
            cell.border=border
            cell.alignment=Alignment(horizontal='center',vertical='center',wrap_text=True)
            cell.font=Font(name='Courier New',size=8,bold=True)
    ws.row_dimensions[4].height=23
    ws.row_dimensions[5].height=18
    ws.row_dimensions[6].height=28
    for i,row in enumerate(rows[:64],7):
        quantities = row.get('feeder_quantities') or []
        vals=[row.get('sr',''),row.get('specification',''),row.get('designation','')]
        vals.append('')
        vals.extend(quantities[j] if j < len(quantities) and quantities[j] not in (0, '0', None, '') else '' for j in range(n))
        total_value = row.get('total', '')
        vals.extend(['' if total_value in (0, '0') else total_value, '', ''])
        for col,val in enumerate(vals,1):
            cell=ws.cell(i,col,val)
            cell.border=border
            cell.font=Font(name='Courier New',size=7)
            cell.alignment=Alignment(horizontal='center' if col!=2 else 'left',vertical='center',wrap_text=col==2)
        spec_lines = str(row.get('specification', '')).splitlines() or ['']
        chars_per_line = max(12, int(spec_width * 1.35))
        wrapped_lines = sum(max(1, (len(line) + chars_per_line - 1) // chars_per_line) for line in spec_lines)
        ws.row_dimensions[i].height = max(18, min(240, 10 * wrapped_lines + 6))
    for i in range(7+len(rows[:64]),70):
        for c in range(1,last_col+1):
            ws.cell(i,c).border=border
            ws.cell(i,c).font=Font(name='Courier New',size=7)
        ws.row_dimensions[i].height=15
    # Keep worksheet view focused on the equipment list. Document metadata is
    # printed in the page header/footer when the user presses Ctrl+P.
    ws.oddHeader.center.text = "&B Equipment List &B"
    ws.oddHeader.right.text = f"Doc.No.: {payload.get('document_no','SI EA/CS/FR/EG/015')}\\nRev.No.: {payload.get('revision','1.0')}  Eff.Dt: {payload.get('effective_date','17/07/2026')}"
    ws.oddHeader.right.size = 7
    ws.oddFooter.left.text = f"Client: {header_data.get('client','')}  |  Sales Ref: {header_data.get('sales_ref','')}\\nDescription: {payload.get('description','')}  |  WO: {header_data.get('wo','')}  |  Drawing: {header_data.get('drawing','')}"
    ws.oddFooter.center.text = f"Prepared by: {header_data.get('prep_by','')}  |  Qty: {payload.get('qty','')}  |  ESD: {header_data.get('esd','')}"
    ws.oddFooter.right.text = "Page &P of &N"
    ws.oddFooter.left.size = 7
    ws.oddFooter.center.size = 7
    ws.oddFooter.right.size = 7
    ws.print_area=f'A1:{__import__("openpyxl").utils.get_column_letter(last_col)}76'
    return wb

def export_combined_pdf(payload):
    out=io.BytesIO()
    doc=SimpleDocTemplate(out,pagesize=A4,rightMargin=5*mm,leftMargin=5*mm,topMargin=6*mm,bottomMargin=20*mm)
    styles=getSampleStyleSheet()
    title=ParagraphStyle('combinedtitle',parent=styles['Normal'],fontName='Helvetica-Bold',fontSize=6,leading=7,alignment=TA_CENTER)
    cell=ParagraphStyle('combinedcell',parent=styles['Normal'],fontName='Helvetica',fontSize=4.2,leading=4.8,spaceAfter=0,spaceBefore=0,wordWrap='CJK')
    center=ParagraphStyle('combinedcenter',parent=cell,alignment=TA_CENTER)
    head=ParagraphStyle('combinedhead',parent=cell,fontName='Helvetica-Bold',alignment=TA_CENTER,fontSize=4.0,leading=4.5)
    feeders=payload.get('feeders') or []
    rows=payload.get('rows') or []
    n=max(1,len(feeders))
    data=[[Paragraph('EQPT.NO',head),Paragraph('SPECIFICATION',head),Paragraph('DESIGNATION',head),''] + [Paragraph('FEEDER<br/>TYPICAL',head) if i==0 else '' for i in range(n)] + [Paragraph('TOTAL<br/>EQPT QTY.',head),Paragraph('MPD',head),Paragraph('AMD',head)]]
    data.append(['','','',Paragraph('QTY.',head)]+[Paragraph(str(f.get('name','')),head) for f in feeders]+['','',''])
    data.append(['','','','']+[Paragraph(str(f.get('quantity') or f.get('qty') or 1),center) for f in feeders]+['','',''])
    for row in rows[:64]:
        spec=str(row.get('specification','') or '').replace('&','&amp;').replace('<','&lt;').replace('>','&gt;').replace('\n','<br/>')
        qs=row.get('feeder_quantities') or []
        data.append([Paragraph(str(row.get('sr','')),center),Paragraph(spec,cell),Paragraph(str(row.get('designation','') or ''),center),Paragraph('',center)]+[Paragraph(str(qs[i] if i<len(qs) and qs[i] not in (0, '0', None, '') else ''),center) for i in range(n)]+[Paragraph('' if row.get('total') in (0, '0') else str(row.get('total') or ''),center),'',''])
    available=200*mm
    fixed=[8*mm,66*mm,25*mm,9*mm,12*mm,10*mm,10*mm]
    # Allocate a little more space to long feeder labels, while keeping the
    # complete table inside landscape A4. Paragraph cells wrap and expand rows.
    remaining=max(8*mm, available-sum(fixed))
    feeder_labels=[len(str(f.get('name',''))) for f in feeders]
    weights=[max(1.0,min(2.0,length/10.0)) for length in feeder_labels]
    weight_sum=sum(weights) or n
    feeder_widths=[remaining*w/weight_sum for w in weights] if feeders else [remaining]
    widths=[fixed[0],fixed[1],fixed[2],fixed[3]]+feeder_widths+[fixed[4],fixed[5],fixed[6]]
    tbl=Table(data,colWidths=widths,rowHeights=[18,14,20]+[None]*(len(data)-3),repeatRows=3)
    spans=[('SPAN',(0,0),(0,2)),('SPAN',(1,0),(1,2)),('SPAN',(2,0),(2,2)),('SPAN',(3,1),(3,2)),('SPAN',(4,0),(3+n,0)),('SPAN',(4+n,0),(4+n,2)),('SPAN',(5+n,0),(5+n,2)),('SPAN',(6+n,0),(6+n,2))]
    # A compact first-row group label and feeder-specific second/third header rows.
    tbl.setStyle(TableStyle(spans+[('GRID',(0,0),(-1,-1),0.45,colors.black),('BACKGROUND',(0,0),(-1,2),colors.HexColor('#d9e7f0')),('VALIGN',(0,0),(-1,-1),'MIDDLE'),('ALIGN',(0,0),(-1,2),'CENTER'),('LEFTPADDING',(0,0),(-1,-1),2),('RIGHTPADDING',(0,0),(-1,-1),2),('TOPPADDING',(0,0),(-1,-1),1),('BOTTOMPADDING',(0,0),(-1,-1),1)]))
    story=[Paragraph('-: Equipment List :-',title),Spacer(1,3*mm),tbl]
    h=payload.get('header',{})
    def footer(canvas, doc_obj):
        canvas.saveState()
        w,hp=doc_obj.pagesize
        y=8*mm
        canvas.setFont('Helvetica',6.5)
        canvas.drawString(6*mm,y+7*mm,f"Client: {h.get('client','')}  Sales Ref: {h.get('sales_ref','')}")
        canvas.drawString(6*mm,y+4*mm,f"Description: {payload.get('description','')}  WO: {h.get('wo','')}  Drawing: {h.get('drawing','')}")
        canvas.drawString(6*mm,y+1*mm,f"Prepared by: {h.get('prep_by','')}  Qty: {payload.get('qty','')}  ESD: {h.get('esd','')}")
        canvas.restoreState()
    doc.build(story,onFirstPage=footer,onLaterPages=footer,canvasmaker=NumberedCanvas)
    out.seek(0)
    return out

@app.get('/')
def root():
    return {
        'service': 'ELEXORA 2 API',
        'status': 'running',
        'health': '/health',
        'docs': '/docs',
    }


@app.get('/health')
def health():
    return {'status':'ok'}

@app.on_event("startup")
def startup():
    init_auth_db()

@app.post('/api/auth/signup')
def signup(payload: AuthRequest):
    username = payload.username.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username):
        raise HTTPException(status_code=400, detail="Username must be 3-32 characters and use only letters, numbers, dot, underscore or hyphen.")
    errors = validate_password(payload.password)
    if errors:
        raise HTTPException(status_code=400, detail="Password must contain " + ", ".join(errors) + ".")
    try:
        with db() as conn:
            conn.execute(
                "INSERT INTO users(username,password_hash) VALUES(%s,%s)",
                (username.lower(), password_hash(payload.password))
            )
            conn.commit()
    except Exception as exc:
        if getattr(exc, "sqlstate", None) == "23505":
            raise HTTPException(status_code=409, detail="That username already exists. Please choose another.")
        raise
    return {"message":"Account created successfully. You can now sign in."}

@app.post('/api/auth/login')
def login(payload: AuthRequest):
    username = payload.username.strip().lower()
    with db() as conn:
        row = conn.execute(
            "SELECT id,password_hash,username FROM users WHERE username=%s",
            (username,)
        ).fetchone()
        if not row or not password_ok(payload.password, row["password_hash"]):
            raise HTTPException(status_code=401, detail="Invalid username or password.")
        token = secrets.token_urlsafe(32)
        expires = int(datetime.now().timestamp()) + TOKEN_TTL_SECONDS
        conn.execute(
            "INSERT INTO sessions(token,user_id,expires_at) VALUES(%s,%s,%s)",
            (token, row["id"], expires)
        )
        conn.commit()
    return {"token":token,"username":row["username"],"expires_at":expires}

@app.post('/api/auth/logout')
def logout(authorization: str = Header(default="")):
    token = authorization[7:].strip() if authorization.startswith("Bearer ") else ""
    if token:
        with db() as conn:
            conn.execute("DELETE FROM sessions WHERE token=%s", (token,))
            conn.commit()
    return {"message":"Signed out."}

@app.post('/api/bom/preview')
async def preview(client:str=Form(''),sales_ref:str=Form(''),drawing:str=Form(''),esd:str=Form(''),wo:str=Form(''),prep_by:str=Form(''),voltage:str=Form(''),msld:UploadFile=File(...),dis:UploadFile=File(...),user:str=Depends(current_user)):
 m,d=await msld.read(),await dis.read(); return extract_from_files(m,msld.filename,d,dis.filename,client,sales_ref,drawing,esd,wo,prep_by,voltage)

def export_book(payload):
 if payload.get('feeders'):
  return export_combined_book(payload)
 wb=Workbook(); ws=wb.active; ws.title='BOM'; thin=Side(style='thin'); border=Border(left=thin,right=thin,top=thin,bottom=thin); h=payload.get('header',{}); rows=payload.get('rows',[]); ws.page_setup.orientation='portrait'; ws.page_setup.paperSize=ws.PAPERSIZE_A4; ws.page_setup.fitToWidth=1; ws.page_setup.fitToHeight=1; ws.sheet_properties.pageSetUpPr.fitToPage=True; ws.page_margins.left=.58; ws.page_margins.right=.75; ws.page_margins.top=.60; ws.page_margins.bottom=.55; ws.sheet_view.showGridLines=False
 for col,width in {'A':12.43,'B':80.99,'C':18.43,'D':10.0,'E':11.87,'F':9.10,'G':5.87,'H':5.87}.items():ws.column_dimensions[col].width=width
 ws.merge_cells('A1:E2'); ws['A1']='-: Equipment List :-'; ws['A1'].font=Font(name='Courier New',size=10); ws['A1'].alignment=Alignment(horizontal='center',vertical='center'); ws['F1']=f'Doc.No.: {payload.get("document_no","SI EA/CS/FR/EG/015")}\nRev.No.: {payload.get("revision","1.0")}, Eff.Dt: {payload.get("effective_date","17/07/2026")}\nCreated By: {payload.get("created_by","EA CS ENGG")}'; ws['F1'].alignment=Alignment(horizontal='right',vertical='top',wrap_text=True); ws['F1'].font=Font(name='Arial',size=7); ws.merge_cells('F1:H2'); ws.row_dimensions[1].height=34; ws.row_dimensions[2].height=16
 for c,v in [('A4','EQPT.NO'),('B4','SPECIFICATION'),('C4','DESIGNATION'),('F4','TOTAL EQPT QTY.'),('G4','MPD'),('H4','AMD')]:ws[c]=v
 ws.merge_cells('A4:A6'); ws.merge_cells('B4:B6'); ws.merge_cells('C4:C6'); ws.merge_cells('D4:E4'); ws.merge_cells('F4:F6'); ws.merge_cells('G4:G6'); ws.merge_cells('H4:H6'); ws['D4']='FEEDER TYPICAL'; ws['D5']='QTY.'; ws['E5']=payload.get('feeder_name',''); ws['E6']=payload.get('feeder_qty','')
 for row in range(4,7):
  for col in range(1,9):ws.cell(row,col).border=border; ws.cell(row,col).alignment=Alignment(horizontal='center',vertical='center',wrap_text=True); ws.cell(row,col).font=Font(name='Courier New',size=10 if row==4 else 9,bold=True)
 ws.row_dimensions[4].height=23.25; ws.row_dimensions[5].height=18; ws.row_dimensions[6].height=18
 for i,row in enumerate(rows[:64],7):
  vals=[row.get('sr',''),row.get('specification',''),row.get('designation',''),' ',row.get('eqpt_qty') if row.get('eqpt_qty') is not None else '',row.get('total') if row.get('total') is not None else '','','']
  for col,val in enumerate(vals,1):ws.cell(i,col,val).border=border; ws.cell(i,col).font=Font(name='Courier New',size=7); ws.cell(i,col).alignment=Alignment(horizontal='center' if col!=2 else 'left',vertical='center',wrap_text=True if col==2 else False)
  ws.row_dimensions[i].height=max(15.6,min(180,15.6*max(1,len(row.get('specification_lines',[])))))
 for i in range(7+len(rows[:64]),70):
  for col in range(1,9):ws.cell(i,col).border=border; ws.cell(i,col).font=Font(name='Courier New',size=7)
  ws.row_dimensions[i].height=15.6
 fr=72; footer_left=[('Item No.','100'),('Client :',h.get('client','')),('Sales Ref No.:',h.get('sales_ref','')),('DATE :',datetime.now().strftime('%d.%m.%Y'))]; footer_mid=[('Description :',payload.get('description','')),('W.O. No.:',h.get('wo','')),('Drg. No.:',h.get('drawing',''))]; footer_right=[('PRE.BY :',h.get('prep_by','')),('Qty.:',payload.get('qty','')),('ESD No.:',h.get('esd','')),('','1 of 1')]
 for i,(label,value) in enumerate(footer_left):
  ws.cell(fr+i,1,label).font=Font(name='Arial',size=7); ws.cell(fr+i,2,value).font=Font(name='Arial',size=7)
  ws.cell(fr+i,1).alignment=Alignment(horizontal='left',vertical='center'); ws.cell(fr+i,2).alignment=Alignment(horizontal='left',vertical='center')
 for i,(label,value) in enumerate(footer_mid):
  ws.cell(fr+i,4,label).font=Font(name='Arial',size=7); ws.cell(fr+i,5,value).font=Font(name='Arial',size=7)
  ws.cell(fr+i,4).alignment=Alignment(horizontal='left',vertical='center'); ws.cell(fr+i,5).alignment=Alignment(horizontal='left',vertical='center')
 for i,(label,value) in enumerate(footer_right):
  ws.cell(fr+i,7,label).font=Font(name='Arial',size=7); ws.cell(fr+i,8,value).font=Font(name='Arial',size=7)
  ws.cell(fr+i,7).alignment=Alignment(horizontal='right',vertical='center'); ws.cell(fr+i,8).alignment=Alignment(horizontal='right',vertical='center')
 ws.print_area='A1:H76'; return wb
@app.post('/api/bom/export')
async def export(payload:dict, user:str=Depends(current_user)):
 wb=export_book(payload);out=io.BytesIO();wb.save(out);out.seek(0);return StreamingResponse(out,media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',headers={'Content-Disposition':'attachment; filename=ELEXORA_BOM.xlsx'})

class NumberedCanvas:
    def __init__(self, *args, **kwargs):
        from reportlab.pdfgen import canvas as pdfcanvas
        self._canvas = pdfcanvas.Canvas(*args, **kwargs)
        self._saved_page_states = []

    def __getattr__(self, name):
        return getattr(self._canvas, name)

    def showPage(self):
        self._saved_page_states.append(dict(self._canvas.__dict__))
        self._canvas._startPage()

    def save(self):
        total = len(self._saved_page_states)
        for state in self._saved_page_states:
            self._canvas.__dict__.update(state)
            self._draw_page_number(total)
            self._canvas.showPage()
        self._canvas.save()

    def _draw_page_number(self, total):
        # Page number occupies its own fourth line in the right footer block.
        w, hp = A4
        self._canvas.setFont('Helvetica', 6.5)
        self._canvas.drawRightString(
            w - 8*mm, 4*mm,
            f'{self._canvas.getPageNumber()} of {total}'
        )


def export_pdf(payload):
    if payload.get('feeders'):
        return export_combined_pdf(payload)
    out = io.BytesIO()
    doc = SimpleDocTemplate(
        out, pagesize=A4,
        rightMargin=8*mm, leftMargin=8*mm,
        topMargin=12*mm, bottomMargin=28*mm
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('bomtitle', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=9, leading=11, alignment=TA_CENTER)
    meta_style = ParagraphStyle('bommeta', parent=styles['Normal'], fontName='Helvetica', fontSize=6.5, leading=8, alignment=TA_RIGHT)
    cell_style = ParagraphStyle('cell', parent=styles['Normal'], fontName='Helvetica', fontSize=6.3, leading=7.5, spaceAfter=0, spaceBefore=0)
    cell_center = ParagraphStyle('cellcenter', parent=cell_style, alignment=TA_CENTER)
    head_style = ParagraphStyle('head', parent=cell_style, fontName='Helvetica-Bold', alignment=TA_CENTER, leading=8)
    h = payload.get('header', {})
    rows = payload.get('rows', [])
    story = [
        Paragraph('-: Equipment List :-', title_style),
        Spacer(1, 4*mm),
    ]
    data = [
        [Paragraph('EQPT.NO',head_style), Paragraph('SPECIFICATION',head_style), Paragraph('DESIGNATION',head_style),
         Paragraph('FEEDER TYPICAL',head_style), '', Paragraph('TOTAL<br/>EQPT<br/>QTY.',head_style), Paragraph('MPD',head_style), Paragraph('AMD',head_style)],
        ['', '', '', Paragraph('QTY.',head_style), Paragraph(str(payload.get('feeder_name','')),head_style), '', '', ''],
        ['', '', '', '', Paragraph(str(payload.get('feeder_qty','')),cell_center), '', '', '']
    ]
    for row in rows[:64]:
        spec = row.get('specification','') or ''
        data.append([
            Paragraph(str(row.get('sr','') or ''), cell_center),
            Paragraph(str(spec).replace('&','&amp;').replace('<','&lt;').replace('>','&gt;').replace('\n','<br/>'), cell_style),
            Paragraph(str(row.get('designation','') or ''), cell_center),
            '',
            Paragraph(str(row.get('eqpt_qty','') or ''), cell_center),
            Paragraph(str(row.get('total','') or ''), cell_center),
            '', ''
        ])
    col_widths=[16*mm, 81*mm, 24*mm, 17*mm, 20*mm, 18*mm, 9*mm, 9*mm]
    tbl=Table(data,colWidths=col_widths,repeatRows=3)
    tbl.setStyle(TableStyle([
        ('SPAN',(0,0),(0,2)),('SPAN',(1,0),(1,2)),('SPAN',(2,0),(2,2)),
        ('SPAN',(3,0),(4,0)),('SPAN',(5,0),(5,2)),('SPAN',(6,0),(6,2)),('SPAN',(7,0),(7,2)),
        ('GRID',(0,0),(-1,-1),0.45,colors.black),
        ('BACKGROUND',(0,0),(-1,2),colors.HexColor('#d9e7f0')),
        ('VALIGN',(0,0),(-1,-1),'MIDDLE'),
        ('ALIGN',(0,0),(-1,2),'CENTER'),
        ('LEFTPADDING',(0,0),(-1,-1),2),('RIGHTPADDING',(0,0),(-1,-1),2),
        ('TOPPADDING',(0,0),(-1,-1),2),('BOTTOMPADDING',(0,0),(-1,-1),2),
    ]))
    story.append(tbl)

    def footer(canvas, doc_obj):
        canvas.saveState()
        w, hp = A4
        y = 11*mm
        canvas.setFont('Helvetica', 6.5)

        # Fixed footer zones matching the supplied BOM: left / center / right.
        left_x = 8*mm
        mid_x = 82*mm
        right_x = 164*mm
        line_step = 3*mm

        left=[
            ('Item No.','100'),
            ('Client :',h.get('client','')),
            ('Sales Ref No.:',h.get('sales_ref','')),
            ('DATE :',datetime.now().strftime('%d.%m.%Y'))
        ]
        mid=[
            ('Description :',payload.get('description','')),
            ('W.O. No.:',h.get('wo','')),
            ('Drg. No.:',h.get('drawing',''))
        ]
        right=[
            ('PRE.BY :',h.get('prep_by','')),
            ('Qty.:',payload.get('qty','')),
            ('ESD No.:',h.get('esd',''))
        ]

        for i,(label,value) in enumerate(left):
            canvas.drawString(left_x, y+(2.5-i)*line_step, (label+' '+str(value)).strip())

        # Keep the center footer inside its own zone so it cannot overlap the right block.
        from reportlab.pdfbase.pdfmetrics import stringWidth
        mid_max_x = right_x - 5*mm
        for i,(label,value) in enumerate(mid):
            text_value = (label+' '+str(value)).strip()
            max_width = mid_max_x - mid_x
            while stringWidth(text_value, 'Helvetica', 6.5) > max_width and len(text_value) > len(label) + 4:
                text_value = text_value[:-2].rstrip() + '...'
            canvas.drawString(mid_x, y+(2.5-i)*line_step, text_value)

        for i,(label,value) in enumerate(right):
            canvas.drawString(right_x, y+(2.5-i)*line_step, (label+' '+str(value)).strip())

        canvas.restoreState()
    doc.build(story,onFirstPage=footer,onLaterPages=footer,canvasmaker=NumberedCanvas)
    out.seek(0)
    return out

@app.post('/api/bom/export-pdf')
async def export_pdf_endpoint(payload:dict, user:str=Depends(current_user)):
    out=export_pdf(payload)
    return StreamingResponse(out,media_type='application/pdf',headers={'Content-Disposition':f'attachment; filename=BOM_{re.sub(r"[^A-Za-z0-9._-]+", "_", str(payload.get("header", {}).get("wo", "WO"))).strip("_")}.pdf'})
@app.post('/api/bom/preview-multiple')
async def preview_multiple(
    client: str = Form(''), sales_ref: str = Form(''), drawing: str = Form(''),
    esd: str = Form(''), wo: str = Form(''), prep_by: str = Form(''),
    voltage: str = Form(''), mslds: list[UploadFile] = File(...),
    dis: UploadFile = File(...), user: str = Depends(current_user)
):
    if not mslds:
        raise HTTPException(status_code=400, detail='Upload at least one MSLD PDF.')
    dis_name = dis.filename or 'DIS.pdf'
    dis_bytes = await dis.read()
    items = []
    filenames = []
    for upload in mslds:
        filename = upload.filename or 'MSLD.pdf'
        items.append((await upload.read(), filename))
        filenames.append(filename)
    result = extract_multiple_from_files(items, dis_bytes, dis_name, client, sales_ref, drawing, esd, wo, prep_by, voltage)
    import json
    with db() as conn:
        conn.execute(
            """INSERT INTO bom_history (username, customer_name, work_order_no, voltage, msld_files, dis_filename, row_count)
               VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)""",
            (user, str(client or result.get('header', {}).get('client', '')), str(wo or result.get('header', {}).get('wo', '')),
             str(voltage or ''), json.dumps(filenames), dis_name, len(result.get('rows') or []))
        )
        conn.commit()
    return result




class SiemensLookupRequest(BaseModel):
    mlfb: str


@app.post('/api/siemens/product-lookup')
def siemens_product_lookup(payload: SiemensLookupRequest, user: str = Depends(current_user)):
    """POC: live lookup of a supplied 7SJ66 MLFB on Siemens Industry Mall."""
    return lookup_siemens_product(payload.mlfb)

@app.get('/api/admin/bom-history')
def admin_bom_history(user: str = Depends(require_admin)):
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) AS total FROM bom_history").fetchone()['total']
        rows = conn.execute(
            """SELECT id, username, customer_name, work_order_no, voltage, msld_files,
                      dis_filename, row_count, created_at
               FROM bom_history ORDER BY created_at DESC LIMIT 500"""
        ).fetchall()
    return {'total_generated': total, 'history': rows}

def export_combined_book(payload):
    wb = Workbook()
    ws = wb.active
    ws.title = 'BOM'
    thin = Side(style='thin')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    header_data = payload.get('header', {})
    feeders = payload.get('feeders') or []
    rows = payload.get('rows') or []
    n = max(1, len(feeders))
    qty_col = 4
    feeder_start_col = 5
    total_col = feeder_start_col + n
    mpd_col, amd_col = total_col + 1, total_col + 2
    last_col = amd_col
    ws.page_setup.orientation = 'landscape'
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 1
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_margins.left = .25
    ws.page_margins.right = .25
    ws.page_margins.top = .45
    ws.page_margins.bottom = .4
    ws.sheet_view.showGridLines = False
    spec_width = max(36, min(78, max([max((len(line) for line in str(r.get('specification','')).splitlines()), default=0) for r in rows] + [36]) * 0.75))
    designation_width = max(14, min(26, max([len(str(r.get('designation',''))) for r in rows] + [14]) * 0.9))
    feeder_width = max(10, min(18, max([len(str(f.get('name',''))) for f in feeders] + [8]) * 0.9))
    widths = [9, spec_width, designation_width, 9] + [feeder_width] * n + [13, 7, 7]
    for col, width in enumerate(widths, 1):
        ws.column_dimensions[__import__('openpyxl').utils.get_column_letter(col)].width = width
    ws.merge_cells(start_row=1, start_column=1, end_row=2, end_column=min(3, last_col))
    ws.cell(1, 1, '-: Equipment List :-')
    ws.cell(1, 1).font = Font(name='Courier New', size=10)
    ws.cell(1, 1).alignment = Alignment(horizontal='center', vertical='center')
    meta_col = max(4, last_col - 2)
    ws.merge_cells(start_row=1, start_column=meta_col, end_row=2, end_column=last_col)
    ws.cell(1, meta_col, f'Doc.No.: {payload.get("document_no","SI EA/CS/FR/EG/015")}\nRev.No.: {payload.get("revision","1.0")}, Eff.Dt: {payload.get("effective_date","17/07/2026")}\nCreated By: {payload.get("created_by","EA CS ENGG")}')
    ws.cell(1, meta_col).font = Font(name='Arial', size=7)
    ws.cell(1, meta_col).alignment = Alignment(horizontal='right', vertical='top', wrap_text=True)
    ws.row_dimensions[1].height = 34
    ws.row_dimensions[2].height = 16
    for col, label in [(1,'EQPT.NO'),(2,'SPECIFICATION'),(3,'DESIGNATION'),(total_col,'TOTAL EQPT QTY.'),(mpd_col,'MPD'),(amd_col,'AMD')]:
        ws.cell(4, col, label)
    for col in [1,2,3,total_col,mpd_col,amd_col]:
        ws.merge_cells(start_row=4, start_column=col, end_row=6, end_column=col)
    ws.merge_cells(start_row=5, start_column=qty_col, end_row=6, end_column=qty_col)
    ws.cell(5,qty_col,'QTY.')
    ws.merge_cells(start_row=4, start_column=feeder_start_col, end_row=4, end_column=feeder_start_col+n-1)
    ws.cell(4,feeder_start_col,'FEEDER TYPICAL')
    for idx, feeder in enumerate(feeders):
        col=feeder_start_col+idx
        ws.cell(5,col,feeder.get('name',''))
        ws.cell(6,col,feeder.get('quantity') or feeder.get('qty') or 1)
    for r in range(4,7):
        for c in range(1,last_col+1):
            cell=ws.cell(r,c)
            cell.border=border
            cell.alignment=Alignment(horizontal='center',vertical='center',wrap_text=True)
            cell.font=Font(name='Courier New',size=8,bold=True)
    ws.row_dimensions[4].height=23
    ws.row_dimensions[5].height=18
    ws.row_dimensions[6].height=28
    for i,row in enumerate(rows[:64],7):
        quantities = row.get('feeder_quantities') or []
        vals=[row.get('sr',''),row.get('specification',''),row.get('designation','')]
        vals.append('')
        vals.extend(quantities[j] if j < len(quantities) and quantities[j] not in (0, '0', None, '') else '' for j in range(n))
        total_value = row.get('total', '')
        vals.extend(['' if total_value in (0, '0') else total_value, '', ''])
        for col,val in enumerate(vals,1):
            cell=ws.cell(i,col,val)
            cell.border=border
            cell.font=Font(name='Courier New',size=7)
            cell.alignment=Alignment(horizontal='center' if col!=2 else 'left',vertical='center',wrap_text=col==2)
        spec_lines = str(row.get('specification', '')).splitlines() or ['']
        chars_per_line = max(12, int(spec_width * 1.35))
        wrapped_lines = sum(max(1, (len(line) + chars_per_line - 1) // chars_per_line) for line in spec_lines)
        ws.row_dimensions[i].height = max(18, min(240, 10 * wrapped_lines + 6))
    for i in range(7+len(rows[:64]),70):
        for c in range(1,last_col+1):
            ws.cell(i,c).border=border
            ws.cell(i,c).font=Font(name='Courier New',size=7)
        ws.row_dimensions[i].height=15
    # Keep worksheet view focused on the equipment list. Document metadata is
    # printed in the page header/footer when the user presses Ctrl+P.
    ws.oddHeader.center.text = "&B Equipment List &B"
    ws.oddHeader.right.text = f"Doc.No.: {payload.get('document_no','SI EA/CS/FR/EG/015')}\\nRev.No.: {payload.get('revision','1.0')}  Eff.Dt: {payload.get('effective_date','17/07/2026')}"
    ws.oddHeader.right.size = 7
    ws.oddFooter.left.text = f"Client: {header_data.get('client','')}  |  Sales Ref: {header_data.get('sales_ref','')}\\nDescription: {payload.get('description','')}  |  WO: {header_data.get('wo','')}  |  Drawing: {header_data.get('drawing','')}"
    ws.oddFooter.center.text = f"Prepared by: {header_data.get('prep_by','')}  |  Qty: {payload.get('qty','')}  |  ESD: {header_data.get('esd','')}"
    ws.oddFooter.right.text = "Page &P of &N"
    ws.oddFooter.left.size = 7
    ws.oddFooter.center.size = 7
    ws.oddFooter.right.size = 7
    ws.print_area=f'A1:{__import__("openpyxl").utils.get_column_letter(last_col)}76'
    return wb

def export_combined_pdf(payload):
    out=io.BytesIO()
    doc=SimpleDocTemplate(out,pagesize=A4,rightMargin=5*mm,leftMargin=5*mm,topMargin=6*mm,bottomMargin=20*mm)
    styles=getSampleStyleSheet()
    title=ParagraphStyle('combinedtitle',parent=styles['Normal'],fontName='Helvetica-Bold',fontSize=6,leading=7,alignment=TA_CENTER)
    cell=ParagraphStyle('combinedcell',parent=styles['Normal'],fontName='Helvetica',fontSize=4.2,leading=4.8,spaceAfter=0,spaceBefore=0,wordWrap='CJK')
    center=ParagraphStyle('combinedcenter',parent=cell,alignment=TA_CENTER)
    head=ParagraphStyle('combinedhead',parent=cell,fontName='Helvetica-Bold',alignment=TA_CENTER,fontSize=4.0,leading=4.5)
    feeders=payload.get('feeders') or []
    rows=payload.get('rows') or []
    n=max(1,len(feeders))
    data=[[Paragraph('EQPT.NO',head),Paragraph('SPECIFICATION',head),Paragraph('DESIGNATION',head),''] + [Paragraph('FEEDER<br/>TYPICAL',head) if i==0 else '' for i in range(n)] + [Paragraph('TOTAL<br/>EQPT QTY.',head),Paragraph('MPD',head),Paragraph('AMD',head)]]
    data.append(['','','',Paragraph('QTY.',head)]+[Paragraph(str(f.get('name','')),head) for f in feeders]+['','',''])
    data.append(['','','','']+[Paragraph(str(f.get('quantity') or f.get('qty') or 1),center) for f in feeders]+['','',''])
    for row in rows[:64]:
        spec=str(row.get('specification','') or '').replace('&','&amp;').replace('<','&lt;').replace('>','&gt;').replace('\n','<br/>')
        qs=row.get('feeder_quantities') or []
        data.append([Paragraph(str(row.get('sr','')),center),Paragraph(spec,cell),Paragraph(str(row.get('designation','') or ''),center),Paragraph('',center)]+[Paragraph(str(qs[i] if i<len(qs) and qs[i] not in (0, '0', None, '') else ''),center) for i in range(n)]+[Paragraph('' if row.get('total') in (0, '0') else str(row.get('total') or ''),center),'',''])
    available=200*mm
    fixed=[8*mm,66*mm,25*mm,9*mm,12*mm,10*mm,10*mm]
    # Allocate a little more space to long feeder labels, while keeping the
    # complete table inside landscape A4. Paragraph cells wrap and expand rows.
    remaining=max(8*mm, available-sum(fixed))
    feeder_labels=[len(str(f.get('name',''))) for f in feeders]
    weights=[max(1.0,min(2.0,length/10.0)) for length in feeder_labels]
    weight_sum=sum(weights) or n
    feeder_widths=[remaining*w/weight_sum for w in weights] if feeders else [remaining]
    widths=[fixed[0],fixed[1],fixed[2],fixed[3]]+feeder_widths+[fixed[4],fixed[5],fixed[6]]
    tbl=Table(data,colWidths=widths,rowHeights=[18,14,20]+[None]*(len(data)-3),repeatRows=3)
    spans=[('SPAN',(0,0),(0,2)),('SPAN',(1,0),(1,2)),('SPAN',(2,0),(2,2)),('SPAN',(3,1),(3,2)),('SPAN',(4,0),(3+n,0)),('SPAN',(4+n,0),(4+n,2)),('SPAN',(5+n,0),(5+n,2)),('SPAN',(6+n,0),(6+n,2))]
    # A compact first-row group label and feeder-specific second/third header rows.
    tbl.setStyle(TableStyle(spans+[('GRID',(0,0),(-1,-1),0.45,colors.black),('BACKGROUND',(0,0),(-1,2),colors.HexColor('#d9e7f0')),('VALIGN',(0,0),(-1,-1),'MIDDLE'),('ALIGN',(0,0),(-1,2),'CENTER'),('LEFTPADDING',(0,0),(-1,-1),2),('RIGHTPADDING',(0,0),(-1,-1),2),('TOPPADDING',(0,0),(-1,-1),1),('BOTTOMPADDING',(0,0),(-1,-1),1)]))
    story=[Paragraph('-: Equipment List :-',title),Spacer(1,3*mm),tbl]
    h=payload.get('header',{})
    def footer(canvas, doc_obj):
        canvas.saveState()
        w,hp=doc_obj.pagesize
        y=8*mm
        canvas.setFont('Helvetica',6.5)
        canvas.drawString(6*mm,y+7*mm,f"Client: {h.get('client','')}  Sales Ref: {h.get('sales_ref','')}")
        canvas.drawString(6*mm,y+4*mm,f"Description: {payload.get('description','')}  WO: {h.get('wo','')}  Drawing: {h.get('drawing','')}")
        canvas.drawString(6*mm,y+1*mm,f"Prepared by: {h.get('prep_by','')}  Qty: {payload.get('qty','')}  ESD: {h.get('esd','')}")
        canvas.restoreState()
    doc.build(story,onFirstPage=footer,onLaterPages=footer,canvasmaker=NumberedCanvas)
    out.seek(0)
    return out

@app.get('/')
def root():
    return {
        'service': 'ELEXORA 2 API',
        'status': 'running',
        'health': '/health',
        'docs': '/docs',
    }


@app.get('/health')
def health():
    return {'status':'ok'}

@app.on_event("startup")
def startup():
    init_auth_db()

@app.post('/api/auth/signup')
def signup(payload: AuthRequest):
    username = payload.username.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username):
        raise HTTPException(status_code=400, detail="Username must be 3-32 characters and use only letters, numbers, dot, underscore or hyphen.")
    errors = validate_password(payload.password)
    if errors:
        raise HTTPException(status_code=400, detail="Password must contain " + ", ".join(errors) + ".")
    try:
        with db() as conn:
            conn.execute(
                "INSERT INTO users(username,password_hash) VALUES(%s,%s)",
                (username.lower(), password_hash(payload.password))
            )
            conn.commit()
    except Exception as exc:
        if getattr(exc, "sqlstate", None) == "23505":
            raise HTTPException(status_code=409, detail="That username already exists. Please choose another.")
        raise
    return {"message":"Account created successfully. You can now sign in."}

@app.post('/api/auth/login')
def login(payload: AuthRequest):
    username = payload.username.strip().lower()
    with db() as conn:
        row = conn.execute(
            "SELECT id,password_hash,username FROM users WHERE username=%s",
            (username,)
        ).fetchone()
        if not row or not password_ok(payload.password, row["password_hash"]):
            raise HTTPException(status_code=401, detail="Invalid username or password.")
        token = secrets.token_urlsafe(32)
        expires = int(datetime.now().timestamp()) + TOKEN_TTL_SECONDS
        conn.execute(
            "INSERT INTO sessions(token,user_id,expires_at) VALUES(%s,%s,%s)",
            (token, row["id"], expires)
        )
        conn.commit()
    return {"token":token,"username":row["username"],"expires_at":expires}

@app.post('/api/auth/logout')
def logout(authorization: str = Header(default="")):
    token = authorization[7:].strip() if authorization.startswith("Bearer ") else ""
    if token:
        with db() as conn:
            conn.execute("DELETE FROM sessions WHERE token=%s", (token,))
            conn.commit()
    return {"message":"Signed out."}

@app.post('/api/bom/preview')
async def preview(client:str=Form(''),sales_ref:str=Form(''),drawing:str=Form(''),esd:str=Form(''),wo:str=Form(''),prep_by:str=Form(''),voltage:str=Form(''),msld:UploadFile=File(...),dis:UploadFile=File(...),user:str=Depends(current_user)):
 m,d=await msld.read(),await dis.read(); return extract_from_files(m,msld.filename,d,dis.filename,client,sales_ref,drawing,esd,wo,prep_by,voltage)

def export_book(payload):
 if payload.get('feeders'):
  return export_combined_book(payload)
 wb=Workbook(); ws=wb.active; ws.title='BOM'; thin=Side(style='thin'); border=Border(left=thin,right=thin,top=thin,bottom=thin); h=payload.get('header',{}); rows=payload.get('rows',[]); ws.page_setup.orientation='portrait'; ws.page_setup.paperSize=ws.PAPERSIZE_A4; ws.page_setup.fitToWidth=1; ws.page_setup.fitToHeight=1; ws.sheet_properties.pageSetUpPr.fitToPage=True; ws.page_margins.left=.58; ws.page_margins.right=.75; ws.page_margins.top=.60; ws.page_margins.bottom=.55; ws.sheet_view.showGridLines=False
 for col,width in {'A':12.43,'B':80.99,'C':18.43,'D':10.0,'E':11.87,'F':9.10,'G':5.87,'H':5.87}.items():ws.column_dimensions[col].width=width
 ws.merge_cells('A1:E2'); ws['A1']='-: Equipment List :-'; ws['A1'].font=Font(name='Courier New',size=10); ws['A1'].alignment=Alignment(horizontal='center',vertical='center'); ws['F1']=f'Doc.No.: {payload.get("document_no","SI EA/CS/FR/EG/015")}\nRev.No.: {payload.get("revision","1.0")}, Eff.Dt: {payload.get("effective_date","17/07/2026")}\nCreated By: {payload.get("created_by","EA CS ENGG")}'; ws['F1'].alignment=Alignment(horizontal='right',vertical='top',wrap_text=True); ws['F1'].font=Font(name='Arial',size=7); ws.merge_cells('F1:H2'); ws.row_dimensions[1].height=34; ws.row_dimensions[2].height=16
 for c,v in [('A4','EQPT.NO'),('B4','SPECIFICATION'),('C4','DESIGNATION'),('F4','TOTAL EQPT QTY.'),('G4','MPD'),('H4','AMD')]:ws[c]=v
 ws.merge_cells('A4:A6'); ws.merge_cells('B4:B6'); ws.merge_cells('C4:C6'); ws.merge_cells('D4:E4'); ws.merge_cells('F4:F6'); ws.merge_cells('G4:G6'); ws.merge_cells('H4:H6'); ws['D4']='FEEDER TYPICAL'; ws['D5']='QTY.'; ws['E5']=payload.get('feeder_name',''); ws['E6']=payload.get('feeder_qty','')
 for row in range(4,7):
  for col in range(1,9):ws.cell(row,col).border=border; ws.cell(row,col).alignment=Alignment(horizontal='center',vertical='center',wrap_text=True); ws.cell(row,col).font=Font(name='Courier New',size=10 if row==4 else 9,bold=True)
 ws.row_dimensions[4].height=23.25; ws.row_dimensions[5].height=18; ws.row_dimensions[6].height=18
 for i,row in enumerate(rows[:64],7):
  vals=[row.get('sr',''),row.get('specification',''),row.get('designation',''),' ',row.get('eqpt_qty') if row.get('eqpt_qty') is not None else '',row.get('total') if row.get('total') is not None else '','','']
  for col,val in enumerate(vals,1):ws.cell(i,col,val).border=border; ws.cell(i,col).font=Font(name='Courier New',size=7); ws.cell(i,col).alignment=Alignment(horizontal='center' if col!=2 else 'left',vertical='center',wrap_text=True if col==2 else False)
  ws.row_dimensions[i].height=max(15.6,min(180,15.6*max(1,len(row.get('specification_lines',[])))))
 for i in range(7+len(rows[:64]),70):
  for col in range(1,9):ws.cell(i,col).border=border; ws.cell(i,col).font=Font(name='Courier New',size=7)
  ws.row_dimensions[i].height=15.6
 fr=72; footer_left=[('Item No.','100'),('Client :',h.get('client','')),('Sales Ref No.:',h.get('sales_ref','')),('DATE :',datetime.now().strftime('%d.%m.%Y'))]; footer_mid=[('Description :',payload.get('description','')),('W.O. No.:',h.get('wo','')),('Drg. No.:',h.get('drawing',''))]; footer_right=[('PRE.BY :',h.get('prep_by','')),('Qty.:',payload.get('qty','')),('ESD No.:',h.get('esd','')),('','1 of 1')]
 for i,(label,value) in enumerate(footer_left):
  ws.cell(fr+i,1,label).font=Font(name='Arial',size=7); ws.cell(fr+i,2,value).font=Font(name='Arial',size=7)
  ws.cell(fr+i,1).alignment=Alignment(horizontal='left',vertical='center'); ws.cell(fr+i,2).alignment=Alignment(horizontal='left',vertical='center')
 for i,(label,value) in enumerate(footer_mid):
  ws.cell(fr+i,4,label).font=Font(name='Arial',size=7); ws.cell(fr+i,5,value).font=Font(name='Arial',size=7)
  ws.cell(fr+i,4).alignment=Alignment(horizontal='left',vertical='center'); ws.cell(fr+i,5).alignment=Alignment(horizontal='left',vertical='center')
 for i,(label,value) in enumerate(footer_right):
  ws.cell(fr+i,7,label).font=Font(name='Arial',size=7); ws.cell(fr+i,8,value).font=Font(name='Arial',size=7)
  ws.cell(fr+i,7).alignment=Alignment(horizontal='right',vertical='center'); ws.cell(fr+i,8).alignment=Alignment(horizontal='right',vertical='center')
 ws.print_area='A1:H76'; return wb
@app.post('/api/bom/export')
async def export(payload:dict, user:str=Depends(current_user)):
 wb=export_book(payload);out=io.BytesIO();wb.save(out);out.seek(0);return StreamingResponse(out,media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',headers={'Content-Disposition':'attachment; filename=ELEXORA_BOM.xlsx'})

class NumberedCanvas:
    def __init__(self, *args, **kwargs):
        from reportlab.pdfgen import canvas as pdfcanvas
        self._canvas = pdfcanvas.Canvas(*args, **kwargs)
        self._saved_page_states = []

    def __getattr__(self, name):
        return getattr(self._canvas, name)

    def showPage(self):
        self._saved_page_states.append(dict(self._canvas.__dict__))
        self._canvas._startPage()

    def save(self):
        total = len(self._saved_page_states)
        for state in self._saved_page_states:
            self._canvas.__dict__.update(state)
            self._draw_page_number(total)
            self._canvas.showPage()
        self._canvas.save()

    def _draw_page_number(self, total):
        # Page number occupies its own fourth line in the right footer block.
        w, hp = A4
        self._canvas.setFont('Helvetica', 6.5)
        self._canvas.drawRightString(
            w - 8*mm, 4*mm,
            f'{self._canvas.getPageNumber()} of {total}'
        )


def export_pdf(payload):
    if payload.get('feeders'):
        return export_combined_pdf(payload)
    out = io.BytesIO()
    doc = SimpleDocTemplate(
        out, pagesize=A4,
        rightMargin=8*mm, leftMargin=8*mm,
        topMargin=12*mm, bottomMargin=28*mm
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('bomtitle', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=9, leading=11, alignment=TA_CENTER)
    meta_style = ParagraphStyle('bommeta', parent=styles['Normal'], fontName='Helvetica', fontSize=6.5, leading=8, alignment=TA_RIGHT)
    cell_style = ParagraphStyle('cell', parent=styles['Normal'], fontName='Helvetica', fontSize=6.3, leading=7.5, spaceAfter=0, spaceBefore=0)
    cell_center = ParagraphStyle('cellcenter', parent=cell_style, alignment=TA_CENTER)
    head_style = ParagraphStyle('head', parent=cell_style, fontName='Helvetica-Bold', alignment=TA_CENTER, leading=8)
    h = payload.get('header', {})
    rows = payload.get('rows', [])
    story = [
        Paragraph('-: Equipment List :-', title_style),
        Spacer(1, 4*mm),
    ]
    data = [
        [Paragraph('EQPT.NO',head_style), Paragraph('SPECIFICATION',head_style), Paragraph('DESIGNATION',head_style),
         Paragraph('FEEDER TYPICAL',head_style), '', Paragraph('TOTAL<br/>EQPT<br/>QTY.',head_style), Paragraph('MPD',head_style), Paragraph('AMD',head_style)],
        ['', '', '', Paragraph('QTY.',head_style), Paragraph(str(payload.get('feeder_name','')),head_style), '', '', ''],
        ['', '', '', '', Paragraph(str(payload.get('feeder_qty','')),cell_center), '', '', '']
    ]
    for row in rows[:64]:
        spec = row.get('specification','') or ''
        data.append([
            Paragraph(str(row.get('sr','') or ''), cell_center),
            Paragraph(str(spec).replace('&','&amp;').replace('<','&lt;').replace('>','&gt;').replace('\n','<br/>'), cell_style),
            Paragraph(str(row.get('designation','') or ''), cell_center),
            '',
            Paragraph(str(row.get('eqpt_qty','') or ''), cell_center),
            Paragraph(str(row.get('total','') or ''), cell_center),
            '', ''
        ])
    col_widths=[16*mm, 81*mm, 24*mm, 17*mm, 20*mm, 18*mm, 9*mm, 9*mm]
    tbl=Table(data,colWidths=col_widths,repeatRows=3)
    tbl.setStyle(TableStyle([
        ('SPAN',(0,0),(0,2)),('SPAN',(1,0),(1,2)),('SPAN',(2,0),(2,2)),
        ('SPAN',(3,0),(4,0)),('SPAN',(5,0),(5,2)),('SPAN',(6,0),(6,2)),('SPAN',(7,0),(7,2)),
        ('GRID',(0,0),(-1,-1),0.45,colors.black),
        ('BACKGROUND',(0,0),(-1,2),colors.HexColor('#d9e7f0')),
        ('VALIGN',(0,0),(-1,-1),'MIDDLE'),
        ('ALIGN',(0,0),(-1,2),'CENTER'),
        ('LEFTPADDING',(0,0),(-1,-1),2),('RIGHTPADDING',(0,0),(-1,-1),2),
        ('TOPPADDING',(0,0),(-1,-1),2),('BOTTOMPADDING',(0,0),(-1,-1),2),
    ]))
    story.append(tbl)

    def footer(canvas, doc_obj):
        canvas.saveState()
        w, hp = A4
        y = 11*mm
        canvas.setFont('Helvetica', 6.5)

        # Fixed footer zones matching the supplied BOM: left / center / right.
        left_x = 8*mm
        mid_x = 82*mm
        right_x = 164*mm
        line_step = 3*mm

        left=[
            ('Item No.','100'),
            ('Client :',h.get('client','')),
            ('Sales Ref No.:',h.get('sales_ref','')),
            ('DATE :',datetime.now().strftime('%d.%m.%Y'))
        ]
        mid=[
            ('Description :',payload.get('description','')),
            ('W.O. No.:',h.get('wo','')),
            ('Drg. No.:',h.get('drawing',''))
        ]
        right=[
            ('PRE.BY :',h.get('prep_by','')),
            ('Qty.:',payload.get('qty','')),
            ('ESD No.:',h.get('esd',''))
        ]

        for i,(label,value) in enumerate(left):
            canvas.drawString(left_x, y+(2.5-i)*line_step, (label+' '+str(value)).strip())

        # Keep the center footer inside its own zone so it cannot overlap the right block.
        from reportlab.pdfbase.pdfmetrics import stringWidth
        mid_max_x = right_x - 5*mm
        for i,(label,value) in enumerate(mid):
            text_value = (label+' '+str(value)).strip()
            max_width = mid_max_x - mid_x
            while stringWidth(text_value, 'Helvetica', 6.5) > max_width and len(text_value) > len(label) + 4:
                text_value = text_value[:-2].rstrip() + '...'
            canvas.drawString(mid_x, y+(2.5-i)*line_step, text_value)

        for i,(label,value) in enumerate(right):
            canvas.drawString(right_x, y+(2.5-i)*line_step, (label+' '+str(value)).strip())

        canvas.restoreState()
    doc.build(story,onFirstPage=footer,onLaterPages=footer,canvasmaker=NumberedCanvas)
    out.seek(0)
    return out

@app.post('/api/bom/export-pdf')
async def export_pdf_endpoint(payload:dict, user:str=Depends(current_user)):
    out=export_pdf(payload)
    return StreamingResponse(out,media_type='application/pdf',headers={'Content-Disposition':f'attachment; filename=BOM_{re.sub(r"[^A-Za-z0-9._-]+", "_", str(payload.get("header", {}).get("wo", "WO"))).strip("_")}.pdf'})
def transform_word(page,word):
 pts=[fitz.Point(word[0],word[1])*page.rotation_matrix,fitz.Point(word[2],word[3])*page.rotation_matrix]
 return {'x':(pts[0].x+pts[1].x)/2,'y':(pts[0].y+pts[1].y)/2,'text':word[4]}
def page_words(page): return [transform_word(page,w) for w in page.get_text('words') if w[4].strip()]
def clean(s): return re.sub(r'\s+',' ',s).strip(' -')
def words_text(words): return ' '.join(w['text'] for w in sorted(words,key=lambda z:(round(z['y'],1),z['x'])))
def text_in_band(words,xmin,xmax,ymin,ymax): return clean(words_text([w for w in words if xmin<=w['x']<xmax and ymin<=w['y']<ymax]))
def rows_by_designation(words,xmin,xmax,ymin,ymax):
 vals=[]
 for w in words:
  if xmin<=w['x']<=xmax and ymin<=w['y']<=ymax:
   t=clean(w['text'])
   if t.upper() in {'DESIG.','QTY.','NO.'}: continue
   if re.fullmatch(r'[A-Z0-9]+(?:[-/,+][A-Z0-9]+)*',t,re.I) and (re.search(r'\d',t) or re.fullmatch(r'PS(?:\+S28)?',t,re.I)): vals.append((w['y'],t))
 vals.sort(); out=[]
 for y,t in vals:
  if not out or abs(y-out[-1][0])>5: out.append([y,t])
 return out
def designation_qty(des):
 total=0
 for part in re.split(r'[,/]',(des or '').upper()):
  part=part.strip()
  # Supports both letter-prefixed (F1-F3) and digit+letter (1H1-1H3)
  # designations used by the fixed MSLD template.
  m=re.fullmatch(r'(?:[A-Z]+|\d+[A-Z]+)(\d+)-(?:[A-Z]+|\d+[A-Z]+)?(\d+)',part)
  if m: total+=max(1,int(m.group(2))-int(m.group(1))+1)
  elif re.fullmatch(r'(?:[A-Z]+|\d+[A-Z]+)\d+',part): total+=1
 if total==0 and des:
  m=re.fullmatch(r'(?:[A-Z]+|\d+[A-Z]+)(\d+)-(?:[A-Z]+|\d+[A-Z]+)(\d+)',des.upper())
  if m: total=max(1,int(m.group(2))-int(m.group(1))+1)
 return total or None
def space_heater_validation(record):
    """Check E11/E12 space-heater wattage and supply against the approved BOM rating."""
    source = clean_value(' '.join(
        str(record.get(key) or '')
        for key in ('designation', 'description', 'details')
    )).upper()
    warnings = []
    wattage = re.search(r'(?<![A-Z0-9])(\d+(?:\.\d+)?)\s*(?:WATTS?|W)\b', source)
    if not wattage:
        warnings.append('Space-heater wattage is missing or unreadable in the MSLD; expected 200 Watts. Please verify.')
    elif float(wattage.group(1)) != 200:
        warnings.append('Space-heater wattage mismatch: MSLD specifies ' + wattage.group(1) + ' Watts; expected 200 Watts. Please verify.')

    voltage = re.search(r'(?<![A-Z0-9])(\d+(?:\.\d+)?)\s*V(?:OLTS?)?\s*(AC|DC)\b', source)
    if not voltage:
        warnings.append('Space-heater supply voltage is missing or unreadable in the MSLD; expected 240V AC. Please verify.')
    elif float(voltage.group(1)) != 240 or voltage.group(2) != 'AC':
        warnings.append('Space-heater supply mismatch: MSLD specifies ' + voltage.group(1) + 'V ' + voltage.group(2) + '; expected 240V AC. Please verify.')
    return warnings


def emergency_push_button_contact_config(record):
    """Read contact arrangement only from the S0 emergency push-button row."""
    designation = clean_value(str(record.get('designation') or '')).upper()
    if designation != 'S0':
        return {'label': None, 'type': None, 'confirmed': False}

    # Keep word boundaries from the extracted cell. Removing all whitespace
    # first can concatenate "1NO+1NC" with following text such as "EM. TRIP",
    # causing a valid combined contact value to fail its end-boundary check.
    details_text = clean_value(str(record.get('details') or '')).upper()
    patterns = [
        (r'(?<![A-Z0-9])1NO\s*(?:\+|&|/|AND)\s*1NC', '1NO+1NC', '3SB54-00-0A'),
        (r'(?<![A-Z0-9])1NC\s*(?:\+|&|/|AND)\s*1NO', '1NO+1NC', '3SB54-00-0A'),
        (r'(?<![A-Z0-9])2NO(?![A-Z0-9])', '2NO', '3SB54-00-0E'),
        (r'(?<![A-Z0-9])2NC(?![A-Z0-9])', '2NC', '3SB54-00-0F'),
        (r'(?<![A-Z0-9])1NO(?![A-Z0-9+&/])', '1NO', '3SB54-00-0B'),
        (r'(?<![A-Z0-9])1NC(?![A-Z0-9+&/])', '1NC', '3SB54-00-0C'),
    ]
    for pattern, label, suffix in patterns:
        if re.search(pattern, details_text):
            return {'label': label, 'type': suffix, 'confirmed': True}
    return {'label': None, 'type': None, 'confirmed': False}


def limit_switch_contact_configuration(record):
    """Extract NO/NC auxiliary-contact arrangement from this limit-switch MSLD row."""
    source = clean_value(' '.join([
        str(record.get('description') or ''),
        str(record.get('details') or ''),
    ])).upper()

    combined = re.search(
        r'(?<![A-Z0-9])(\d+)\s*NO\s*(?:\+|&|/|AND)\s*(\d+)\s*NC(?![A-Z0-9])',
        source
    )
    if combined:
        return f'{int(combined.group(1))}NO+{int(combined.group(2))}NC'

    reverse = re.search(
        r'(?<![A-Z0-9])(\d+)\s*NC\s*(?:\+|&|/|AND)\s*(\d+)\s*NO(?![A-Z0-9])',
        source
    )
    if reverse:
        return f'{int(reverse.group(2))}NO+{int(reverse.group(1))}NC'

    # Also accept single-type arrangements such as 2NO or 2NC.
    single = re.search(r'(?<![A-Z0-9])(\d+)\s*(NO|NC)(?![A-Z0-9])', source)
    if single:
        return f'{int(single.group(1))}{single.group(2)}'
    return '[CONTACT CONFIGURATION NOT SPECIFIED]'


def master_match(description,details,designation):
 # Active final classifier: designation may contain the equipment type.
 s=(str(description or '')+' '+str(details or '')+' '+str(designation or '')).upper()
 des=re.sub(r'\s+','',str(designation or '')).upper()
 if is_test_service_limit_switch(description, details, designation):return 'S16_AUX_SWITCH'
 if des == 'S16':return 'S16_AUX_SWITCH'
 if re.search(r'\bLIMIT\s+SWITCH\b', s):return 'LIMIT_SWITCH'
 # F21 is the MCB feeding the space heater; do not classify it as the heater
 # merely because its row text mentions the space-heater circuit/load.
 if des == 'F21':return 'MCB'
 if des in ('E11','E12') or 'SPACE HEATER' in s:return 'SPACE_HEATER'
 if (('EMERGENCY' in s and 'PUSH BUTTON' in s) or 'EMERGENCY TRIP PB' in s or ('PUSH BUTTON' in s and ('EM. TRIP' in s or 'EM TRIP' in s or 'EMERGENCY TRIP' in s))):return 'EMERGENCY_PUSH_BUTTON'
 if des == 'E2':return 'E2_LED'
 if des == 'S14' or 'TNC SWITCH' in s or 'BREAKER CONTROL SWITCH' in s:return 'TNC_SWITCH'
 if des == 'S13' or 'ROTARY SWITCH' in s or 'LOCAL REMOTE' in s:return 'ROTARY_SWITCH'
 if 'LED LAMP' in s or re.search(r'\bLED\b',s):return 'LED'
 if 'CURRENT TRANSFORMER' in s:return 'CT'
 if 'DAMPING RESISTOR' in s:return 'PT_DAMPING_RESISTOR'
 if 'POTENTIAL TRANSFORMER' in s:return 'PT'
 if re.search(r'\b7SJ66\d{2}\b', s) or 'NUM. PROT. RELAY' in s or 'NUMERICAL PROTECTION RELAY' in s:return '7SJ6611'
 if 'VAJH13' in s or 'MASTER TRIP RELAY' in s:return 'VAJH13'
 if 'DIGITAL MF METER' in s or 'EM6400NG' in s:return 'EM6400NG'
 if 'DIGITAL VOLTMETER' in s:return 'VOLTMETER'
 if 'DIGITAL AMMETER' in s:return 'AMMETER'
 if '3NW32NNSL' in re.sub(r'[^A-Z0-9]','',s) or 'CONTROL LINK' in s:return 'CONTROL_LINK'
 if (des in ('PS','PS+S28') and 'PLUG SOCKET' in s) or 'PLUG SOCKET WITH BUILT-IN SWITCH' in s or ('PLUG SOCKET' in s and re.search(r'\b(?:1P|1 POLE|SINGLE POLE|SP)\b', s) and re.search(r'\bMCB\b|MINIATURE CIRCUIT BREAKER', s)):return 'PLUG_SOCKET_SWITCH'
 if 'MINIATURE CIRCUIT BREAKER' in s or re.search(r'\bMCB\b',s) or '5SL4206-7RC' in s:return 'MCB'
 return designation or 'UNMATCHED'

def extract_fixed_table(data,filename):
 pages=pdf_pages(data,filename); records=[]; feeders=[]; warnings=[]
 for page_no,page in enumerate(pages,1):
  if page_no!=1:continue
  words=page_words(page); feeder_type=text_in_band(words,440,520,410,430); feeder_designation=text_in_band(words,440,520,395,415); feeder_rating=text_in_band(words,440,520,430,450); wiring=text_in_band(words,440,520,450,465); feeder_quantity=text_in_band(words,440,520,360,380)
  if feeder_type or feeder_designation:feeders.append({'name':feeder_designation or feeder_type,'type':feeder_type,'designation':feeder_designation,'rating':feeder_rating,'wiring':wiring,'quantity':feeder_quantity or '1'})
  # Q10 VCB is above the normal equipment rows in the rotated MSLD.
  q10_designation=text_in_band(words,275,320,460,480)
  if re.search(r'\bQ10\b',q10_designation,re.I):
   q10_desc=text_in_band(words,50,275,460,480)
   q10_details=text_in_band(words,320,615,465,515)
   records.append({'source_page':page_no,'description':q10_desc,'designation':'Q10','details':q10_details,'master_code':'VCB','quantity':1,'section':'MSLD equipment'})
  left=rows_by_designation(words,275,320,450,790)
  for i,(y,des) in enumerate(left):
   next_y=left[i+1][0] if i+1<len(left) else 790
   # T1-T3 is a two-line CT block in the fixed MSLD template. Capture the
   # complete technical-data block rather than only one visual row.
   row_top = y-18 if des.upper() == 'T1-T3' else y-9
   lo,hi=max(490,row_top),min(790,(y+next_y)/2)
   desc=text_in_band(words,50,275,lo,hi); details=text_in_band(words,320,615,lo,hi)
   if desc or details:records.append({'source_page':page_no,'description':desc,'designation':des,'details':details,'master_code':master_match(desc,details,des),'quantity':designation_qty(des),'section':'MSLD equipment'})
  # Include bottom equipment rows (E11/E12 and X42) before the drawing footer.
  for ymin,ymax in [(35,345),(350,765)]:
   right=rows_by_designation(words,840,880,ymin,ymax)
   for i,(y,des) in enumerate(right):
    next_y=right[i+1][0] if i+1<len(right) else ymax; lo,hi=max(ymin,y-8),min(ymax,(y+next_y)/2); desc=text_in_band(words,630,835,lo,hi); details=text_in_band(words,890,1175,lo,hi)
    if desc or details or des.upper() in ('PS','PS+S28'):records.append({'source_page':page_no,'description':desc,'designation':des,'details':details,'master_code':master_match(desc,details,des),'quantity':designation_qty(des) or 1,'section':'MSLD equipment'})
 return feeders,records,warnings

def extract_header_from_page(data,filename):
 pages=pdf_pages(data,filename)
 if not pages:return {}
 words=page_words(pages[0]); band=lambda a,b,c,d:text_in_band(words,a,b,c,d); drawing_band=band(980,1110,805,830); drawing=first(r'(G\d{4,}-[A-Z0-9]+-[A-Z0-9]+)',drawing_band,drawing_band); esd=first(r'\b(SED\d+)\b',drawing_band) or first(r'\b(SED\d+)\b',words_text(words))
 return {'client':band(345,440,782,792),'sales_ref':band(910,960,782,793),'drawing':drawing,'esd':esd,'wo':band(905,960,793,805),'description':band(690,835,775,795),'qty':band(1000,1040,790,805),'master_singleline':band(690,835,800,815)}
def header(msld,dis,client,sales,drawing,esd,wo,prep,voltage):
 s=msld+'\n'+dis
 return {'client':client.strip() or first(r'Client\s*:?\s*([^\n]+)',s),'sales_ref':sales.strip() or first(r'Sales\s*Ref\.?\s*:?\s*([^\n]+)',s),'drawing':drawing.strip() or first(r'Drg\.?\s*No\.?\s*:?\s*([^\n]+)',s),'esd':esd.strip() or first(r'\b(SED\d+)\b',s) or first(r'ESD\s*No\.?\s*:?\s*([^\n]+)',s),'wo':wo.strip() or first(r'W\.?O\.?\s*No\.?\s*:?\s*([^\n]+)',s),'prep_by':prep.strip(),'voltage':norm_voltage(voltage)}

def clean_value(value):
    if not value:
        return ''
    value = value.replace('\n', ' ')
    value = re.sub(r'\s+', ' ', value)
    return value.strip()

def first_match(pattern, text, flags=re.IGNORECASE):
    if not text:
        return ''
    m = re.search(pattern, text, flags)
    return clean_value(m.group(1)) if m else ''

def extract_ct_ratio(ct_text):
    m = re.search(r'\bCTR\s*:\s*([0-9]+(?:\.[0-9]+)?(?:-[0-9]+(?:\.[0-9]+)?)?)\s*/', ct_text or '', re.I)
    return m.group(1) if m else ''

def extract_ct_secondary_current(ct_text):
    m = re.search(r'\bCTR\s*:\s*[0-9]+(?:\.[0-9]+)?(?:-[0-9]+(?:\.[0-9]+)?)?\s*/\s*([0-9]+(?:\.[0-9]+)?)\s*A', ct_text or '', re.I)
    return f'{m.group(1)}A' if m else ''

def extract_ct_core_lines(ct_text):
    labeled = re.findall(r'CORE\s*(\d+)\s*:\s*(.*?)(?=,\s*CORE\s*\d+\s*:|$)', ct_text or '', re.I)
    if labeled:
        return [clean_value(v) for _, v in sorted(labeled, key=lambda x:int(x[0]))]
    values=[]
    # The fixed MSLD extraction normalizes line breaks, so multiple CTR
    # entries can arrive on the same line. Stop each core at the next CTR
    # (or STC) instead of consuming the whole technical-data string.
    for m in re.finditer(r'\bCTR\s*:\s*(.*?)(?=\s+CTR\s*:|\s+STC\s*:|\s+SHORT\s+TIME\s+CURRENT\s*:|$)', ct_text or '', re.I):
        value=clean_value(m.group(1))
        if value: values.append(value)
    return values

def extract_ct_core_count(ct_text):
    explicit=first_match(r'NO\.?\s*OF\s*CORES\s*[:\-]?\s*(\d+)',ct_text)
    if explicit: return int(explicit)
    return len(extract_ct_core_lines(ct_text)) or 1

def generate_ct_code(ct_text):
    ratio=extract_ct_ratio(ct_text)
    secondary=extract_ct_secondary_current(ct_text)
    count=extract_ct_core_count(ct_text)
    return f'CT{ratio}{count}C-{secondary}' if ratio and secondary else ''

def extract_ct_type(ct_text):
    for pattern in [r'(EPOXY\s+CAST\s+RESIN\s*\(\s*WOUND\s+TYPE\s*\))',r'(WOUND\s+TYPE)',r'(WINDOW\s+TYPE)']:
        value=first_match(pattern,ct_text)
        if value:return value.upper()
    return ''

def build_ct_description(ct_text):
    t=extract_ct_type(ct_text)
    # Fixed CT wording required by the BOM template.
    return f'CURRENT TRANSFORMER EPOXY CAST RESIN ({t})' if t else 'CURRENT TRANSFORMER EPOXY CAST RESIN (WOUND TYPE)'

def build_ctr_line(ct_text):
    n=extract_ct_core_count(ct_text)
    word={1:'SINGLE',2:'TWO',3:'THREE'}.get(n,str(n))
    return f'{word} CORE CT'

def dis_search_text(dis_text):
    return re.sub(r'\s+', ' ', dis_text or '').strip()

def dis_field(dis_text, code, label=''):
    # The DIS is a fixed template. Use the numbered field as the primary
    # anchor because PDF text extraction can change spacing or wording.
    text = dis_search_text(dis_text)
    pattern = rf'{re.escape(code)}\s*(?:{label}\s*)?:?\s*(.*?)(?=\s+\d{{1,2}}\.\d{{2}}\.\d{{2}}\s+|$)'
    m = re.search(pattern, text, re.IGNORECASE)
    return clean_value(m.group(1)) if m else ''

def extract_annexure_make(dis_text, equipment):
    """Return the common CT/PT make list from DIS Annexure 1."""
    text = dis_search_text(dis_text)
    section_match = re.search(
        r'ANNEXURE\s*[-:]?\s*1\b(.*?)(?=ANNEXURE\s*[-:]?\s*[2-9]\b|$)',
        text, re.IGNORECASE | re.DOTALL
    )
    section = section_match.group(1) if section_match else text
    # The approved description lists all three makes together for both CT and PT.
    required = ['ECS', 'NPT', 'PRAGATI']
    present = [name for name in required if re.search(rf'\b{re.escape(name)}\b', section, re.IGNORECASE)]
    if len(present) == 3:
        return 'ECS/NPT/PRAGATI'
    # Annexure text extraction may lose part of a table. Keep the requested
    # standard list consistent rather than outputting a misleading single make.
    return 'ECS/NPT/PRAGATI'



def extract_ct_make(dis_text):
    text = dis_search_text(dis_text)
    # Fixed DIS make may appear as "PRAGATI MAKE", "PRAGATI/ECS MAKE",
    # "CT MAKE: PRAGATI", or "MAKE OF CT: PRAGATI". Do not depend on one
    # exact PDF text layout.
    patterns = [
        r'\b(PRAGATI(?:\s*/\s*[A-Z][A-Z0-9.&\-]*)+)\s+MAKE\b',
        r'\b(PRAGATI(?:\s*/\s*[A-Z][A-Z0-9.&\-]*)+)\b',
        r'\b(PRAGATI)\s+MAKE\b',
        r'(?:CT\s*/?\s*MAKE|CT/PT\s+MAKE|MAKE\s+OF\s+CT)\s*[:\-]?\s*([A-Za-z][A-Za-z0-9.&/\-]*(?:\s*/\s*[A-Za-z][A-Za-z0-9.&/\-]*)*)',
        r'\bMAKE\s*[:\-]\s*([A-Za-z][A-Za-z0-9.&/\-]*(?:\s*/\s*[A-Za-z][A-Za-z0-9.&/\-]*)*)',
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            value = re.sub(r'\s*/\s*', '/', clean_value(m.group(1)))
            if value and value.upper() not in {'OF','CT','PT','MAKE'}:
                return value
    return ''

def extract_rated_voltage(dis_text):
    value = dis_field(dis_text, '1.03.00', r'(?:Rated\s+(?:operational\s+)?voltage)')
    m = re.search(r'([0-9]+(?:\.[0-9]+)?)\s*(KV|kV)', value or '', re.IGNORECASE)
    if m:
        return f'{m.group(1)}kV'
    text = dis_search_text(dis_text)
    m = re.search(r'Rated\s+(?:operational\s+)?voltage\s*:?\s*([0-9]+(?:\.[0-9]+)?)\s*(KV|kV)', text, re.IGNORECASE)
    return f'{m.group(1)}kV' if m else ''

def extract_frequency(dis_text):
    value = dis_field(dis_text, '1.02.00', r'Main\s+System')
    text = value or dis_search_text(dis_text)
    m = re.search(r'([0-9]+(?:\.[0-9]+)?)\s*Hz', text, re.IGNORECASE)
    return f'{m.group(1)}Hz' if m else ''

def extract_bil(dis_text):
    values = []
    for code in ('1.04.00','1.06.00','1.07.00'):
        value = dis_field(dis_text, code)
        m = re.search(r'([0-9]+(?:\.[0-9]+)?)', value)
        if not m:
            return ''
        values.append(m.group(1))
    return '/'.join(values) + 'KVp'

def extract_panel_suitability(dis_text):
    # Source is DIS 2.01.00 "Type of switchboard", not 2.02.01 Location.
    # Example fixed-template value:
    # 8BK80(1000mm)+3AH3 VCB
    value = dis_field(dis_text, '2.01.00', r'Type\s+of\s+switchboard')
    if value:
        m = re.search(r'\b(8BK80)\s*\(?\s*(\d{3,4})\s*mm\s*\)?', value, re.I)
        if m:
            return f'{m.group(1).upper()}-{m.group(2)}mm WIDTH PANEL'
    # Fallback: search the whole DIS only if the numbered field was not
    # extracted cleanly.
    text = dis_search_text(dis_text)
    m = re.search(r'\b(8BK80)\s*\(?\s*(\d{3,4})\s*mm\s*\)?', text, re.I)
    if m:
        return f'{m.group(1).upper()}-{m.group(2)}mm WIDTH PANEL'
    return ''

def extract_pt_section_text(msld_text):
    """Return only the PT equipment-list row, excluding the adjacent CT and fuse rows."""
    text = clean_value(msld_text)
    if not text:
        return ''

    start = re.search(r'\bPOTENTIAL\s+TRANSFORMERS?\b', text, re.IGNORECASE)
    if not start:
        return ''

    section = text[start.start():]
    # In this MSLD layout the PT core table ends before the built-in primary
    # fuse row. Other common equipment labels are additional safe boundaries.
    end = re.search(
        r'\b(?:WITH\s+BUILT[-\s]?IN\s+PRIMARY\s+FUSES?|'
        r'DAMPING\s+RESISTOR|REAR\s+COVER\s+SOLENOID|'
        r'REAR\s+DOOR\s+LIMIT\s+SWITCH|DIGITAL\s+AMMETER|'
        r'DIGITAL\s+VOLTMETER|CURRENT\s+TRANSDUCER|'
        r'DIGITAL\s+MF\s+METER|NUM\.?\s*PROT\.?\s*RELAY|'
        r'EQPT\.?\s*NO\.?)\b',
        section,
        re.IGNORECASE
    )
    return section[:end.start()] if end else section


def extract_pt_core_lines(pt_text):
    """Extract burden/class only from explicitly numbered cores in the PT equipment row."""
    text = clean_value(pt_text)
    if not text:
        return []

    # Do not scan the complete MSLD here: CT rows can also contain VA/class data.
    # Each core is bounded by its own CORE n label and the next core label.
    sections = list(re.finditer(
        r'\bCORE\s*(\d+)\s*:\s*(.*?)(?=\bCORE\s*\d+\s*:|$)',
        text,
        re.IGNORECASE
    ))
    extracted = {}
    for match in sections:
        core_number = int(match.group(1))
        section = match.group(2)

        burden = first_match(r'\b(\d+(?:\.\d+)?)\s*VA\b', section)
        accuracy = re.search(
            r'\b(?:CL\.?|CLASS)\s*[-:=]?\s*([A-Z0-9]+(?:\.[A-Z0-9]+)?)(\s*\(\s*OD\s*\))?',
            section,
            re.IGNORECASE
        )
        # A burden without a readable accuracy class is incomplete; never
        # substitute a neighbouring CT burden or emit a partial "CLASS" value.
        if not burden or not accuracy:
            continue

        class_value = accuracy.group(1).upper()
        suffix = ' (OD)' if accuracy.group(2) else ''
        extracted[core_number] = f'{burden}VA, CL-{class_value}{suffix}'

    return [extracted[number] for number in sorted(extracted)]


def extract_pt_fuse_rating(msld_text, dis_text):
    text = clean_value((msld_text or '') + ' ' + (dis_text or ''))
    patterns = [
        r'(?:IN[- ]?BUILT\s+)?PRIMARY\s+FUSE(?:\s+OF|\s+RATING)?\s*[:=]?\s*(\d+(?:\.\d+)?)\s*A\b',
        r'FUSE\s+(?:RATING|CURRENT)\s*[:=]?\s*(\d+(?:\.\d+)?)\s*A\b',
    ]
    for pattern in patterns:
        value = first_match(pattern, text)
        if value:
            return f'{value}A'
    return ''


def extract_pt_panel_suitability(msld_text, dis_text=''):
    """Use the exact same panel extraction source/rule as CT: DIS 2.01.00."""
    return extract_panel_suitability(dis_text)



def pt_spec(pt_text, msld_text, dis_text):
    """Format PT description from MSLD/DIS using the approved fixed layout."""
    source = clean_value((pt_text or '') + ' ' + (msld_text or ''))
    rated_voltage = extract_rated_voltage(dis_text)
    if not rated_voltage:
        rated_voltage = first_match(r'\b([0-9]+(?:\.[0-9]+)?\s*kV)\b', source)
        rated_voltage = re.sub(r'\s+', '', rated_voltage)
    # PT make is sourced from DIS.
    make = extract_annexure_make(dis_text, 'PT') or extract_ct_make(dis_text)
    if not make:
        dis_source = dis_search_text(dis_text)
        make = first_match(r'(?:Manufacturer(?:\s+name)?|Make\s+of\s+(?:PT|potential\s+transformer))\s*[:=\-]?\s*([A-Z][A-Z0-9.&/\-]*(?:\s*/\s*[A-Z][A-Z0-9.&/\-]*)?)', dis_source)
        if not make:
            make = first_match(r'\b([A-Z][A-Z0-9.&/\-]*(?:\s*/\s*[A-Z][A-Z0-9.&/\-]*)?)\s+MAKE\b', dis_source)
        make = re.sub(r'\s*/\s*', '/', make)
    # Preserve transformer type from MSLD when it is present.
    transformer_type = first_match(
        r'(POTENTIAL\s+TRANSFORMERS?\s+IN\s+.+?(?:DRAWOUT\s+TYPE|TYPE))',
        source
    )
    if not transformer_type:
        transformer_type = 'POTENTIAL TRANSFORMERS IN CAST RESIN EPOXY CASE (DRAWOUT TYPE)'
    transformer_type = clean_value(transformer_type).upper()
    pt_section_text = extract_pt_section_text(msld_text)
    cores = extract_pt_core_lines(pt_section_text or pt_text)
    core_count = len(cores) or 3
    core_word = {1: 'SINGLE', 2: 'TWO', 3: 'THREE'}.get(core_count, str(core_count))
    frequency = extract_frequency(dis_text)
    bil = extract_bil(dis_text)
    if bil:
        bil = bil.upper()
    panel = extract_pt_panel_suitability(msld_text, dis_text)
    lines = [
        f'PT {rated_voltage}' if rated_voltage else 'PT',
        f'{make.upper()} MAKE' if make else 'MAKE',
        transformer_type,
        '- SINGLE PHASE WITH IN BUILT PRIMARY FUSE OF 3.15A',
        f'{core_word} CORE PT, AS PER IS/IEC STANDARD',
        'AS GIVEN BELOW:-',
    ]
    for index, value in enumerate(cores, 1):
        lines.append(f'- CORE {index}: {value}')
    lines.extend([
        f'- RATED FREQUENCY: {frequency}' if frequency else '- RATED FREQUENCY:',
        '- VF: 1.2COUNT/1.9 FOR 8 HOURS',
        f'- BIL: {bil}' if bil else '- BIL:',
        'INSULATION CLASS:- B',
        f'SUITABLE FOR {panel}' if panel else 'SUITABLE FOR',
    ])
    return lines

def ct_spec(ct_text, dis_text):
    ct_code=generate_ct_code(ct_text)
    ct_make=extract_annexure_make(dis_text, 'CT') or extract_ct_make(dis_text)
    # Retain the legacy default only if Annexure 1 and the DIS text expose no CT make.
    if not ct_make:
        ct_make = ''
    ct_description=build_ct_description(ct_text)
    rated_voltage=extract_rated_voltage(dis_text)
    frequency=extract_frequency(dis_text)
    stc=first_match(r'(?:SHORT\s+TIME\s+CURRENT|STC)\s*:?\s*(.*?)(?=\s+CTR\s*:|\s+CORE\s*\d+\s*:|\s+STC\s*:|\s*$)',ct_text or '')
    bil=extract_bil(dis_text)
    cores=extract_ct_core_lines(ct_text)
    panel=extract_panel_suitability(dis_text)
    lines=[
        ct_code or 'CT',
        f'{ct_make.upper()} MAKE' if ct_make else '',
        ct_description,
        '- SINGLE PHASE, AS PER IS/IEC STANDARD',
        f'- RATED VOLTAGE: {rated_voltage}' if rated_voltage else '- RATED VOLTAGE:',
        f'- RATED FREQUENCY: {frequency}' if frequency else '- RATED FREQUENCY:',
        f'- SHORT TIME CURRENT: {stc}' if stc else '- SHORT TIME CURRENT:',
        f'- BIL: {bil}' if bil else '- BIL:',
        'INSULATION CLASS:-B',
        'AS GIVEN BELOW:-',
        build_ctr_line(ct_text)
    ]
    for i,value in enumerate(cores,1):
        lines.append(f'- CORE {i}: {value}')
    lines.extend([
        f'SUITABLE FOR {panel}' if panel else 'SUITABLE FOR',
        'CT SECONDARY TERMINAL ON P2 SIDE'
    ])
    return lines


def load_tnc_switch_master():
    """Read the Switron TNC switch reference from the existing master CSV."""
    import csv
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'master_database.csv')
    if not os.path.exists(path):
        return None
    with open(path, newline='', encoding='utf-8-sig') as fh:
        for row in csv.DictReader(fh):
            if (row.get('CATEGORY') or '').strip().upper() == 'TNC SWITCH' and (row.get('MODEL') or '').strip().upper() == 'SBC 25P 484R':
                return {str(k).lower(): (v or '').strip() for k, v in row.items()}
    return None


def tnc_switch_validation(record):
    """Validate S14 contact disposition and lockability against the non-lockable Switron master."""
    source = clean_value(' '.join([
        str(record.get('designation') or ''),
        str(record.get('description') or ''),
        str(record.get('details') or '')
    ])).upper()
    errors = []
    # MSLDs may express the same S14 contact count as either '4 NO' or '4 CONTACTS'.
    # Accept both wordings for CLOSE and TRIP while retaining the existing count check.
    close_match = re.search(r'\b4\s*(?:NO|CONTACTS?)\s*(?:IN|AT|FOR)?\s*CLOSE\b|\bCLOSE\s*[:=-]?\s*4\s*(?:NO|CONTACTS?)\b', source)
    trip_match = re.search(r'\b4\s*(?:NO|CONTACTS?)\s*(?:IN|AT|FOR)?\s*TRIP\b|\bTRIP\s*[:=-]?\s*4\s*(?:NO|CONTACTS?)\b', source)
    if not close_match:
        errors.append('Contact disposition mismatch: expected 4 NO in CLOSE.')
    if not trip_match:
        errors.append('Contact disposition mismatch: expected 4 NO in TRIP.')
    non_lockable = bool(re.search(r'\bNON[ -]?LOCKABLE\b', source))
    lockable = bool(re.search(r'\bLOCKABLE\b', source)) and not non_lockable
    if lockable:
        errors.append('Locking type mismatch: master model SBC 25P 484R is NON-LOCKABLE.')
    elif not non_lockable:
        errors.append('Locking type not specified: master model SBC 25P 484R is NON-LOCKABLE; confirm before issuing the BOM.')
    return {'errors': errors, 'is_valid': not errors}


def load_rotary_switch_master():
    """Load the supplied Switron S13 4-pole lockable rotary-switch master."""
    import csv
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'master_database.csv')
    if not os.path.exists(path):
        return None
    with open(path, newline='', encoding='utf-8-sig') as fh:
        for row in csv.DictReader(fh):
            if (row.get('CATEGORY') or '').strip().upper() == 'ROTARY SWITCH' and (row.get('MODEL') or '').strip().upper() == 'SPC 25P 449 B/L':
                return {str(k).lower(): (v or '').strip() for k, v in row.items()}
    return None


def rotary_switch_validation(record, master_item=None):
    """Validate S13 against the fixed 4P, 25A AC/DC, lockable Switron master."""
    source = clean_value(' '.join([
        str(record.get('designation') or ''),
        str(record.get('description') or ''),
        str(record.get('details') or '')
    ])).upper()
    errors = []

    pole_match = re.search(
        r'\b(2|3|4)\s*(?:P|POLE)S?\b|\bNUMBER OF POLES\s*[:=-]?\s*(2|3|4)\s*POLES?\b',
        source
    )
    source_poles = next((group for group in pole_match.groups() if group), None) if pole_match else None

    # S13 is fixed at 4P. Do not let a missing/changed master row weaken this rule.
    if source_poles is None:
        errors.append('Number of poles not specified in MSLD; required S13 configuration is 4P.')
    elif source_poles != '4':
        errors.append(
            'Pole count mismatch: MSLD specifies ' + source_poles +
            'P; required S13 configuration is 4P (model SPC 25P 449 B/L).'
        )

    master_text = (master_item or {}).get('discription', '').upper()
    master_non_lockable = bool(re.search(r'\bNON[ -]?LOCKABLE\b', master_text))
    master_lockable = bool(re.search(r'\bLOCKABLE\b', master_text)) and not master_non_lockable
    source_non_lockable = bool(re.search(r'\bNON[ -]?LOCKABLE\b', source))
    source_lockable = bool(re.search(r'\bLOCKABLE\b', source)) and not source_non_lockable

    if not master_item:
        errors.append('Switron S13 master specification SPC 25P 449 B/L is unavailable; lockability cannot be verified.')
    elif master_lockable == master_non_lockable:
        errors.append('Switron S13 master specification does not define lockability; verify the master data.')
    elif source_lockable == source_non_lockable:
        expected = 'LOCKABLE' if master_lockable else 'NON-LOCKABLE'
        errors.append('Lockability not specified in MSLD; required model specification is ' + expected + '.')
    elif source_lockable != master_lockable or source_non_lockable != master_non_lockable:
        expected = 'NON-LOCKABLE' if master_non_lockable else 'LOCKABLE'
        actual = 'NON-LOCKABLE' if source_non_lockable else 'LOCKABLE'
        errors.append(
            'Lockability mismatch: MSLD specifies ' + actual +
            '; model SPC 25P 449 B/L requires ' + expected + '.'
        )

    current_match = re.search(
        r'\b(?:CURRENT\s*[:=-]?\s*)?(\d+(?:\.\d+)?)\s*A\b',
        source
    )
    source_current = float(current_match.group(1)) if current_match else None

    # Check the current type independently from the ampere rating. For example,
    # "16A AC/DC" has the correct AC/DC type even though its rating is wrong.
    current_type_match = re.search(
        r'\bAC\s*/\s*DC\b|\bAC\s*(?:&|AND)\s*DC\b|\bACDC\b',
        source
    )
    source_current_type = 'AC/DC' if current_type_match else None

    if source_current is None:
        errors.append('Current rating not specified in MSLD; required rating is 25A AC/DC.')
    elif source_current != 25:
        errors.append(
            'Current rating mismatch: MSLD specifies ' +
            str(source_current).rstrip('0').rstrip('.') +
            'A; required Switron S13 rating is 25A AC/DC.'
        )

    if not current_type_match:
        errors.append('Current type mismatch: MSLD must specify AC/DC for Switron S13.')

    return {
        'errors': errors,
        'is_valid': not errors,
        'source_poles': source_poles,
        'required_poles': '4',
        'source_current': source_current,
        'required_current': 25,
        'source_current_type': source_current_type,
        'required_current_type': 'AC/DC',
        'current_type_verified': bool(current_type_match)
    }


def build_rows(feeder_info,records,msld_text='',dis_text=''):
 feeder_name=feeder_info[0].get('designation') or feeder_info[0].get('name','') if feeder_info else ''; feeder_qty=feeder_info[0].get('quantity','1') if feeder_info else '1'; feeder_match=re.search(r'\d+',str(feeder_qty)); feeder_qty_num=int(feeder_match.group()) if feeder_match else 1; rows=[]
 # A PT with an in-built primary fuse is one BOM row. Put the fuse
 # designation on the matching description line in the designation cell;
 # retain the PT row's existing equipment quantity and total quantity.
 pt_fuse_by_pt_index = {}
 consumed_pt_fuse_indices = set()
 pt_indices = [idx for idx, item in enumerate(records) if item.get('master_code') == 'PT']
 for pt_index in pt_indices:
  for fuse_index, fuse_record in enumerate(records):
   if fuse_index == pt_index or fuse_index in consumed_pt_fuse_indices:
    continue
   fuse_designation = pt_primary_fuse_designation(fuse_record)
   if fuse_designation:
    pt_fuse_by_pt_index[pt_index] = fuse_designation
    consumed_pt_fuse_indices.add(fuse_index)
    break
 for i,r in enumerate(records,1):
  record_index = i - 1
  if record_index in consumed_pt_fuse_indices:
   continue
  relay_analysis = None
  vcb_validation = None
  auxiliary_relay_validation = None
  damping_resistor_validation = None
  if r['master_code']=='S16_AUX_SWITCH':
   spec_lines=[
    'B3NZ331',
    'SIEMENS MAKE',
    'AUX SWITCH FOR 3AH CIRCUIT',
    'BREAKER TEST/SERVICE POSITION',
    '4NO AT EACH TEST & SERVICE POSITION'
   ]
   spec='\n'.join(spec_lines)
   editable_fields=[]
   editable_indices=[]
  elif r['master_code']=='LIMIT_SWITCH':
   contact_configuration = limit_switch_contact_configuration(r)
   spec_lines=[
    'ELMEX MAKE LIMIT SWITCH',
    'IN OPEN EXECUTION WITH PUSH ROD',
    'CONTACT ACTION: SNAP',
    'AUXILIARY CONTACTS: ' + contact_configuration,
   ]
   spec='\n'.join(spec_lines)
   editable_fields=[]
   editable_indices=[]
  elif r['master_code']=='EMERGENCY_PUSH_BUTTON':
   contact_config = emergency_push_button_contact_config(r)
   contact_label = contact_config['label'] or '[CONTACT CONFIGURATION NOT SPECIFIED]'
   contact_type = contact_config['type'] or '[CONTACT TYPE NOT SPECIFIED]'
   spec_lines=[
    '3SB50',
    'SIEMENS MAKE',
    'EMERGENCY TRIP PUSH BUTTON',
    'SICONT PUSH BUTTON ACTUATOR',
    'MUSHROOM WITH P.F.R',
    'WITH HOLDER FOR REAR CONNECTION',
    'COLOUR : RED',
    'WITH ' + contact_label + ' CONTACT BLOCK',
    'TYPE :3SB50-00-0UC01+' + contact_type,
    'WITH PROTECTIVE SHROUDS'
   ]
   spec='\n'.join(spec_lines)
   editable_fields=[]
   editable_indices=[]
  elif r['master_code']=='PLUG_SOCKET_SWITCH':
   spec_lines=[
    'ANCHOR MAKE 6/16A,2PIN+E PLUG SOCKET',
    'SUITABLE FOR 240V AC',
    'WITH BUILT-IN SWITCH',
    'VOLTAGE: 240V AC'
   ]
   spec='\n'.join(spec_lines)
   editable_fields=[]
   editable_indices=[]
  elif r['master_code']=='SPACE_HEATER':
   spec_lines=[
    'APPROVED MAKE',
    'PANEL SPACE HEATER',
    'IN PERFORATED ENCLOSURE',
    'WATTAGE: 200 WATTS',
    'VOLTAGE: 240V AC'
   ]
   spec='\n'.join(spec_lines)
   editable_fields=[]
   editable_indices=[]
  elif r['master_code']=='VCB':
   spec_lines, vcb_validation = vcb_spec(r, dis_text, msld_text)
   spec='\n'.join(spec_lines)
   editable_fields=[]
   editable_indices=[]
  elif r['master_code']=='CT':
   ct_row_text = ' '.join([r.get('designation',''), r.get('description',''), r.get('details','')])
   spec_lines=ct_spec(ct_row_text,dis_text)
   spec='\n'.join(spec_lines)
   editable_fields=['INSULATION CLASS:-B','CT SECONDARY TERMINAL ON P2 SIDE']
   # Core count changes the position of the SUITABLE FOR line. Resolve
   # editable positions from their actual labels so 3-core CTs never mark
   # the SUITABLE FOR row as editable by mistake.
   editable_indices = []
   for editable_label in editable_fields:
    target = editable_label.split(' ON P2 SIDE')[0]
    line_index = next((idx for idx, line in enumerate(spec_lines)
                       if target in line), None)
    if line_index is not None:
     editable_indices.append(line_index)
  elif r['master_code']=='PT':
   pt_row_text = ' '.join([r.get('designation',''), r.get('description',''), r.get('details','')])
   spec_lines=pt_spec(pt_row_text, msld_text, dis_text)
   spec='\n'.join(spec_lines)
   editable_fields=['INSULATION CLASS:- B']
   editable_indices=[12]
  elif r['master_code']=='TNC_SWITCH':
   tnc_item = load_tnc_switch_master()
   if tnc_item:
    tnc_description = tnc_item.get('discription', '')
    spec_lines = [part.strip() for part in tnc_description.split(',') if part.strip()]
    spec = '\n'.join(spec_lines)
   else:
    spec = r['description']+((' | '+r['details']) if r['details'] else '')
    spec_lines = [clean(spec)] if spec else []
   editable_fields=[]
   editable_indices=[]
  elif r['master_code']=='ROTARY_SWITCH':
   rotary_item = load_rotary_switch_master()
   rotary_validation = rotary_switch_validation(r, rotary_item)
   if rotary_item:
    # Always use the approved master description for the BOM output.
    # MSLD mismatches remain visible as validation warnings; they must not
    # replace the standard 4P/25A model description with the source wording.
    spec_lines = [
     'LRSW',
     'SWITRON MAKE ROTARY SWITCH',
     '2 POSITION, LEVER TYPE HANDLE (LOCKABLE)',
     'OPEN EXECUTION FLUSH BACK OF PANEL',
     'MOUNTING ON LT CHAMBER DOOR',
     'TWO POSITION 90 DEG.',
     'INSCRIPTION: LOCAL REMOTE IN CLOCKWISE SEQ.',
     'CUT OUT TYPE : H',
     'NUMBER OF POLES: 4 POLE',
     'MOUNTING FLUSH, 2 WAY WITH NO OFF',
     'VOLTAGE: 240V, AC/DC',
     'CURRENT: 25A AC/DC',
     'MODEL NO: SPC 25P 449 B/L'
    ]
    spec = '\n'.join(spec_lines)
   else:
    spec = r['description']+((' | '+r['details']) if r['details'] else '')
    spec_lines = [clean(spec)] if spec else []
   editable_fields=[]
   editable_indices=[]
  elif r['master_code']=='CONTROL_LINK':
   master_item = load_control_link_master('CONTROL-LINK-3NW-32NNSL')
   raw_spec = (master_item or {}).get('discription', '')
   spec_lines = [part.strip() for part in raw_spec.split(',') if part.strip()]
   spec = '\n'.join(spec_lines)
   editable_fields=[]
   editable_indices=[]
  elif r['master_code']=='MCB':
   mcb_item = mcb_master_match(r.get('description',''), r.get('details',''), r.get('designation',''))
   if mcb_item:
    spec, spec_lines = mcb_spec(mcb_item)
   else:
    spec = r['description']+((' | '+r['details']) if r['details'] else '')
    spec_lines = [clean(spec)] if spec else []
   editable_fields=[]
   editable_indices=[]
  elif r['master_code']=='PT_DAMPING_RESISTOR':
   spec_lines, damping_warnings = pt_damping_resistor_spec(r, records)
   spec = '\n'.join(spec_lines)
   damping_resistor_validation = {'warnings': damping_warnings}
   editable_fields=[]
   editable_indices=[]
  elif r['master_code']=='VAJH13':
   spec_lines, vajh13_warnings = vajh13_spec(r)
   spec = '\n'.join(spec_lines) if spec_lines else (r['description']+((' | '+r['details']) if r['details'] else ''))
   spec_lines = spec_lines or ([clean(spec)] if spec else [])
   auxiliary_relay_validation = {'warnings': vajh13_warnings}
   editable_fields=[]
   editable_indices=[]
  elif r['master_code']=='EM6400NG':
   spec_lines, meter_missing = em6400ng_spec(r, records)
   spec = '\n'.join(spec_lines)
   meter_validation = {'warnings': meter_missing}
   editable_fields=[]
   editable_indices=[]
  elif r['master_code']=='7SJ6611':
   # Relay validation must use only this equipment row. Passing the full MSLD
   # could borrow an MLFB from another section/row and create a relay BOM line
   # that is not actually specified for this designation.
   relay_source = ' '.join([r.get('designation',''), r.get('description',''), r.get('details','')])
   relay_analysis = analyze_7sj66(relay_source)
   # Never drop a detected relay row just because its MLFB is missing or
   # its chart-coded options conflict with the MSLD details. Preserve the
   # source text in the BOM so engineering details are not lost, and attach
   # validation feedback for the UI warning list.
   source_relay_text = clean_value(' | '.join(
    part for part in [r.get('description',''), r.get('details','')] if part
   ))
   relay_validation = relay_analysis.get('validation') or {}
   has_mlfb = bool(relay_analysis.get('fields', {}).get('mlfb', {}).get('value'))
   # Only validate/report a missing MLFB when this MSLD row explicitly
   # identifies a 7SJ66 relay. A generic numerical-relay label is not enough.
   has_7sj66_evidence = bool(re.search(r'\b7SJ66\w*\b', relay_source, re.IGNORECASE))
   if not has_mlfb and not has_7sj66_evidence:
    continue
   # When an MLFB is extracted, always show the structured description.
   # Validation errors/warnings stay separate and do not replace BOM details.
   if has_mlfb:
    spec = relay_analysis['description']
    spec_lines = spec.splitlines()
   else:
    # Keep source text only when the MSLD actually identifies a relay.
    spec = source_relay_text
    spec_lines = [spec] if spec else []
   editable_fields=[]
   editable_indices=[]
  elif r['master_code']=='E2_LED':
   spec_lines=[
    'LED',
    'PHILIPS/REPUTED MAKE LED 5W- TUBE FIXTURE',
    'SUITABLE FOR 240V AC, 50Hz AC SUPPLY',
    'SUITABLE FOR SURFACE MOUNTING'
   ]
   spec='\n'.join(spec_lines)
   editable_fields=[]
   editable_indices=[]
  elif r['master_code']=='LED':
   h789 = led_specs_for_h7_h8_h9(r.get('description',''), r.get('details',''), r.get('designation',''))
   if h789:
    # Split the source quantity across the colour-specific H7/H8/H9 rows.
    # Example: source qty 3 and three colours -> 1 each, total remains 3 per feeder.
    group_qty = int(r.get('quantity') or 1)
    split_count = len(h789)
    base_qty, remainder = divmod(group_qty, split_count)
    for part_index, part in enumerate(h789):
     part_lines=part['specification_lines']
     part_qty = base_qty + (1 if part_index < remainder else 0)
     rows.append({'sr':len(rows)+1,'specification':'\n'.join(part_lines),'specification_lines':part_lines,'editable_fields':[],'editable_indices':[],'designation':part['designation'],'feeder_name':feeder_name,'feeder_qty':feeder_qty,'total':part_qty*feeder_qty_num,'eqpt_qty':part_qty,'mpd':'','amd':'','master_code':'LED'})
    continue
   spec_lines, led_item = led_spec(r.get('description',''), r.get('details',''), r.get('designation',''))
   spec='\n'.join(spec_lines) if spec_lines else r['description']+((' | '+r['details']) if r['details'] else '')
   spec_lines=spec_lines or ([clean(spec)] if spec else [])
   editable_fields=[]
   editable_indices=[]
  else:
   spec=r['description']+((' | '+r['details']) if r['details'] else '')
   spec_lines=[clean(spec)] if spec else []
   editable_fields=[]
   editable_indices=[]
  row_sr = len(rows)+1
  row_designation = r['designation']
  row_eqpt_qty = (r['quantity'] or 1)
  fuse_designation = pt_fuse_by_pt_index.get(record_index)
  if fuse_designation:
   # pt_spec() places the built-in primary-fuse description at line index 3.
   # Blank designation lines keep F1-F3 visually aligned with that description.
   # Keep both designations on the PT row; the QTY column remains blank,
   # consistent with the existing BOM layout for other equipment.
   designation_lines = [fuse_designation, str(row_designation or '')]
   row_designation = '\n'.join(designation_lines)
  rows.append({'sr':row_sr,'specification':spec,'specification_lines':spec_lines,'editable_fields':editable_fields,'editable_indices':editable_indices,'designation':row_designation,'feeder_name':feeder_name,'feeder_qty':feeder_qty,'total':row_eqpt_qty*feeder_qty_num,'eqpt_qty':row_eqpt_qty,'mpd':'','amd':'','master_code':r['master_code'],'relay_validation':relay_analysis.get('validation') if relay_analysis else None,'vcb_validation':vcb_validation,'meter_validation':meter_validation if r['master_code']=='EM6400NG' else None,'auxiliary_relay_validation':auxiliary_relay_validation if r['master_code']=='VAJH13' else None,'damping_resistor_validation':damping_resistor_validation if r['master_code']=='PT_DAMPING_RESISTOR' else None})
  if r['master_code']=='CONTROL_LINK':
   link_sr = row_sr
   link_qty = (r['quantity'] or 1)
   base_designation = str(r.get('designation','')) + 'A'
   base_item = load_control_link_master('CONTROL-FUSE-BASE-3NW-32NNSF')
   base_spec = (base_item or {}).get('discription', '')
   base_lines = [part.strip() for part in base_spec.split(',') if part.strip()]
   if base_lines:
    rows.append({'sr':str(link_sr)+'A','specification':'\n'.join(base_lines),'specification_lines':base_lines,'editable_fields':[],'editable_indices':[],'designation':base_designation,'feeder_name':feeder_name,'feeder_qty':feeder_qty,'total':link_qty*feeder_qty_num,'eqpt_qty':link_qty,'mpd':'','amd':'','master_code':'CONTROL_FUSE_BASE'})
  if r['master_code']=='MCB':
   mcb_source_text = clean_value(' '.join([r.get('description',''), r.get('details','')])).upper()
   has_aux_1no_1nc = bool(re.search(r'\b1\s*NO\s*\+\s*1\s*NC\b', mcb_source_text) and re.search(r'\bAUX(?:ILIARY)?\b|\bCONTACTS?\b', mcb_source_text))
   if has_aux_1no_1nc:
    mcb_qty = (r['quantity'] or 1)
    addon_item = load_mcb_addon_master()
    if addon_item and addon_item.get('discription'):
     addon_spec = addon_item['discription']
     addon_lines = [part.strip() for part in addon_spec.split(',') if part.strip()]
     rows.append({'sr':len(rows)+1,'specification':'\n'.join(addon_lines),'specification_lines':addon_lines,'editable_fields':[],'editable_indices':[],'designation':str(r.get('designation',''))+'A','feeder_name':feeder_name,'feeder_qty':feeder_qty,'total':mcb_qty*feeder_qty_num,'eqpt_qty':mcb_qty,'mpd':'','amd':'','master_code':'MCB ADD-ON'})
 return rows,feeder_name,feeder_qty

def e2_led_wattage_warning(records, source_name=''):
    messages = []
    for record in records:
        if record.get('master_code') != 'E2_LED':
            continue
        source_text = ' '.join([
            str(record.get('designation') or ''),
            str(record.get('description') or ''),
            str(record.get('details') or '')
        ])
        match = re.search(r'(?<![A-Z0-9])(\d+(?:\.\d+)?)\s*W(?:ATT(?:S)?)?\b', source_text, re.IGNORECASE)
        if match and float(match.group(1)) != 5:
            label = str(record.get('designation') or 'E2 LED')
            prefix = (str(source_name) + ': ') if source_name else ''
            messages.append(prefix + label + ': Please recheck the LED lamp wattage. MSLD shows ' + match.group(1) + 'W; required wattage is 5W.')
    return messages


def extract_from_files(msld_bytes,msld_name,dis_bytes,dis_name,client,sales,drawing,esd,wo,prep,voltage):
 msld_text=pdf_text(msld_bytes,msld_name); dis_text=pdf_text(dis_bytes,dis_name); same=bool(msld_text.strip()) and re.sub(r'\s+','',msld_text)==re.sub(r'\s+','',dis_text); feeder_info,records,warnings=extract_fixed_table(msld_bytes,msld_name)
 if same:warnings.append('The same PDF was supplied in both MSLD and DIS slots. It was processed once; DIS was used as the validation copy.')
 elif dis_text.strip():warnings.append('DIS supplied: used as the fixed specification source; MSLD remains the primary project-data source.')
 rows,feeder_name,feeder_qty=build_rows(feeder_info,records,msld_text,dis_text)
 for record in records:
  if record.get('master_code') == 'TNC_SWITCH':
   validation = tnc_switch_validation(record)
   label = 'Switron TNC switch ' + str(record.get('designation') or 'S14')
   warnings.extend([label + ': ' + message for message in validation['errors']])
 for record in records:
  if record.get('master_code') == 'ROTARY_SWITCH':
   validation = rotary_switch_validation(record, load_rotary_switch_master())
   label = 'Switron rotary switch ' + str(record.get('designation') or 'S13')
   warnings.extend([label + ': ' + message for message in validation['errors']])
 for record in records:
  if (record.get('master_code') == 'PLUG_SOCKET_SWITCH' and re.search(r'\b(?:1P|1 POLE|SINGLE POLE|SP)\b', ' '.join([str(record.get('description') or ''), str(record.get('details') or ''), str(record.get('designation') or '')]), re.I) and re.search(r'\bMCB\b|MINIATURE CIRCUIT BREAKER', ' '.join([str(record.get('description') or ''), str(record.get('details') or ''), str(record.get('designation') or '')]), re.I)):
   warnings.append('Plug socket ' + str(record.get('designation') or '') + ': 1P MCB is shown in the plug socket row. Please use a switched plug socket with built-in switch instead of a separate 1P MCB.')
 warnings.extend(e2_led_wattage_warning(records))
 for row in rows:
  meter_validation = row.get('meter_validation')
  if meter_validation:
   meter_label = 'EM6400NG meter ' + str(row.get('designation', ''))
   warnings.extend([meter_label + ': ' + message for message in meter_validation.get('warnings', [])])
  damping_validation = row.get('damping_resistor_validation')
  if damping_validation:
   damping_label = 'PT damping resistor ' + str(row.get('designation', ''))
   warnings.extend([damping_label + ': ' + message for message in damping_validation.get('warnings', [])])
  auxiliary_validation = row.get('auxiliary_relay_validation')
  if auxiliary_validation:
   auxiliary_label = 'VAJH13 master trip relay ' + str(row.get('designation', ''))
   warnings.extend([auxiliary_label + ': ' + message for message in auxiliary_validation.get('warnings', [])])
  validation=row.get('relay_validation')
  if validation:
   relay_label='7SJ66 relay '+str(row.get('designation',''))
   warnings.extend([relay_label+': '+message for message in validation.get('errors',[])])
   warnings.extend([relay_label+': '+message for message in validation.get('warnings',[])])
 h=header(msld_text,dis_text,client,sales,drawing,esd,wo,prep,voltage); ph=extract_header_from_page(msld_bytes,msld_name)
 for key in ('client','sales_ref','drawing','wo'):
  if ph.get(key):h[key]=ph[key]
 if ph.get('esd'):h['esd']=ph['esd']
 source=msld_text+'\n'+dis_text; q=first(r'\bQty\.\s*:\s*(\d+x?)\b',source) or first(r'\bQTY\.\s*:\s*(\d+x?)\b',source) or ph.get('qty',''); v=norm_voltage(voltage) or first(r'\b(6\.6|11|33)\s*KV\b',source); description=f'{v}kV SWITCHBOARD' if v else 'SWITCHBOARD'
 if not rows:warnings.append('No equipment rows were confidently extracted from the fixed MSLD table.')
 unmatched=[r['designation'] for r in records if r['master_code']=='UNMATCHED']
 if unmatched:warnings.append('Master Data match not available for: '+', '.join(unmatched)+'. Source description was preserved; no engineering item was guessed.')
 unmatched_mcb=[r['designation'] for r in records if r['master_code']=='MCB' and not mcb_master_match(r.get('description',''),r.get('details',''),r.get('designation',''))]
 if unmatched_mcb:warnings.append('MCB specifications could not be fully verified against the master DISCRIPTION for: '+', '.join(unmatched_mcb)+'. Source text was preserved; verify before issuing the BOM.')
 return {'header':h,'document_no':'SI EA/CS/FR/EG/015','revision':'1.0','effective_date':'17/07/2026','created_by':'EA CS ENGG','description':description,'rows':rows,'feeder_name':feeder_name,'feeder_qty':feeder_qty,'qty':q,'warnings':warnings,'feeder_details':feeder_info,'same_input_file':same}


def extract_multiple_from_files(msld_files, dis_bytes, dis_name, client, sales, drawing, esd, wo, prep, voltage):
    """Build one BOM across many MSLDs, using one common DIS as the specification source."""
    dis_text = pdf_text(dis_bytes, dis_name)
    all_rows = []
    feeders = []
    warnings = []
    header_source = ""
    first_ph = {}
    qty_text = ""
    for idx, item in enumerate(msld_files):
        msld_bytes, msld_name = item
        msld_text = pdf_text(msld_bytes, msld_name)
        if idx == 0:
            header_source = msld_text
            first_ph = extract_header_from_page(msld_bytes, msld_name)
            source = msld_text + "\n" + dis_text
            qty_text = first(r'\bQty\.\s*:\s*(\d+x?)\b', source) or first(r'\bQTY\.\s*:\s*(\d+x?)\b', source) or first_ph.get('qty', '')
        feeder_info, records, file_warnings = extract_fixed_table(msld_bytes, msld_name)
        for record in records:
            if record.get('master_code') == 'TNC_SWITCH':
                validation = tnc_switch_validation(record)
                label = msld_name + ': Switron TNC switch ' + str(record.get('designation') or 'S14')
                warnings.extend([label + ': ' + message for message in validation['errors']])
        for record in records:
            if record.get('master_code') == 'ROTARY_SWITCH':
                validation = rotary_switch_validation(record, load_rotary_switch_master())
                label = msld_name + ': Switron rotary switch ' + str(record.get('designation') or 'S13')
                warnings.extend([label + ': ' + message for message in validation['errors']])
        for record in records:
            if record.get('master_code') == 'SPACE_HEATER':
                label = msld_name + ': Space heater ' + str(record.get('designation') or '')
                warnings.extend(label + ': ' + message for message in space_heater_validation(record))
        warnings.extend(e2_led_wattage_warning(records, msld_name))
        if not feeder_info:
            warnings.append(f"{msld_name}: feeder information could not be confidently extracted.")
            feeder_info = [{'name': os.path.splitext(os.path.basename(msld_name))[0], 'designation': os.path.splitext(os.path.basename(msld_name))[0], 'quantity': '1'}]
        feeder_name = feeder_info[0].get('designation') or feeder_info[0].get('name') or f"MSLD {idx + 1}"
        feeder_qty_text = feeder_info[0].get('quantity', '1')
        qty_match = re.search(r'\d+', str(feeder_qty_text))
        feeder_qty_num = int(qty_match.group()) if qty_match else 1
        feeders.append({'name': str(feeder_name), 'qty': str(feeder_qty_text), 'quantity': feeder_qty_num, 'source_file': msld_name})
        built, _, _ = build_rows(feeder_info, records, msld_text, dis_text)
        for row in built:
            meter_validation = row.get('meter_validation')
            if meter_validation:
                meter_label = str(msld_name) + ': EM6400NG meter ' + str(row.get('designation', ''))
                warnings.extend([meter_label + ': ' + message for message in meter_validation.get('warnings', [])])
            auxiliary_validation = row.get('auxiliary_relay_validation')
            if auxiliary_validation:
                auxiliary_label = str(msld_name) + ': VAJH13 master trip relay ' + str(row.get('designation', ''))
                warnings.extend([auxiliary_label + ': ' + message for message in auxiliary_validation.get('warnings', [])])
            validation = row.get('relay_validation')
            if validation:
                relay_label = str(msld_name) + ': 7SJ66 relay ' + str(row.get('designation', ''))
                warnings.extend([relay_label + ': ' + message for message in validation.get('errors', [])])
                warnings.extend([relay_label + ': ' + message for message in validation.get('warnings', [])])
        for row in built:
            row['_feeder_index'] = idx
            all_rows.append(row)
        warnings.extend([f"{msld_name}: {w}" for w in file_warnings])
        unmatched = [r['designation'] for r in records if r['master_code'] == 'UNMATCHED']
        if unmatched:
            warnings.append(f"{msld_name}: Master Data match not available for: " + ', '.join(unmatched) + ". Source description was preserved; no engineering item was guessed.")
        unmatched_mcb = [r['designation'] for r in records if r['master_code'] == 'MCB' and not mcb_master_match(r.get('description',''), r.get('details',''), r.get('designation',''))]
        if unmatched_mcb:
            warnings.append(f"{msld_name}: MCB specifications could not be fully verified against the master DISCRIPTION for: " + ', '.join(unmatched_mcb) + ". Source text was preserved; verify before issuing the BOM.")
    # Merge identical equipment only when its normalized specification and category match.
    merged = {}
    for row in all_rows:
        idx = row.pop('_feeder_index')
        spec_lines = row.get('specification_lines') or []
        identity_text = row.get('specification') or '\n'.join(spec_lines)
        key = (str(row.get('master_code', '')).strip().upper(), re.sub(r'\s+', ' ', identity_text).strip().casefold())
        if key not in merged:
            entry = dict(row)
            entry['sr'] = len(merged) + 1
            entry['feeder_quantities'] = [0] * len(feeders)
            entry['designation_by_feeder'] = [''] * len(feeders)
            entry['total'] = 0
            entry['eqpt_qty'] = 0
            merged[key] = entry
        entry = merged[key]
        qty = row.get('eqpt_qty')
        if qty is None:
            qty = 1
        try:
            qty = int(qty)
        except (ValueError, TypeError):
            qty = 1
        # Feeder columns show equipment quantity per typical panel. Total is
        # calculated only after merging, using that feeder's panel multiplier.
        entry['feeder_quantities'][idx] += qty
        designation = str(row.get('designation', '') or '').strip()
        if designation:
            existing = entry['designation_by_feeder'][idx]
            if isinstance(existing, list):
                if designation not in existing:
                    existing.append(designation)
            elif existing and existing != designation:
                entry['designation_by_feeder'][idx] = [existing, designation]
            else:
                entry['designation_by_feeder'][idx] = [designation]
        entry['eqpt_qty'] += qty
    rows = list(merged.values())
    for row in rows:
        quantities = row.get('feeder_quantities') or []
        per_feeder_quantities = []
        total = 0
        for i, feeder in enumerate(feeders):
            try:
                equipment_qty = int(quantities[i] or 0) if i < len(quantities) else 0
            except (TypeError, ValueError):
                equipment_qty = 0
            try:
                feeder_qty = int(feeder.get('quantity') or 1)
            except (TypeError, ValueError):
                feeder_qty = 1
            per_feeder_quantities.append(equipment_qty)
            total += equipment_qty * max(1, feeder_qty)
        row['feeder_quantities'] = per_feeder_quantities
        row['total'] = total
    def equipment_order(row):
        code = str(row.get('master_code', '')).strip().upper()
        spec = str(row.get('specification', '')).upper()
        text = ' '.join([code, spec, str(row.get('designation', '')).upper()])
        # Keep corresponding equipment from all MSLDs together in a stable,
        # engineering-oriented category order.
        if code == 'CT' or re.search(r'\bCURRENT TRANSFORMER\b|\bC\.T\.\b', text):
            rank = 0
        elif code in ('PT', 'VT') or re.search(r'\b(POTENTIAL|VOLTAGE) TRANSFORMER\b|\bP\.T\.\b', text):
            rank = 1
        elif code in ('LA', 'SA') or 'SURGE ARRESTER' in text:
            rank = 2
        elif code in ('CB', 'VCB', 'BREAKER') or 'CIRCUIT BREAKER' in text:
            rank = 3
        elif code.startswith('MCB') or re.search(r'\bMCB\b', text):
            rank = 4
        elif code in ('LED', 'LAMP') or 'INDICATION LAMP' in text or re.search(r'\bLED\b', text):
            rank = 5
        elif code in ('CONTROL_LINK', 'CONTROL_FUSE_BASE'):
            rank = 6
        else:
            rank = 7

        designation = str(row.get('designation', '') or '').strip().upper()
        parent_designation = designation[:-1] if code == 'CONTROL_FUSE_BASE' and designation.endswith('A') else designation
        pair_order = 1 if code == 'CONTROL_FUSE_BASE' else 0
        return (
            rank,
            parent_designation if code in ('CONTROL_LINK', 'CONTROL_FUSE_BASE') else code,
            pair_order,
            re.sub(r'\s+', ' ', str(row.get('specification', ''))).casefold(),
            designation,
        )

    # Rebuild control equipment descriptions from the master after feeder merging
    # so their specification text is still rendered as separate BOM lines.
    for row in rows:
        code = str(row.get('master_code', '')).strip().upper()
        if code in ('CONTROL_LINK', 'CONTROL_FUSE_BASE'):
            material_code = 'CONTROL-LINK-3NW-32NNSL' if code == 'CONTROL_LINK' else 'CONTROL-FUSE-BASE-3NW-32NNSF'
            master_item = load_control_link_master(material_code)
            master_description = (master_item or {}).get('discription', '')
            lines = [part.strip() for part in master_description.split(',') if part.strip()]
            if lines:
                row['specification_lines'] = lines
                row['specification'] = '\n'.join(lines)

    rows.sort(key=equipment_order)
    next_sr = 1
    last_control_link_sr = None
    for row in rows:
        code = str(row.get('master_code', '')).strip().upper()
        if code == 'CONTROL_FUSE_BASE' and last_control_link_sr is not None:
            row['sr'] = str(last_control_link_sr) + 'A'
        else:
            row['sr'] = next_sr
            if code == 'CONTROL_LINK':
                last_control_link_sr = next_sr
            next_sr += 1
        designations = []
        for values in row.pop('designation_by_feeder', []):
            if isinstance(values, str):
                values = [values]
            for value in values or []:
                value = str(value).strip()
                if value and value not in designations:
                    designations.append(value)
        row['designation'] = ', '.join(designations)
        row['feeder_name'] = ''
        row['feeder_qty'] = ''
    h = header(header_source, dis_text, client, sales, drawing, esd, wo, prep, voltage)
    for key in ('client', 'sales_ref', 'drawing', 'wo'):
        if first_ph.get(key):
            h[key] = first_ph[key]
    if first_ph.get('esd'):
        h['esd'] = first_ph['esd']
    source = header_source + '\n' + dis_text
    v = norm_voltage(voltage) or first(r'\b(6\.6|11|33)\s*KV\b', source)
    description = f'{v}kV SWITCHBOARD' if v else 'SWITCHBOARD'
    if not rows:
        warnings.append('No equipment rows were confidently extracted from the supplied MSLD files.')
    if dis_text.strip():
        warnings.append('One common DIS supplied: used as the specification source for all MSLDs.')
    return {
        'header': h, 'document_no': 'SI EA/CS/FR/EG/015', 'revision': '1.0',
        'effective_date': '17/07/2026', 'created_by': 'EA CS ENGG',
        'description': description, 'rows': rows, 'feeders': feeders,
        'feeder_name': feeders[0]['name'] if feeders else '',
        'feeder_qty': feeders[0]['qty'] if feeders else '',
        'qty': qty_text, 'warnings': list(dict.fromkeys(warnings)),
        'feeder_details': feeders, 'same_input_file': False
    }

@app.post('/api/bom/check-customers')
async def check_msld_customers(
    mslds: list[UploadFile] = File(...),
    dis: UploadFile = File(...),
    client: str = Form(''),
    wo: str = Form(''),
    voltage: str = Form(''),
    user: str = Depends(current_user)
):
    """Check MSLD and common DIS header values before BOM generation."""
    results = []
    for upload in mslds:
        data = await upload.read()
        filename = upload.filename or 'MSLD.pdf'
        extracted = extract_header_from_page(data, filename)
        source_text = pdf_text(data, filename)
        customer = clean_value(extracted.get('client', '')).strip(' :-')
        if not customer:
            customer = first(r'Client\s*[:\-]*\s*([^\n]+)', source_text).strip(' :-') or first(r'Customer\s*Name\s*[:\-]*\s*([^\n]+)', source_text).strip(' :-')
        work_order = clean_value(extracted.get('wo', '')).strip(' :-')
        if not work_order:
            work_order = first(r'W\.?\s*O\.?\s*(?:No\.?)?\s*[:\-]\s*([^\n]+)', source_text).strip(' :-')
        voltage_match = first(r'\b(6\.6|11|33)\s*k?V\b', source_text)
        results.append({'filename': filename, 'customer': clean_value(customer), 'work_order': clean_value(work_order), 'voltage': clean_value(voltage_match)})
    dis_bytes = await dis.read()
    dis_name = dis.filename or 'DIS.pdf'
    dis_header = extract_header_from_page(dis_bytes, dis_name)
    dis_text = pdf_text(dis_bytes, dis_name)
    # DIS is a two-column footer table. Parse the Client/W.O. values from
    # words on the same visual row, stopping before the right-hand columns.
    dis_words = page_words(pdf_pages(dis_bytes, dis_name)[0]) if pdf_pages(dis_bytes, dis_name) else []
    def footer_value(label_pattern):
        # Read only words to the right of the requested label on the same visual row.
        labels = [
            w for w in dis_words
            if re.fullmatch(label_pattern, w['text'].strip(' :.-'), re.IGNORECASE)
        ]
        for label_word in labels:
            row = sorted(
                [w for w in dis_words if abs(w['y'] - label_word['y']) <= 8 and w['x'] > label_word['x']],
                key=lambda w: w['x']
            )
            picked = []
            for word in row:
                token = word['text'].strip(' :.-')
                if not token:
                    continue
                if re.match(
                    r'^(?:Sales|Project|W\.?O\.?|Item|Qty|Description|Sh\.?|Work|Details|Drg\.?|Date|Prep|Ckd|Client)\b',
                    token,
                    re.IGNORECASE
                ):
                    break
                picked.append(token)
            value = clean_value(' '.join(picked)).strip(' :-')
            if value:
                return value
        return ''

    dis_customer = footer_value(r'^Client$')
    dis_wo = footer_value(r'^(?:W\.?O\.?|Work\s*Order)$')
    if not dis_customer:
        dis_customer = clean_value(dis_header.get('client', '')).strip(' :-')
    if not dis_wo:
        dis_wo = clean_value(dis_header.get('wo', '')).strip(' :-')
    dis_voltage = first(r'\b(6\.6|11|33)\s*k?V\b', dis_text)
    def distinct(values):
        return list(dict.fromkeys(re.sub(r'\s+', ' ', str(v)).strip() for v in values if str(v or '').strip()))
    customers = distinct([x['customer'] for x in results])
    work_orders = distinct([x['work_order'] for x in results])
    voltages = distinct([x['voltage'] for x in results])
    mismatches = []
    if len({x.casefold() for x in customers}) > 1:
        mismatches.append('The uploaded MSLD PDFs contain different customer names.')
    if len({x.casefold() for x in work_orders}) > 1:
        mismatches.append('The uploaded MSLD PDFs contain different work order numbers.')
    if len({x.casefold() for x in voltages}) > 1:
        mismatches.append('The uploaded MSLD PDFs contain different voltage values.')
    entered = {'customer': clean_value(client), 'work_order': clean_value(wo), 'voltage': clean_value(voltage)}
    for label, values, dis_value, entered_value in [
        ('Customer name', customers, dis_customer, entered['customer']),
        ('Work order number', work_orders, dis_wo, entered['work_order']),
        ('Voltage', voltages, dis_voltage, entered['voltage'])
    ]:
        all_values = distinct(values + ([dis_value] if dis_value else []) + ([entered_value] if entered_value else []))
        if label == 'Voltage':
            all_values = distinct([norm_voltage(value) or value for value in all_values])
        if len({x.casefold() for x in all_values}) > 1:
            mismatches.append(f'{label} differs between entered details, MSLD and/or DIS.')

    # Do not silently proceed when a PDF header field could not be extracted.
    # The existing verification modal displays these messages and lets the user
    # review the extracted values before choosing whether to continue.
    for item in results:
        if not item['work_order']:
            mismatches.append(f"Work order number could not be read from MSLD: {item['filename']}.")
        if not item['voltage']:
            mismatches.append(f"Voltage could not be read from MSLD: {item['filename']}.")
    if not dis_wo:
        mismatches.append(f"Work order number could not be read from DIS: {dis_name}.")
    if not dis_voltage:
        mismatches.append(f"Voltage could not be read from DIS: {dis_name}.")
    return {
        'mslds': results, 'customers': customers, 'different_customers': len({x.casefold() for x in customers}) > 1,
        'unknown_customer_files': [x['filename'] for x in results if not x['customer']],
        'mismatches': list(dict.fromkeys(mismatches)),
        'dis': {'filename': dis_name, 'customer': dis_customer, 'work_order': dis_wo, 'voltage': clean_value(dis_voltage)}
    }

@app.post('/api/bom/preview-multiple')
async def preview_multiple(
    client: str = Form(''), sales_ref: str = Form(''), drawing: str = Form(''),
    esd: str = Form(''), wo: str = Form(''), prep_by: str = Form(''),
    voltage: str = Form(''), mslds: list[UploadFile] = File(...),
    dis: UploadFile = File(...), user: str = Depends(current_user)
):
    if not mslds:
        raise HTTPException(status_code=400, detail='Upload at least one MSLD PDF.')
    dis_name = dis.filename or 'DIS.pdf'
    dis_bytes = await dis.read()
    items = []
    filenames = []
    for upload in mslds:
        filename = upload.filename or 'MSLD.pdf'
        items.append((await upload.read(), filename))
        filenames.append(filename)
    result = extract_multiple_from_files(items, dis_bytes, dis_name, client, sales_ref, drawing, esd, wo, prep_by, voltage)
    import json
    with db() as conn:
        conn.execute(
            """INSERT INTO bom_history (username, customer_name, work_order_no, voltage, msld_files, dis_filename, row_count)
               VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)""",
            (user, str(client or result.get('header', {}).get('client', '')), str(wo or result.get('header', {}).get('wo', '')),
             str(voltage or ''), json.dumps(filenames), dis_name, len(result.get('rows') or []))
        )
        conn.commit()
    return result




class SiemensLookupRequest(BaseModel):
    mlfb: str


@app.post('/api/siemens/product-lookup')
def siemens_product_lookup(payload: SiemensLookupRequest, user: str = Depends(current_user)):
    """POC: live lookup of a supplied 7SJ66 MLFB on Siemens Industry Mall."""
    return lookup_siemens_product(payload.mlfb)

@app.get('/api/admin/bom-history')
def admin_bom_history(user: str = Depends(require_admin)):
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) AS total FROM bom_history").fetchone()['total']
        rows = conn.execute(
            """SELECT id, username, customer_name, work_order_no, voltage, msld_files,
                      dis_filename, row_count, created_at
               FROM bom_history ORDER BY created_at DESC LIMIT 500"""
        ).fetchall()
    return {'total_generated': total, 'history': rows}

def export_combined_book(payload):
    wb = Workbook()
    ws = wb.active
    ws.title = 'BOM'
    thin = Side(style='thin')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    header_data = payload.get('header', {})
    feeders = payload.get('feeders') or []
    rows = payload.get('rows') or []
    n = max(1, len(feeders))
    qty_col = 4
    feeder_start_col = 5
    total_col = feeder_start_col + n
    mpd_col, amd_col = total_col + 1, total_col + 2
    last_col = amd_col
    ws.page_setup.orientation = 'landscape'
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 1
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_margins.left = .25
    ws.page_margins.right = .25
    ws.page_margins.top = .45
    ws.page_margins.bottom = .4
    ws.sheet_view.showGridLines = False
    spec_width = max(36, min(78, max([max((len(line) for line in str(r.get('specification','')).splitlines()), default=0) for r in rows] + [36]) * 0.75))
    designation_width = max(14, min(26, max([len(str(r.get('designation',''))) for r in rows] + [14]) * 0.9))
    feeder_width = max(10, min(18, max([len(str(f.get('name',''))) for f in feeders] + [8]) * 0.9))
    widths = [9, spec_width, designation_width, 9] + [feeder_width] * n + [13, 7, 7]
    for col, width in enumerate(widths, 1):
        ws.column_dimensions[__import__('openpyxl').utils.get_column_letter(col)].width = width
    ws.merge_cells(start_row=1, start_column=1, end_row=2, end_column=min(3, last_col))
    ws.cell(1, 1, '-: Equipment List :-')
    ws.cell(1, 1).font = Font(name='Courier New', size=10)
    ws.cell(1, 1).alignment = Alignment(horizontal='center', vertical='center')
    meta_col = max(4, last_col - 2)
    ws.merge_cells(start_row=1, start_column=meta_col, end_row=2, end_column=last_col)
    ws.cell(1, meta_col, f'Doc.No.: {payload.get("document_no","SI EA/CS/FR/EG/015")}\nRev.No.: {payload.get("revision","1.0")}, Eff.Dt: {payload.get("effective_date","17/07/2026")}\nCreated By: {payload.get("created_by","EA CS ENGG")}')
    ws.cell(1, meta_col).font = Font(name='Arial', size=7)
    ws.cell(1, meta_col).alignment = Alignment(horizontal='right', vertical='top', wrap_text=True)
    ws.row_dimensions[1].height = 34
    ws.row_dimensions[2].height = 16
    for col, label in [(1,'EQPT.NO'),(2,'SPECIFICATION'),(3,'DESIGNATION'),(total_col,'TOTAL EQPT QTY.'),(mpd_col,'MPD'),(amd_col,'AMD')]:
        ws.cell(4, col, label)
    for col in [1,2,3,total_col,mpd_col,amd_col]:
        ws.merge_cells(start_row=4, start_column=col, end_row=6, end_column=col)
    ws.merge_cells(start_row=5, start_column=qty_col, end_row=6, end_column=qty_col)
    ws.cell(5,qty_col,'QTY.')
    ws.merge_cells(start_row=4, start_column=feeder_start_col, end_row=4, end_column=feeder_start_col+n-1)
    ws.cell(4,feeder_start_col,'FEEDER TYPICAL')
    for idx, feeder in enumerate(feeders):
        col=feeder_start_col+idx
        ws.cell(5,col,feeder.get('name',''))
        ws.cell(6,col,feeder.get('quantity') or feeder.get('qty') or 1)
    for r in range(4,7):
        for c in range(1,last_col+1):
            cell=ws.cell(r,c)
            cell.border=border
            cell.alignment=Alignment(horizontal='center',vertical='center',wrap_text=True)
            cell.font=Font(name='Courier New',size=8,bold=True)
    ws.row_dimensions[4].height=23
    ws.row_dimensions[5].height=18
    ws.row_dimensions[6].height=28
    for i,row in enumerate(rows[:64],7):
        quantities = row.get('feeder_quantities') or []
        vals=[row.get('sr',''),row.get('specification',''),row.get('designation','')]
        vals.append('')
        vals.extend(quantities[j] if j < len(quantities) and quantities[j] not in (0, '0', None, '') else '' for j in range(n))
        total_value = row.get('total', '')
        vals.extend(['' if total_value in (0, '0') else total_value, '', ''])
        for col,val in enumerate(vals,1):
            cell=ws.cell(i,col,val)
            cell.border=border
            cell.font=Font(name='Courier New',size=7)
            cell.alignment=Alignment(horizontal='center' if col!=2 else 'left',vertical='center',wrap_text=col==2)
        spec_lines = str(row.get('specification', '')).splitlines() or ['']
        chars_per_line = max(12, int(spec_width * 1.35))
        wrapped_lines = sum(max(1, (len(line) + chars_per_line - 1) // chars_per_line) for line in spec_lines)
        ws.row_dimensions[i].height = max(18, min(240, 10 * wrapped_lines + 6))
    for i in range(7+len(rows[:64]),70):
        for c in range(1,last_col+1):
            ws.cell(i,c).border=border
            ws.cell(i,c).font=Font(name='Courier New',size=7)
        ws.row_dimensions[i].height=15
    # Keep worksheet view focused on the equipment list. Document metadata is
    # printed in the page header/footer when the user presses Ctrl+P.
    ws.oddHeader.center.text = "&B Equipment List &B"
    ws.oddHeader.right.text = f"Doc.No.: {payload.get('document_no','SI EA/CS/FR/EG/015')}\\nRev.No.: {payload.get('revision','1.0')}  Eff.Dt: {payload.get('effective_date','17/07/2026')}"
    ws.oddHeader.right.size = 7
    ws.oddFooter.left.text = f"Client: {header_data.get('client','')}  |  Sales Ref: {header_data.get('sales_ref','')}\\nDescription: {payload.get('description','')}  |  WO: {header_data.get('wo','')}  |  Drawing: {header_data.get('drawing','')}"
    ws.oddFooter.center.text = f"Prepared by: {header_data.get('prep_by','')}  |  Qty: {payload.get('qty','')}  |  ESD: {header_data.get('esd','')}"
    ws.oddFooter.right.text = "Page &P of &N"
    ws.oddFooter.left.size = 7
    ws.oddFooter.center.size = 7
    ws.oddFooter.right.size = 7
    ws.print_area=f'A1:{__import__("openpyxl").utils.get_column_letter(last_col)}76'
    return wb

def export_combined_pdf(payload):
    out=io.BytesIO()
    doc=SimpleDocTemplate(out,pagesize=A4,rightMargin=5*mm,leftMargin=5*mm,topMargin=6*mm,bottomMargin=20*mm)
    styles=getSampleStyleSheet()
    title=ParagraphStyle('combinedtitle',parent=styles['Normal'],fontName='Helvetica-Bold',fontSize=6,leading=7,alignment=TA_CENTER)
    cell=ParagraphStyle('combinedcell',parent=styles['Normal'],fontName='Helvetica',fontSize=4.2,leading=4.8,spaceAfter=0,spaceBefore=0,wordWrap='CJK')
    center=ParagraphStyle('combinedcenter',parent=cell,alignment=TA_CENTER)
    head=ParagraphStyle('combinedhead',parent=cell,fontName='Helvetica-Bold',alignment=TA_CENTER,fontSize=4.0,leading=4.5)
    feeders=payload.get('feeders') or []
    rows=payload.get('rows') or []
    n=max(1,len(feeders))
    data=[[Paragraph('EQPT.NO',head),Paragraph('SPECIFICATION',head),Paragraph('DESIGNATION',head),''] + [Paragraph('FEEDER<br/>TYPICAL',head) if i==0 else '' for i in range(n)] + [Paragraph('TOTAL<br/>EQPT QTY.',head),Paragraph('MPD',head),Paragraph('AMD',head)]]
    data.append(['','','',Paragraph('QTY.',head)]+[Paragraph(str(f.get('name','')),head) for f in feeders]+['','',''])
    data.append(['','','','']+[Paragraph(str(f.get('quantity') or f.get('qty') or 1),center) for f in feeders]+['','',''])
    for row in rows[:64]:
        spec=str(row.get('specification','') or '').replace('&','&amp;').replace('<','&lt;').replace('>','&gt;').replace('\n','<br/>')
        qs=row.get('feeder_quantities') or []
        data.append([Paragraph(str(row.get('sr','')),center),Paragraph(spec,cell),Paragraph(str(row.get('designation','') or ''),center),Paragraph('',center)]+[Paragraph(str(qs[i] if i<len(qs) and qs[i] not in (0, '0', None, '') else ''),center) for i in range(n)]+[Paragraph('' if row.get('total') in (0, '0') else str(row.get('total') or ''),center),'',''])
    available=200*mm
    fixed=[8*mm,66*mm,25*mm,9*mm,12*mm,10*mm,10*mm]
    # Allocate a little more space to long feeder labels, while keeping the
    # complete table inside landscape A4. Paragraph cells wrap and expand rows.
    remaining=max(8*mm, available-sum(fixed))
    feeder_labels=[len(str(f.get('name',''))) for f in feeders]
    weights=[max(1.0,min(2.0,length/10.0)) for length in feeder_labels]
    weight_sum=sum(weights) or n
    feeder_widths=[remaining*w/weight_sum for w in weights] if feeders else [remaining]
    widths=[fixed[0],fixed[1],fixed[2],fixed[3]]+feeder_widths+[fixed[4],fixed[5],fixed[6]]
    tbl=Table(data,colWidths=widths,rowHeights=[18,14,20]+[None]*(len(data)-3),repeatRows=3)
    spans=[('SPAN',(0,0),(0,2)),('SPAN',(1,0),(1,2)),('SPAN',(2,0),(2,2)),('SPAN',(3,1),(3,2)),('SPAN',(4,0),(3+n,0)),('SPAN',(4+n,0),(4+n,2)),('SPAN',(5+n,0),(5+n,2)),('SPAN',(6+n,0),(6+n,2))]
    # A compact first-row group label and feeder-specific second/third header rows.
    tbl.setStyle(TableStyle(spans+[('GRID',(0,0),(-1,-1),0.45,colors.black),('BACKGROUND',(0,0),(-1,2),colors.HexColor('#d9e7f0')),('VALIGN',(0,0),(-1,-1),'MIDDLE'),('ALIGN',(0,0),(-1,2),'CENTER'),('LEFTPADDING',(0,0),(-1,-1),2),('RIGHTPADDING',(0,0),(-1,-1),2),('TOPPADDING',(0,0),(-1,-1),1),('BOTTOMPADDING',(0,0),(-1,-1),1)]))
    story=[Paragraph('-: Equipment List :-',title),Spacer(1,3*mm),tbl]
    h=payload.get('header',{})
    def footer(canvas, doc_obj):
        canvas.saveState()
        w,hp=doc_obj.pagesize
        y=8*mm
        canvas.setFont('Helvetica',6.5)
        canvas.drawString(6*mm,y+7*mm,f"Client: {h.get('client','')}  Sales Ref: {h.get('sales_ref','')}")
        canvas.drawString(6*mm,y+4*mm,f"Description: {payload.get('description','')}  WO: {h.get('wo','')}  Drawing: {h.get('drawing','')}")
        canvas.drawString(6*mm,y+1*mm,f"Prepared by: {h.get('prep_by','')}  Qty: {payload.get('qty','')}  ESD: {h.get('esd','')}")
        canvas.restoreState()
    doc.build(story,onFirstPage=footer,onLaterPages=footer,canvasmaker=NumberedCanvas)
    out.seek(0)
    return out

@app.get('/')
def root():
    return {
        'service': 'ELEXORA 2 API',
        'status': 'running',
        'health': '/health',
        'docs': '/docs',
    }


@app.get('/health')
def health():
    return {'status':'ok'}

@app.on_event("startup")
def startup():
    init_auth_db()

@app.post('/api/auth/signup')
def signup(payload: AuthRequest):
    username = payload.username.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username):
        raise HTTPException(status_code=400, detail="Username must be 3-32 characters and use only letters, numbers, dot, underscore or hyphen.")
    errors = validate_password(payload.password)
    if errors:
        raise HTTPException(status_code=400, detail="Password must contain " + ", ".join(errors) + ".")
    try:
        with db() as conn:
            conn.execute(
                "INSERT INTO users(username,password_hash) VALUES(%s,%s)",
                (username.lower(), password_hash(payload.password))
            )
            conn.commit()
    except Exception as exc:
        if getattr(exc, "sqlstate", None) == "23505":
            raise HTTPException(status_code=409, detail="That username already exists. Please choose another.")
        raise
    return {"message":"Account created successfully. You can now sign in."}

@app.post('/api/auth/login')
def login(payload: AuthRequest):
    username = payload.username.strip().lower()
    with db() as conn:
        row = conn.execute(
            "SELECT id,password_hash,username FROM users WHERE username=%s",
            (username,)
        ).fetchone()
        if not row or not password_ok(payload.password, row["password_hash"]):
            raise HTTPException(status_code=401, detail="Invalid username or password.")
        token = secrets.token_urlsafe(32)
        expires = int(datetime.now().timestamp()) + TOKEN_TTL_SECONDS
        conn.execute(
            "INSERT INTO sessions(token,user_id,expires_at) VALUES(%s,%s,%s)",
            (token, row["id"], expires)
        )
        conn.commit()
    return {"token":token,"username":row["username"],"expires_at":expires}

@app.post('/api/auth/logout')
def logout(authorization: str = Header(default="")):
    token = authorization[7:].strip() if authorization.startswith("Bearer ") else ""
    if token:
        with db() as conn:
            conn.execute("DELETE FROM sessions WHERE token=%s", (token,))
            conn.commit()
    return {"message":"Signed out."}

@app.post('/api/bom/preview')
async def preview(client:str=Form(''),sales_ref:str=Form(''),drawing:str=Form(''),esd:str=Form(''),wo:str=Form(''),prep_by:str=Form(''),voltage:str=Form(''),msld:UploadFile=File(...),dis:UploadFile=File(...),user:str=Depends(current_user)):
 m,d=await msld.read(),await dis.read(); return extract_from_files(m,msld.filename,d,dis.filename,client,sales_ref,drawing,esd,wo,prep_by,voltage)

def export_book(payload):
 if payload.get('feeders'):
  return export_combined_book(payload)
 wb=Workbook(); ws=wb.active; ws.title='BOM'; thin=Side(style='thin'); border=Border(left=thin,right=thin,top=thin,bottom=thin); h=payload.get('header',{}); rows=payload.get('rows',[]); ws.page_setup.orientation='portrait'; ws.page_setup.paperSize=ws.PAPERSIZE_A4; ws.page_setup.fitToWidth=1; ws.page_setup.fitToHeight=1; ws.sheet_properties.pageSetUpPr.fitToPage=True; ws.page_margins.left=.58; ws.page_margins.right=.75; ws.page_margins.top=.60; ws.page_margins.bottom=.55; ws.sheet_view.showGridLines=False
 for col,width in {'A':12.43,'B':80.99,'C':18.43,'D':10.0,'E':11.87,'F':9.10,'G':5.87,'H':5.87}.items():ws.column_dimensions[col].width=width
 ws.merge_cells('A1:E2'); ws['A1']='-: Equipment List :-'; ws['A1'].font=Font(name='Courier New',size=10); ws['A1'].alignment=Alignment(horizontal='center',vertical='center'); ws['F1']=f'Doc.No.: {payload.get("document_no","SI EA/CS/FR/EG/015")}\nRev.No.: {payload.get("revision","1.0")}, Eff.Dt: {payload.get("effective_date","17/07/2026")}\nCreated By: {payload.get("created_by","EA CS ENGG")}'; ws['F1'].alignment=Alignment(horizontal='right',vertical='top',wrap_text=True); ws['F1'].font=Font(name='Arial',size=7); ws.merge_cells('F1:H2'); ws.row_dimensions[1].height=34; ws.row_dimensions[2].height=16
 for c,v in [('A4','EQPT.NO'),('B4','SPECIFICATION'),('C4','DESIGNATION'),('F4','TOTAL EQPT QTY.'),('G4','MPD'),('H4','AMD')]:ws[c]=v
 ws.merge_cells('A4:A6'); ws.merge_cells('B4:B6'); ws.merge_cells('C4:C6'); ws.merge_cells('D4:E4'); ws.merge_cells('F4:F6'); ws.merge_cells('G4:G6'); ws.merge_cells('H4:H6'); ws['D4']='FEEDER TYPICAL'; ws['D5']='QTY.'; ws['E5']=payload.get('feeder_name',''); ws['E6']=payload.get('feeder_qty','')
 for row in range(4,7):
  for col in range(1,9):ws.cell(row,col).border=border; ws.cell(row,col).alignment=Alignment(horizontal='center',vertical='center',wrap_text=True); ws.cell(row,col).font=Font(name='Courier New',size=10 if row==4 else 9,bold=True)
 ws.row_dimensions[4].height=23.25; ws.row_dimensions[5].height=18; ws.row_dimensions[6].height=18
 for i,row in enumerate(rows[:64],7):
  vals=[row.get('sr',''),row.get('specification',''),row.get('designation',''),' ',row.get('eqpt_qty') if row.get('eqpt_qty') is not None else '',row.get('total') if row.get('total') is not None else '','','']
  for col,val in enumerate(vals,1):ws.cell(i,col,val).border=border; ws.cell(i,col).font=Font(name='Courier New',size=7); ws.cell(i,col).alignment=Alignment(horizontal='center' if col!=2 else 'left',vertical='center',wrap_text=True if col==2 else False)
  ws.row_dimensions[i].height=max(15.6,min(180,15.6*max(1,len(row.get('specification_lines',[])))))
 for i in range(7+len(rows[:64]),70):
  for col in range(1,9):ws.cell(i,col).border=border; ws.cell(i,col).font=Font(name='Courier New',size=7)
  ws.row_dimensions[i].height=15.6
 fr=72; footer_left=[('Item No.','100'),('Client :',h.get('client','')),('Sales Ref No.:',h.get('sales_ref','')),('DATE :',datetime.now().strftime('%d.%m.%Y'))]; footer_mid=[('Description :',payload.get('description','')),('W.O. No.:',h.get('wo','')),('Drg. No.:',h.get('drawing',''))]; footer_right=[('PRE.BY :',h.get('prep_by','')),('Qty.:',payload.get('qty','')),('ESD No.:',h.get('esd','')),('','1 of 1')]
 for i,(label,value) in enumerate(footer_left):
  ws.cell(fr+i,1,label).font=Font(name='Arial',size=7); ws.cell(fr+i,2,value).font=Font(name='Arial',size=7)
  ws.cell(fr+i,1).alignment=Alignment(horizontal='left',vertical='center'); ws.cell(fr+i,2).alignment=Alignment(horizontal='left',vertical='center')
 for i,(label,value) in enumerate(footer_mid):
  ws.cell(fr+i,4,label).font=Font(name='Arial',size=7); ws.cell(fr+i,5,value).font=Font(name='Arial',size=7)
  ws.cell(fr+i,4).alignment=Alignment(horizontal='left',vertical='center'); ws.cell(fr+i,5).alignment=Alignment(horizontal='left',vertical='center')
 for i,(label,value) in enumerate(footer_right):
  ws.cell(fr+i,7,label).font=Font(name='Arial',size=7); ws.cell(fr+i,8,value).font=Font(name='Arial',size=7)
  ws.cell(fr+i,7).alignment=Alignment(horizontal='right',vertical='center'); ws.cell(fr+i,8).alignment=Alignment(horizontal='right',vertical='center')
 ws.print_area='A1:H76'; return wb
@app.post('/api/bom/export')
async def export(payload:dict, user:str=Depends(current_user)):
 wb=export_book(payload);out=io.BytesIO();wb.save(out);out.seek(0);return StreamingResponse(out,media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',headers={'Content-Disposition':'attachment; filename=ELEXORA_BOM.xlsx'})

class NumberedCanvas:
    def __init__(self, *args, **kwargs):
        from reportlab.pdfgen import canvas as pdfcanvas
        self._canvas = pdfcanvas.Canvas(*args, **kwargs)
        self._saved_page_states = []

    def __getattr__(self, name):
        return getattr(self._canvas, name)

    def showPage(self):
        self._saved_page_states.append(dict(self._canvas.__dict__))
        self._canvas._startPage()

    def save(self):
        total = len(self._saved_page_states)
        for state in self._saved_page_states:
            self._canvas.__dict__.update(state)
            self._draw_page_number(total)
            self._canvas.showPage()
        self._canvas.save()

    def _draw_page_number(self, total):
        # Page number occupies its own fourth line in the right footer block.
        w, hp = A4
        self._canvas.setFont('Helvetica', 6.5)
        self._canvas.drawRightString(
            w - 8*mm, 4*mm,
            f'{self._canvas.getPageNumber()} of {total}'
        )


def export_pdf(payload):
    if payload.get('feeders'):
        return export_combined_pdf(payload)
    out = io.BytesIO()
    doc = SimpleDocTemplate(
        out, pagesize=A4,
        rightMargin=8*mm, leftMargin=8*mm,
        topMargin=12*mm, bottomMargin=28*mm
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('bomtitle', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=9, leading=11, alignment=TA_CENTER)
    meta_style = ParagraphStyle('bommeta', parent=styles['Normal'], fontName='Helvetica', fontSize=6.5, leading=8, alignment=TA_RIGHT)
    cell_style = ParagraphStyle('cell', parent=styles['Normal'], fontName='Helvetica', fontSize=6.3, leading=7.5, spaceAfter=0, spaceBefore=0)
    cell_center = ParagraphStyle('cellcenter', parent=cell_style, alignment=TA_CENTER)
    head_style = ParagraphStyle('head', parent=cell_style, fontName='Helvetica-Bold', alignment=TA_CENTER, leading=8)
    h = payload.get('header', {})
    rows = payload.get('rows', [])
    story = [
        Paragraph('-: Equipment List :-', title_style),
        Spacer(1, 4*mm),
    ]
    data = [
        [Paragraph('EQPT.NO',head_style), Paragraph('SPECIFICATION',head_style), Paragraph('DESIGNATION',head_style),
         Paragraph('FEEDER TYPICAL',head_style), '', Paragraph('TOTAL<br/>EQPT<br/>QTY.',head_style), Paragraph('MPD',head_style), Paragraph('AMD',head_style)],
        ['', '', '', Paragraph('QTY.',head_style), Paragraph(str(payload.get('feeder_name','')),head_style), '', '', ''],
        ['', '', '', '', Paragraph(str(payload.get('feeder_qty','')),cell_center), '', '', '']
    ]
    for row in rows[:64]:
        spec = row.get('specification','') or ''
        data.append([
            Paragraph(str(row.get('sr','') or ''), cell_center),
            Paragraph(str(spec).replace('&','&amp;').replace('<','&lt;').replace('>','&gt;').replace('\n','<br/>'), cell_style),
            Paragraph(str(row.get('designation','') or ''), cell_center),
            '',
            Paragraph(str(row.get('eqpt_qty','') or ''), cell_center),
            Paragraph(str(row.get('total','') or ''), cell_center),
            '', ''
        ])
    col_widths=[16*mm, 81*mm, 24*mm, 17*mm, 20*mm, 18*mm, 9*mm, 9*mm]
    tbl=Table(data,colWidths=col_widths,repeatRows=3)
    tbl.setStyle(TableStyle([
        ('SPAN',(0,0),(0,2)),('SPAN',(1,0),(1,2)),('SPAN',(2,0),(2,2)),
        ('SPAN',(3,0),(4,0)),('SPAN',(5,0),(5,2)),('SPAN',(6,0),(6,2)),('SPAN',(7,0),(7,2)),
        ('GRID',(0,0),(-1,-1),0.45,colors.black),
        ('BACKGROUND',(0,0),(-1,2),colors.HexColor('#d9e7f0')),
        ('VALIGN',(0,0),(-1,-1),'MIDDLE'),
        ('ALIGN',(0,0),(-1,2),'CENTER'),
        ('LEFTPADDING',(0,0),(-1,-1),2),('RIGHTPADDING',(0,0),(-1,-1),2),
        ('TOPPADDING',(0,0),(-1,-1),2),('BOTTOMPADDING',(0,0),(-1,-1),2),
    ]))
    story.append(tbl)

    def footer(canvas, doc_obj):
        canvas.saveState()
        w, hp = A4
        y = 11*mm
        canvas.setFont('Helvetica', 6.5)

        # Fixed footer zones matching the supplied BOM: left / center / right.
        left_x = 8*mm
        mid_x = 82*mm
        right_x = 164*mm
        line_step = 3*mm

        left=[
            ('Item No.','100'),
            ('Client :',h.get('client','')),
            ('Sales Ref No.:',h.get('sales_ref','')),
            ('DATE :',datetime.now().strftime('%d.%m.%Y'))
        ]
        mid=[
            ('Description :',payload.get('description','')),
            ('W.O. No.:',h.get('wo','')),
            ('Drg. No.:',h.get('drawing',''))
        ]
        right=[
            ('PRE.BY :',h.get('prep_by','')),
            ('Qty.:',payload.get('qty','')),
            ('ESD No.:',h.get('esd',''))
        ]

        for i,(label,value) in enumerate(left):
            canvas.drawString(left_x, y+(2.5-i)*line_step, (label+' '+str(value)).strip())

        # Keep the center footer inside its own zone so it cannot overlap the right block.
        from reportlab.pdfbase.pdfmetrics import stringWidth
        mid_max_x = right_x - 5*mm
        for i,(label,value) in enumerate(mid):
            text_value = (label+' '+str(value)).strip()
            max_width = mid_max_x - mid_x
            while stringWidth(text_value, 'Helvetica', 6.5) > max_width and len(text_value) > len(label) + 4:
                text_value = text_value[:-2].rstrip() + '...'
            canvas.drawString(mid_x, y+(2.5-i)*line_step, text_value)

        for i,(label,value) in enumerate(right):
            canvas.drawString(right_x, y+(2.5-i)*line_step, (label+' '+str(value)).strip())

        canvas.restoreState()
    doc.build(story,onFirstPage=footer,onLaterPages=footer,canvasmaker=NumberedCanvas)
    out.seek(0)
    return out

@app.post('/api/bom/export-pdf')
async def export_pdf_endpoint(payload:dict, user:str=Depends(current_user)):
    out=export_pdf(payload)
    return StreamingResponse(out,media_type='application/pdf',headers={'Content-Disposition':f'attachment; filename=BOM_{re.sub(r"[^A-Za-z0-9._-]+", "_", str(payload.get("header", {}).get("wo", "WO"))).strip("_")}.pdf'})
@app.post('/api/bom/preview-multiple')
async def preview_multiple(
    client: str = Form(''), sales_ref: str = Form(''), drawing: str = Form(''),
    esd: str = Form(''), wo: str = Form(''), prep_by: str = Form(''),
    voltage: str = Form(''), mslds: list[UploadFile] = File(...),
    dis: UploadFile = File(...), user: str = Depends(current_user)
):
    if not mslds:
        raise HTTPException(status_code=400, detail='Upload at least one MSLD PDF.')
    dis_name = dis.filename or 'DIS.pdf'
    dis_bytes = await dis.read()
    items = []
    filenames = []
    for upload in mslds:
        filename = upload.filename or 'MSLD.pdf'
        items.append((await upload.read(), filename))
        filenames.append(filename)
    result = extract_multiple_from_files(items, dis_bytes, dis_name, client, sales_ref, drawing, esd, wo, prep_by, voltage)
    import json
    with db() as conn:
        conn.execute(
            """INSERT INTO bom_history (username, customer_name, work_order_no, voltage, msld_files, dis_filename, row_count)
               VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)""",
            (user, str(client or result.get('header', {}).get('client', '')), str(wo or result.get('header', {}).get('wo', '')),
             str(voltage or ''), json.dumps(filenames), dis_name, len(result.get('rows') or []))
        )
        conn.commit()
    return result




class SiemensLookupRequest(BaseModel):
    mlfb: str


@app.post('/api/siemens/product-lookup')
def siemens_product_lookup(payload: SiemensLookupRequest, user: str = Depends(current_user)):
    """POC: live lookup of a supplied 7SJ66 MLFB on Siemens Industry Mall."""
    return lookup_siemens_product(payload.mlfb)

@app.get('/api/admin/bom-history')
def admin_bom_history(user: str = Depends(require_admin)):
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) AS total FROM bom_history").fetchone()['total']
        rows = conn.execute(
            """SELECT id, username, customer_name, work_order_no, voltage, msld_files,
                      dis_filename, row_count, created_at
               FROM bom_history ORDER BY created_at DESC LIMIT 500"""
        ).fetchall()
    return {'total_generated': total, 'history': rows}

def export_combined_book(payload):
    wb = Workbook()
    ws = wb.active
    ws.title = 'BOM'
    thin = Side(style='thin')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    header_data = payload.get('header', {})
    feeders = payload.get('feeders') or []
    rows = payload.get('rows') or []
    n = max(1, len(feeders))
    qty_col = 4
    feeder_start_col = 5
    total_col = feeder_start_col + n
    mpd_col, amd_col = total_col + 1, total_col + 2
    last_col = amd_col
    ws.page_setup.orientation = 'landscape'
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 1
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_margins.left = .25
    ws.page_margins.right = .25
    ws.page_margins.top = .45
    ws.page_margins.bottom = .4
    ws.sheet_view.showGridLines = False
    spec_width = max(36, min(78, max([max((len(line) for line in str(r.get('specification','')).splitlines()), default=0) for r in rows] + [36]) * 0.75))
    designation_width = max(14, min(26, max([len(str(r.get('designation',''))) for r in rows] + [14]) * 0.9))
    feeder_width = max(10, min(18, max([len(str(f.get('name',''))) for f in feeders] + [8]) * 0.9))
    widths = [9, spec_width, designation_width, 9] + [feeder_width] * n + [13, 7, 7]
    for col, width in enumerate(widths, 1):
        ws.column_dimensions[__import__('openpyxl').utils.get_column_letter(col)].width = width
    ws.merge_cells(start_row=1, start_column=1, end_row=2, end_column=min(3, last_col))
    ws.cell(1, 1, '-: Equipment List :-')
    ws.cell(1, 1).font = Font(name='Courier New', size=10)
    ws.cell(1, 1).alignment = Alignment(horizontal='center', vertical='center')
    meta_col = max(4, last_col - 2)
    ws.merge_cells(start_row=1, start_column=meta_col, end_row=2, end_column=last_col)
    ws.cell(1, meta_col, f'Doc.No.: {payload.get("document_no","SI EA/CS/FR/EG/015")}\nRev.No.: {payload.get("revision","1.0")}, Eff.Dt: {payload.get("effective_date","17/07/2026")}\nCreated By: {payload.get("created_by","EA CS ENGG")}')
    ws.cell(1, meta_col).font = Font(name='Arial', size=7)
    ws.cell(1, meta_col).alignment = Alignment(horizontal='right', vertical='top', wrap_text=True)
    ws.row_dimensions[1].height = 34
    ws.row_dimensions[2].height = 16
    for col, label in [(1,'EQPT.NO'),(2,'SPECIFICATION'),(3,'DESIGNATION'),(total_col,'TOTAL EQPT QTY.'),(mpd_col,'MPD'),(amd_col,'AMD')]:
        ws.cell(4, col, label)
    for col in [1,2,3,total_col,mpd_col,amd_col]:
        ws.merge_cells(start_row=4, start_column=col, end_row=6, end_column=col)
    ws.merge_cells(start_row=5, start_column=qty_col, end_row=6, end_column=qty_col)
    ws.cell(5,qty_col,'QTY.')
    ws.merge_cells(start_row=4, start_column=feeder_start_col, end_row=4, end_column=feeder_start_col+n-1)
    ws.cell(4,feeder_start_col,'FEEDER TYPICAL')
    for idx, feeder in enumerate(feeders):
        col=feeder_start_col+idx
        ws.cell(5,col,feeder.get('name',''))
        ws.cell(6,col,feeder.get('quantity') or feeder.get('qty') or 1)
    for r in range(4,7):
        for c in range(1,last_col+1):
            cell=ws.cell(r,c)
            cell.border=border
            cell.alignment=Alignment(horizontal='center',vertical='center',wrap_text=True)
            cell.font=Font(name='Courier New',size=8,bold=True)
    ws.row_dimensions[4].height=23
    ws.row_dimensions[5].height=18
    ws.row_dimensions[6].height=28
    for i,row in enumerate(rows[:64],7):
        quantities = row.get('feeder_quantities') or []
        vals=[row.get('sr',''),row.get('specification',''),row.get('designation','')]
        vals.append('')
        vals.extend(quantities[j] if j < len(quantities) and quantities[j] not in (0, '0', None, '') else '' for j in range(n))
        total_value = row.get('total', '')
        vals.extend(['' if total_value in (0, '0') else total_value, '', ''])
        for col,val in enumerate(vals,1):
            cell=ws.cell(i,col,val)
            cell.border=border
            cell.font=Font(name='Courier New',size=7)
            cell.alignment=Alignment(horizontal='center' if col!=2 else 'left',vertical='center',wrap_text=col==2)
        spec_lines = str(row.get('specification', '')).splitlines() or ['']
        chars_per_line = max(12, int(spec_width * 1.35))
        wrapped_lines = sum(max(1, (len(line) + chars_per_line - 1) // chars_per_line) for line in spec_lines)
        ws.row_dimensions[i].height = max(18, min(240, 10 * wrapped_lines + 6))
    for i in range(7+len(rows[:64]),70):
        for c in range(1,last_col+1):
            ws.cell(i,c).border=border
            ws.cell(i,c).font=Font(name='Courier New',size=7)
        ws.row_dimensions[i].height=15
    # Keep worksheet view focused on the equipment list. Document metadata is
    # printed in the page header/footer when the user presses Ctrl+P.
    ws.oddHeader.center.text = "&B Equipment List &B"
    ws.oddHeader.right.text = f"Doc.No.: {payload.get('document_no','SI EA/CS/FR/EG/015')}\\nRev.No.: {payload.get('revision','1.0')}  Eff.Dt: {payload.get('effective_date','17/07/2026')}"
    ws.oddHeader.right.size = 7
    ws.oddFooter.left.text = f"Client: {header_data.get('client','')}  |  Sales Ref: {header_data.get('sales_ref','')}\\nDescription: {payload.get('description','')}  |  WO: {header_data.get('wo','')}  |  Drawing: {header_data.get('drawing','')}"
    ws.oddFooter.center.text = f"Prepared by: {header_data.get('prep_by','')}  |  Qty: {payload.get('qty','')}  |  ESD: {header_data.get('esd','')}"
    ws.oddFooter.right.text = "Page &P of &N"
    ws.oddFooter.left.size = 7
    ws.oddFooter.center.size = 7
    ws.oddFooter.right.size = 7
    ws.print_area=f'A1:{__import__("openpyxl").utils.get_column_letter(last_col)}76'
    return wb

def export_combined_pdf(payload):
    out=io.BytesIO()
    doc=SimpleDocTemplate(out,pagesize=A4,rightMargin=5*mm,leftMargin=5*mm,topMargin=27*mm,bottomMargin=20*mm)
    styles=getSampleStyleSheet()
    title=ParagraphStyle('combinedtitle',parent=styles['Normal'],fontName='Helvetica-Bold',fontSize=6,leading=7,alignment=TA_CENTER)
    cell=ParagraphStyle('combinedcell',parent=styles['Normal'],fontName='Helvetica',fontSize=4.2,leading=4.8,spaceAfter=0,spaceBefore=0,wordWrap='CJK')
    center=ParagraphStyle('combinedcenter',parent=cell,alignment=TA_CENTER)
    head=ParagraphStyle('combinedhead',parent=cell,fontName='Helvetica-Bold',alignment=TA_CENTER,fontSize=4.0,leading=4.5)
    feeders=payload.get('feeders') or []
    rows=payload.get('rows') or []
    n=max(1,len(feeders))
    data=[[Paragraph('EQPT.NO',head),Paragraph('SPECIFICATION',head),Paragraph('DESIGNATION',head),''] + [Paragraph('FEEDER<br/>TYPICAL',head) if i==0 else '' for i in range(n)] + [Paragraph('TOTAL<br/>EQPT QTY.',head),Paragraph('MPD',head),Paragraph('AMD',head)]]
    data.append(['','','',Paragraph('QTY.',head)]+[Paragraph(str(f.get('name','')),head) for f in feeders]+['','',''])
    data.append(['','','','']+[Paragraph(str(f.get('quantity') or f.get('qty') or 1),center) for f in feeders]+['','',''])
    for row in rows[:64]:
        spec=str(row.get('specification','') or '').replace('&','&amp;').replace('<','&lt;').replace('>','&gt;').replace('\n','<br/>')
        qs=row.get('feeder_quantities') or []
        data.append([Paragraph(str(row.get('sr','')),center),Paragraph(spec,cell),Paragraph(str(row.get('designation','') or ''),center),Paragraph('',center)]+[Paragraph(str(qs[i] if i<len(qs) and qs[i] not in (0, '0', None, '') else ''),center) for i in range(n)]+[Paragraph('' if row.get('total') in (0, '0') else str(row.get('total') or ''),center),'',''])
    available=200*mm
    # Keep each typical-feeder column compact. For one feeder it is 25 mm;
    # with multiple feeders the combined feeder area is capped at 60 mm.
    feeder_total=min(available-74*mm, max(25*mm, 60*mm if n>2 else 25*mm*n))
    feeder_labels=[len(str(f.get('name',''))) for f in feeders]
    weights=[max(1.0,min(2.0,length/10.0)) for length in feeder_labels]
    weight_sum=sum(weights) or n
    feeder_widths=[feeder_total*w/weight_sum for w in weights] if feeders else [feeder_total]
    spec_width=available-74*mm-feeder_total
    widths=[8*mm,spec_width,25*mm,9*mm]+feeder_widths+[12*mm,10*mm,10*mm]
    tbl=Table(data,colWidths=widths,rowHeights=[18,14,20]+[None]*(len(data)-3),repeatRows=3)
    spans=[('SPAN',(0,0),(0,2)),('SPAN',(1,0),(1,2)),('SPAN',(2,0),(2,2)),('SPAN',(3,1),(3,2)),('SPAN',(4,0),(3+n,0)),('SPAN',(4+n,0),(4+n,2)),('SPAN',(5+n,0),(5+n,2)),('SPAN',(6+n,0),(6+n,2))]
    # A compact first-row group label and feeder-specific second/third header rows.
    tbl.setStyle(TableStyle(spans+[('GRID',(0,0),(-1,-1),0.45,colors.black),('BACKGROUND',(0,0),(-1,2),colors.HexColor('#d9e7f0')),('VALIGN',(0,0),(-1,-1),'MIDDLE'),('ALIGN',(0,0),(-1,2),'CENTER'),('LEFTPADDING',(0,0),(-1,-1),2),('RIGHTPADDING',(0,0),(-1,-1),2),('TOPPADDING',(0,0),(-1,-1),1),('BOTTOMPADDING',(0,0),(-1,-1),1)]))
    story=[Spacer(1,8*mm),tbl]
    h=payload.get('header',{})
    def footer(canvas, doc_obj):
        canvas.saveState()
        w,hp=doc_obj.pagesize
        canvas.setFont('Helvetica',6.5)
        canvas.setFont('Helvetica-Bold',9)
        canvas.drawCentredString(w/2,hp-9*mm,'-: Equipment List :-')
        canvas.setFont('Helvetica',6.5)
        right_edge=w-19*mm
        line_step=3*mm

        # Header metadata follows the reference BOM's top-right alignment.
        header_lines=[
            f"Doc.No.: {payload.get('document_no','SI EA/CS/FR/EG/015')}",
            f"Rev.No.: {payload.get('revision','1.0')}, Eff.Dt: {payload.get('effective_date','17/07/2026')}",
            f"Created By: {payload.get('created_by','EA CS ENGG')}"
        ]
        for i,text_value in enumerate(header_lines):
            canvas.drawRightString(right_edge,hp-14*mm-i*4*mm,text_value)

        # Footer values occupy the same left / center / right zones as the reference.
        y=7*mm
        left=[f"Client : {h.get('client','')}",f"Sales Ref No.: {h.get('sales_ref','')}"]
        mid=[f"Description : {payload.get('description','')}",f"W.O. No.: {h.get('wo','')}",f"Drg. No.: {h.get('drawing','')}"]
        right=[f"PRE.BY : {h.get('prep_by','')}",f"Qty.: {payload.get('qty','')}",f"ESD No.: {h.get('esd','')}"]
        left_x=19*mm
        center_x=w/2
        right_x=150*mm
        center_max_width=68*mm
        from reportlab.pdfbase.pdfmetrics import stringWidth
        for i,text_value in enumerate(left):
            canvas.drawString(left_x,y+(1.5-i)*line_step,text_value)
        for i,text_value in enumerate(mid):
            while stringWidth(text_value,'Helvetica',6.5)>center_max_width and len(text_value)>12:
                text_value=text_value[:-4].rstrip()+'...'
            canvas.drawCentredString(center_x,y+(1.5-i)*line_step,text_value)
        for i,text_value in enumerate(right):
            canvas.drawRightString(right_edge,y+(1.5-i)*line_step,text_value)
        canvas.restoreState()
    doc.build(story,onFirstPage=footer,onLaterPages=footer,canvasmaker=NumberedCanvas)
    out.seek(0)
    return out

@app.get('/')
def root():
    return {
        'service': 'ELEXORA 2 API',
        'status': 'running',
        'health': '/health',
        'docs': '/docs',
    }


@app.get('/health')
def health():
    return {'status':'ok'}

@app.on_event("startup")
def startup():
    init_auth_db()

@app.post('/api/auth/signup')
def signup(payload: AuthRequest):
    username = payload.username.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username):
        raise HTTPException(status_code=400, detail="Username must be 3-32 characters and use only letters, numbers, dot, underscore or hyphen.")
    errors = validate_password(payload.password)
    if errors:
        raise HTTPException(status_code=400, detail="Password must contain " + ", ".join(errors) + ".")
    try:
        with db() as conn:
            conn.execute(
                "INSERT INTO users(username,password_hash) VALUES(%s,%s)",
                (username.lower(), password_hash(payload.password))
            )
            conn.commit()
    except Exception as exc:
        if getattr(exc, "sqlstate", None) == "23505":
            raise HTTPException(status_code=409, detail="That username already exists. Please choose another.")
        raise
    return {"message":"Account created successfully. You can now sign in."}

@app.post('/api/auth/login')
def login(payload: AuthRequest):
    username = payload.username.strip().lower()
    with db() as conn:
        row = conn.execute(
            "SELECT id,password_hash,username FROM users WHERE username=%s",
            (username,)
        ).fetchone()
        if not row or not password_ok(payload.password, row["password_hash"]):
            raise HTTPException(status_code=401, detail="Invalid username or password.")
        token = secrets.token_urlsafe(32)
        expires = int(datetime.now().timestamp()) + TOKEN_TTL_SECONDS
        conn.execute(
            "INSERT INTO sessions(token,user_id,expires_at) VALUES(%s,%s,%s)",
            (token, row["id"], expires)
        )
        conn.commit()
    return {"token":token,"username":row["username"],"expires_at":expires}

@app.post('/api/auth/logout')
def logout(authorization: str = Header(default="")):
    token = authorization[7:].strip() if authorization.startswith("Bearer ") else ""
    if token:
        with db() as conn:
            conn.execute("DELETE FROM sessions WHERE token=%s", (token,))
            conn.commit()
    return {"message":"Signed out."}

@app.post('/api/bom/preview')
async def preview(client:str=Form(''),sales_ref:str=Form(''),drawing:str=Form(''),esd:str=Form(''),wo:str=Form(''),prep_by:str=Form(''),voltage:str=Form(''),msld:UploadFile=File(...),dis:UploadFile=File(...),user:str=Depends(current_user)):
 m,d=await msld.read(),await dis.read(); return extract_from_files(m,msld.filename,d,dis.filename,client,sales_ref,drawing,esd,wo,prep_by,voltage)

def export_book(payload):
 if payload.get('feeders'):
  return export_combined_book(payload)
 wb=Workbook(); ws=wb.active; ws.title='BOM'; thin=Side(style='thin'); border=Border(left=thin,right=thin,top=thin,bottom=thin); h=payload.get('header',{}); rows=payload.get('rows',[]); ws.page_setup.orientation='portrait'; ws.page_setup.paperSize=ws.PAPERSIZE_A4; ws.page_setup.fitToWidth=1; ws.page_setup.fitToHeight=1; ws.sheet_properties.pageSetUpPr.fitToPage=True; ws.page_margins.left=.58; ws.page_margins.right=.75; ws.page_margins.top=.60; ws.page_margins.bottom=.55; ws.sheet_view.showGridLines=False
 for col,width in {'A':12.43,'B':80.99,'C':18.43,'D':10.0,'E':11.87,'F':9.10,'G':5.87,'H':5.87}.items():ws.column_dimensions[col].width=width
 ws.merge_cells('A1:E2'); ws['A1']='-: Equipment List :-'; ws['A1'].font=Font(name='Courier New',size=10); ws['A1'].alignment=Alignment(horizontal='center',vertical='center'); ws['F1']=f'Doc.No.: {payload.get("document_no","SI EA/CS/FR/EG/015")}\nRev.No.: {payload.get("revision","1.0")}, Eff.Dt: {payload.get("effective_date","17/07/2026")}\nCreated By: {payload.get("created_by","EA CS ENGG")}'; ws['F1'].alignment=Alignment(horizontal='right',vertical='top',wrap_text=True); ws['F1'].font=Font(name='Arial',size=7); ws.merge_cells('F1:H2'); ws.row_dimensions[1].height=34; ws.row_dimensions[2].height=16
 for c,v in [('A4','EQPT.NO'),('B4','SPECIFICATION'),('C4','DESIGNATION'),('F4','TOTAL EQPT QTY.'),('G4','MPD'),('H4','AMD')]:ws[c]=v
 ws.merge_cells('A4:A6'); ws.merge_cells('B4:B6'); ws.merge_cells('C4:C6'); ws.merge_cells('D4:E4'); ws.merge_cells('F4:F6'); ws.merge_cells('G4:G6'); ws.merge_cells('H4:H6'); ws['D4']='FEEDER TYPICAL'; ws['D5']='QTY.'; ws['E5']=payload.get('feeder_name',''); ws['E6']=payload.get('feeder_qty','')
 for row in range(4,7):
  for col in range(1,9):ws.cell(row,col).border=border; ws.cell(row,col).alignment=Alignment(horizontal='center',vertical='center',wrap_text=True); ws.cell(row,col).font=Font(name='Courier New',size=10 if row==4 else 9,bold=True)
 ws.row_dimensions[4].height=23.25; ws.row_dimensions[5].height=18; ws.row_dimensions[6].height=18
 for i,row in enumerate(rows[:64],7):
  vals=[row.get('sr',''),row.get('specification',''),row.get('designation',''),' ',row.get('eqpt_qty') if row.get('eqpt_qty') is not None else '',row.get('total') if row.get('total') is not None else '','','']
  for col,val in enumerate(vals,1):ws.cell(i,col,val).border=border; ws.cell(i,col).font=Font(name='Courier New',size=7); ws.cell(i,col).alignment=Alignment(horizontal='center' if col!=2 else 'left',vertical='center',wrap_text=True if col==2 else False)
  ws.row_dimensions[i].height=max(15.6,min(180,15.6*max(1,len(row.get('specification_lines',[])))))
 for i in range(7+len(rows[:64]),70):
  for col in range(1,9):ws.cell(i,col).border=border; ws.cell(i,col).font=Font(name='Courier New',size=7)
  ws.row_dimensions[i].height=15.6
 fr=72; footer_left=[('Item No.','100'),('Client :',h.get('client','')),('Sales Ref No.:',h.get('sales_ref','')),('DATE :',datetime.now().strftime('%d.%m.%Y'))]; footer_mid=[('Description :',payload.get('description','')),('W.O. No.:',h.get('wo','')),('Drg. No.:',h.get('drawing',''))]; footer_right=[('PRE.BY :',h.get('prep_by','')),('Qty.:',payload.get('qty','')),('ESD No.:',h.get('esd','')),('','1 of 1')]
 for i,(label,value) in enumerate(footer_left):
  ws.cell(fr+i,1,label).font=Font(name='Arial',size=7); ws.cell(fr+i,2,value).font=Font(name='Arial',size=7)
  ws.cell(fr+i,1).alignment=Alignment(horizontal='left',vertical='center'); ws.cell(fr+i,2).alignment=Alignment(horizontal='left',vertical='center')
 for i,(label,value) in enumerate(footer_mid):
  ws.cell(fr+i,4,label).font=Font(name='Arial',size=7); ws.cell(fr+i,5,value).font=Font(name='Arial',size=7)
  ws.cell(fr+i,4).alignment=Alignment(horizontal='left',vertical='center'); ws.cell(fr+i,5).alignment=Alignment(horizontal='left',vertical='center')
 for i,(label,value) in enumerate(footer_right):
  ws.cell(fr+i,7,label).font=Font(name='Arial',size=7); ws.cell(fr+i,8,value).font=Font(name='Arial',size=7)
  ws.cell(fr+i,7).alignment=Alignment(horizontal='right',vertical='center'); ws.cell(fr+i,8).alignment=Alignment(horizontal='right',vertical='center')
 ws.print_area='A1:H76'; return wb
@app.post('/api/bom/export')
async def export(payload:dict, user:str=Depends(current_user)):
 wb=export_book(payload);out=io.BytesIO();wb.save(out);out.seek(0);return StreamingResponse(out,media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',headers={'Content-Disposition':'attachment; filename=ELEXORA_BOM.xlsx'})

class NumberedCanvas:
    def __init__(self, *args, **kwargs):
        from reportlab.pdfgen import canvas as pdfcanvas
        self._canvas = pdfcanvas.Canvas(*args, **kwargs)
        self._saved_page_states = []

    def __getattr__(self, name):
        return getattr(self._canvas, name)

    def showPage(self):
        self._saved_page_states.append(dict(self._canvas.__dict__))
        self._canvas._startPage()

    def save(self):
        total = len(self._saved_page_states)
        for state in self._saved_page_states:
            self._canvas.__dict__.update(state)
            self._draw_page_number(total)
            self._canvas.showPage()
        self._canvas.save()

    def _draw_page_number(self, total):
        # Page number occupies its own fourth line in the right footer block.
        w, hp = A4
        self._canvas.setFont('Helvetica', 6.5)
        self._canvas.drawRightString(
            w - 19*mm, 2.5*mm,
            f'{self._canvas.getPageNumber()} of {total}'
        )


def export_pdf(payload):
    if payload.get('feeders'):
        return export_combined_pdf(payload)
    out = io.BytesIO()
    doc = SimpleDocTemplate(
        out, pagesize=A4,
        rightMargin=8*mm, leftMargin=8*mm,
        topMargin=12*mm, bottomMargin=28*mm
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('bomtitle', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=9, leading=11, alignment=TA_CENTER)
    meta_style = ParagraphStyle('bommeta', parent=styles['Normal'], fontName='Helvetica', fontSize=6.5, leading=8, alignment=TA_RIGHT)
    cell_style = ParagraphStyle('cell', parent=styles['Normal'], fontName='Helvetica', fontSize=6.3, leading=7.5, spaceAfter=0, spaceBefore=0)
    cell_center = ParagraphStyle('cellcenter', parent=cell_style, alignment=TA_CENTER)
    head_style = ParagraphStyle('head', parent=cell_style, fontName='Helvetica-Bold', alignment=TA_CENTER, leading=8)
    h = payload.get('header', {})
    rows = payload.get('rows', [])
    story = [
        Spacer(1, 8*mm),
    ]
    data = [
        [Paragraph('EQPT.NO',head_style), Paragraph('SPECIFICATION',head_style), Paragraph('DESIGNATION',head_style),
         Paragraph('FEEDER TYPICAL',head_style), '', Paragraph('TOTAL<br/>EQPT<br/>QTY.',head_style), Paragraph('MPD',head_style), Paragraph('AMD',head_style)],
        ['', '', '', Paragraph('QTY.',head_style), Paragraph(str(payload.get('feeder_name','')),head_style), '', '', ''],
        ['', '', '', '', Paragraph(str(payload.get('feeder_qty','')),cell_center), '', '', '']
    ]
    for row in rows[:64]:
        spec = row.get('specification','') or ''
        data.append([
            Paragraph(str(row.get('sr','') or ''), cell_center),
            Paragraph(str(spec).replace('&','&amp;').replace('<','&lt;').replace('>','&gt;').replace('\n','<br/>'), cell_style),
            Paragraph(str(row.get('designation','') or ''), cell_center),
            '',
            Paragraph(str(row.get('eqpt_qty','') or ''), cell_center),
            Paragraph(str(row.get('total','') or ''), cell_center),
            '', ''
        ])
    col_widths=[16*mm, 81*mm, 24*mm, 17*mm, 20*mm, 18*mm, 9*mm, 9*mm]
    tbl=Table(data,colWidths=col_widths,repeatRows=3)
    tbl.setStyle(TableStyle([
        ('SPAN',(0,0),(0,2)),('SPAN',(1,0),(1,2)),('SPAN',(2,0),(2,2)),
        ('SPAN',(3,0),(4,0)),('SPAN',(5,0),(5,2)),('SPAN',(6,0),(6,2)),('SPAN',(7,0),(7,2)),
        ('GRID',(0,0),(-1,-1),0.45,colors.black),
        ('BACKGROUND',(0,0),(-1,2),colors.HexColor('#d9e7f0')),
        ('VALIGN',(0,0),(-1,-1),'MIDDLE'),
        ('ALIGN',(0,0),(-1,2),'CENTER'),
        ('LEFTPADDING',(0,0),(-1,-1),2),('RIGHTPADDING',(0,0),(-1,-1),2),
        ('TOPPADDING',(0,0),(-1,-1),2),('BOTTOMPADDING',(0,0),(-1,-1),2),
    ]))
    story.append(tbl)

    def footer(canvas, doc_obj):
        canvas.saveState()
        w, hp = A4
        y = 7*mm
        canvas.setFont('Helvetica-Bold', 9)
        canvas.drawCentredString(w/2, hp-9*mm, '-: Equipment List :-')
        canvas.setFont('Helvetica', 6.5)

        # Header metadata and footer zones follow the supplied A4 BOM layout.
        right_edge = w - 19*mm
        header_lines = [
            f"Doc.No.: {payload.get('document_no','SI EA/CS/FR/EG/015')}",
            f"Rev.No.: {payload.get('revision','1.0')}, Eff.Dt: {payload.get('effective_date','17/07/2026')}",
            f"Created By: {payload.get('created_by','EA CS ENGG')}"
        ]
        for i, text_value in enumerate(header_lines):
            canvas.drawRightString(right_edge, hp - 14*mm - i*4*mm, text_value)

        left_x = 19*mm
        center_x = w/2
        center_max_width = 68*mm
        line_step = 3*mm

        left=[
            ('Item No.','100'),
            ('Client :',h.get('client','')),
            ('Sales Ref No.:',h.get('sales_ref','')),
            ('DATE :',datetime.now().strftime('%d.%m.%Y'))
        ]
        mid=[
            ('Description :',payload.get('description','')),
            ('W.O. No.:',h.get('wo','')),
            ('Drg. No.:',h.get('drawing',''))
        ]
        right=[
            ('PRE.BY :',h.get('prep_by','')),
            ('Qty.:',payload.get('qty','')),
            ('ESD No.:',h.get('esd',''))
        ]

        for i,(label,value) in enumerate(left):
            canvas.drawString(left_x, y+(2.5-i)*line_step, (label+' '+str(value)).strip())

        # Center the middle group on the page, matching the supplied reference.
        # A fixed width keeps long values clear of the left and right footer groups.
        from reportlab.pdfbase.pdfmetrics import stringWidth
        for i,(label,value) in enumerate(mid):
            text_value = (label+' '+str(value)).strip()
            while stringWidth(text_value, 'Helvetica', 6.5) > center_max_width and len(text_value) > len(label) + 4:
                text_value = text_value[:-2].rstrip() + '...'
            canvas.drawCentredString(center_x, y+(1.5-i)*line_step, text_value)

        for i,(label,value) in enumerate(right):
            canvas.drawRightString(right_edge, y+(1.5-i)*line_step, (label+' '+str(value)).strip())

        canvas.restoreState()
    doc.build(story,onFirstPage=footer,onLaterPages=footer,canvasmaker=NumberedCanvas)
    out.seek(0)
    return out

@app.post('/api/bom/export-pdf')
async def export_pdf_endpoint(payload:dict, user:str=Depends(current_user)):
    out=export_pdf(payload)
    return StreamingResponse(out,media_type='application/pdf',headers={'Content-Disposition':f'attachment; filename=BOM_{re.sub(r"[^A-Za-z0-9._-]+", "_", str(payload.get("header", {}).get("wo", "WO"))).strip("_")}.pdf'})
