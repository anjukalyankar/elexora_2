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
        conn.execute("""CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            expires_at BIGINT NOT NULL
        )""")
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


app = FastAPI(title='ELEXORA 2 API', version='0.7.0')
app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_credentials=True, allow_methods=['*'], allow_headers=['*'])
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

def extract_led_attributes(description, details, designation):
    text = clean_value(' '.join([description or '', details or '', designation or '']))
    colours = re.findall(r'\b(RED|GREEN|AMBER|YELLOW|BLUE|WHITE|CLEAR)\b', text, re.I)
    colour = colours[0].upper() if colours else ''
    voltage = first_match(r'\b(\d+(?:\.\d+)?(?:/\d+(?:\.\d+)?)?V(?:\s*AC/DC|\s*AC|\s*DC)?)\b', text)
    if not voltage:
        voltage = first_match(r'\b(\d+(?:\.\d+)?(?:\s*TO\s*\d+(?:\.\d+)?)?V(?:\s*AC\s*&\s*\d+(?:\.\d+)?(?:\s*TO\s*\d+(?:\.\d+)?)?V)?\s*(?:AC|DC))\b', text)
    return colour, clean_value(voltage).upper()


def extract_led_colours(description, details, designation):
    text = clean_value(' '.join([description or '', details or '', designation or '']))
    return [x.upper() for x in re.findall(r'\b(RED|GREEN|AMBER|YELLOW|BLUE|WHITE|CLEAR)\b', text, re.I)]


def led_voltage_matches(msld_voltage, master_voltage):
    """
    Special variable-voltage rule used only for the 63.5V LED.
    A 63.5V AC MSLD lamp is supported by the master entry
    '42 to 240V AC & 42 to 220V DC'.
    """
    target = clean_value(msld_voltage).upper().replace('–','-').replace('—','-')
    available = clean_value(master_voltage).upper().replace('–','-').replace('—','-')

    if not re.fullmatch(r'63\.5\s*V\s*(AC|DC)', target, re.I):
        return False

    supply = re.search(r'(AC|DC)\s*
    colour = clean_value(colour).upper()
    voltage = clean_value(voltage).upper()
    if not colour or not voltage:
        return None

    target = re.sub(r'\s+', ' ', voltage)
    for item in load_led_master():
        if item['colour'] != colour:
            continue
        master_voltage = re.sub(r'\s+', ' ', item['voltage']).upper()

        # Normal rule: every voltage must match the exact master-data entry.
        if master_voltage == target:
            return item

        # Special fixed-template rule: ONLY 63.5V AC/DC uses the
        # variable-voltage LED family from the master database.
        if allow_variable and re.fullmatch(r'63\.5\s*V\s*(AC|DC)', target, re.I):
            if led_voltage_matches(target, master_voltage):
                return item
    return None


def led_master_match(description, details, designation):
    colour, voltage = extract_led_attributes(description, details, designation)
    return led_master_match_by_values(colour, voltage)


def led_spec_from_item(item):
    if not item:
        return [], None
    make = (item.get('manufacturer') or '').strip()
    desc = item.get('description','').strip()
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


def led_spec(description, details, designation):
    item = led_master_match(description, details, designation)
    return led_spec_from_item(item)


def led_specs_for_h7_h9(description, details, designation):
    """
    Fixed MSLD special case for H7/H8/H9:
    H7 = RED, H8 = YELLOW, H9 = BLUE.
    Each lamp is emitted as its own BOM row.
    For 63.5V only, use the variable 42-240V AC / 42-220V DC
    master-data family. Other voltages remain exact-match only.
    """
    text = clean_value(' '.join([description or '', details or '', designation or '']))
    # H7/H8/H9 is a fixed-template three-lamp group. Do not depend on
    # the PDF text extractor preserving the visual order of colours.
    expected = [('H7', 'RED'), ('H8', 'YELLOW'), ('H9', 'BLUE')]
    selected = expected if re.search(r'H7\s*/\s*H8\s*/\s*H9', text, re.I) else []
    if not selected:
        return []

    voltage = first_match(r'\b(\d+(?:\.\d+)?)\s*V\s*(AC|DC)\b', text)
    if not voltage:
        return []

    results = []
    for des, colour in selected:
        item = led_master_match_by_values(
            colour,
            voltage,
            allow_variable=re.fullmatch(r'63\.5\s*V\s*(AC|DC)', voltage, re.I) is not None
        )
        lines, item = led_spec_from_item(item)
        if lines:
            results.append({'designation': des, 'specification_lines': lines, 'item': item})
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
 v=(v or '').upper().replace('KV','').strip(); return v if v in {'6.6','11','33'} else ''
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
 if 'LED LAMP' in s or re.search(r'\bLED\b',s):return 'LED'
 if 'CURRENT TRANSFORMER' in s:return 'CT'
 if 'POTENTIAL TRANSFORMER' in s:return 'PT'
 if '7SJ6611' in s or 'NUM. PROT. RELAY' in s or 'NUMERICAL PROTECTION RELAY' in s:return '7SJ6611'
 if 'DIGITAL MF METER' in s or 'EM6400NG' in s:return 'EM6400NG'
 if 'DIGITAL VOLTMETER' in s:return 'VOLTMETER'
 if 'DIGITAL AMMETER' in s:return 'AMMETER'
 if re.search(r'\bMCB\b',s):return 'MCB'
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

def extract_ct_make(dis_text):
    text = dis_search_text(dis_text)
    # Fixed DIS make may appear as "PRAGATI MAKE", "PRAGATI/ECS MAKE",
    # "CT MAKE: PRAGATI", or "MAKE OF CT: PRAGATI". Do not depend on one
    # exact PDF text layout.
    patterns = [
        r'\b(PRAGATI\s*/\s*ECS)\s+MAKE\b',
        r'\b(PRAGATI\s*/\s*ECS)\b',
        r'\b(PRAGATI)\s+MAKE\b',
        r'(?:CT\s*/?\s*MAKE|CT/PT\s+MAKE|MAKE\s+OF\s+CT)\s*[:\-]?\s*([A-Za-z][A-Za-z0-9.&/\-]*(?:\s*/\s*[A-Za-z][A-Za-z0-9.&/\-]*)?)',
        r'\bMAKE\s*[:\-]\s*([A-Za-z][A-Za-z0-9.&/\-]*(?:\s*/\s*[A-Za-z][A-Za-z0-9.&/\-]*)?)',
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

def ct_spec(ct_text, dis_text):
    ct_code=generate_ct_code(ct_text)
    ct_make=extract_ct_make(dis_text)
    # Annexure-2/master-data make for the fixed CT template. If the PDF
    # extraction does not expose the Annexure-2 table text, retain the
    # approved CT master value so the BOM does not lose the MAKE line.
    if not ct_make:
        ct_make = 'PRAGATI/ECS'
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
  if r['master_code']=='CT':
   ct_row_text = ' '.join([r.get('designation',''), r.get('description',''), r.get('details','')])
   spec_lines=ct_spec(ct_row_text,dis_text)
   spec='\n'.join(spec_lines)
   editable_fields=['INSULATION CLASS:-B','CT SECONDARY TERMINAL ON P2 SIDE']
   editable_indices=[8,14]
   rows.append({'sr':i,'specification':spec,'specification_lines':spec_lines,'editable_fields':editable_fields,'editable_indices':editable_indices,'designation':r['designation'],'feeder_name':feeder_name,'feeder_qty':feeder_qty,'total':(r['quantity'] or 1)*feeder_qty_num,'eqpt_qty':(r['quantity'] or 1),'mpd':'','amd':'','master_code':r['master_code']})
  elif r['master_code']=='LED':
   # H7/H8/H9 are three separate lamps in the fixed MSLD template.
   if re.fullmatch(r'H7/H8/H9', r.get('designation','').strip(), re.I):
    led_parts = led_specs_for_h7_h9(r.get('description',''), r.get('details',''), r.get('designation',''))
    if led_parts:
     for part in led_parts:
      spec='\n'.join(part['specification_lines'])
      rows.append({'sr':len(rows)+1,'specification':spec,'specification_lines':part['specification_lines'],'editable_fields':[],'editable_indices':[],'designation':part['designation'],'feeder_name':feeder_name,'feeder_qty':feeder_qty,'total':1*feeder_qty_num,'eqpt_qty':1,'mpd':'','amd':'','master_code':r['master_code']})
     continue
   spec_lines, led_item = led_spec(r.get('description',''), r.get('details',''), r.get('designation',''))
   spec='\n'.join(spec_lines) if spec_lines else r['description']+((' | '+r['details']) if r['details'] else '')
   spec_lines=spec_lines or ([clean(spec)] if spec else [])
   rows.append({'sr':len(rows)+1,'specification':spec,'specification_lines':spec_lines,'editable_fields':[],'editable_indices':[],'designation':r['designation'],'feeder_name':feeder_name,'feeder_qty':feeder_qty,'total':(r['quantity'] or 1)*feeder_qty_num,'eqpt_qty':(r['quantity'] or 1),'mpd':'','amd':'','master_code':r['master_code']})
  else:
   spec=r['description']+((' | '+r['details']) if r['details'] else '')
   spec_lines=[clean(spec)] if spec else []
   rows.append({'sr':len(rows)+1,'specification':spec,'specification_lines':spec_lines,'editable_fields':[],'editable_indices':[],'designation':r['designation'],'feeder_name':feeder_name,'feeder_qty':feeder_qty,'total':(r['quantity'] or 1)*feeder_qty_num,'eqpt_qty':(r['quantity'] or 1),'mpd':'','amd':'','master_code':r['master_code']})
 return rows,feeder_name,feeder_qty

def extract_from_files(msld_bytes,msld_name,dis_bytes,dis_name,client,sales,drawing,esd,wo,prep,voltage):
 msld_text=pdf_text(msld_bytes,msld_name); dis_text=pdf_text(dis_bytes,dis_name); same=bool(msld_text.strip()) and re.sub(r'\s+','',msld_text)==re.sub(r'\s+','',dis_text); feeder_info,records,warnings=extract_fixed_table(msld_bytes,msld_name)
 if same:warnings.append('The same PDF was supplied in both MSLD and DIS slots. It was processed once; DIS was used as the validation copy.')
 elif dis_text.strip():warnings.append('DIS supplied: used as the fixed specification source; MSLD remains the primary project-data source.')
 rows,feeder_name,feeder_qty=build_rows(feeder_info,records,msld_text,dis_text); h=header(msld_text,dis_text,client,sales,drawing,esd,wo,prep,voltage); ph=extract_header_from_page(msld_bytes,msld_name)
 for key in ('client','sales_ref','drawing','wo'):
  if ph.get(key):h[key]=ph[key]
 if ph.get('esd'):h['esd']=ph['esd']
 source=msld_text+'\n'+dis_text; q=first(r'\bQty\.\s*:\s*(\d+x?)\b',source) or first(r'\bQTY\.\s*:\s*(\d+x?)\b',source) or ph.get('qty',''); v=norm_voltage(voltage) or first(r'\b(6\.6|11|33)\s*KV\b',source); description=f'{v}kV SWITCHBOARD' if v else 'SWITCHBOARD'
 if not rows:warnings.append('No equipment rows were confidently extracted from the fixed MSLD table.')
 unmatched=[r['designation'] for r in records if r['master_code']=='UNMATCHED']
 if unmatched:warnings.append('Master Data match not available for: '+', '.join(unmatched)+'. Source description was preserved; no engineering item was guessed.')
 return {'header':h,'document_no':'SI EA/CS/FR/EG/015','revision':'1.0','effective_date':'17/07/2026','created_by':'EA CS ENGG','description':description,'rows':rows,'feeder_name':feeder_name,'feeder_qty':feeder_qty,'qty':q,'warnings':warnings,'feeder_details':feeder_info,'same_input_file':same}

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
, target, re.I).group(1).upper()

    # Accept only the fixed variable-voltage master family for the
    # corresponding supply type.
    if supply == 'AC':
        return bool(re.search(r'42\s*TO\s*240\s*V\s*AC', available, re.I))
    return bool(re.search(r'42\s*TO\s*220\s*V\s*DC', available, re.I))


def led_master_match_by_values(colour, voltage, allow_variable=False):
    colour = clean_value(colour).upper()
    voltage = clean_value(voltage).upper()
    if not colour or not voltage:
        return None

    target = re.sub(r'\s+', ' ', voltage)
    for item in load_led_master():
        if item['colour'] != colour:
            continue
        master_voltage = re.sub(r'\s+', ' ', item['voltage']).upper()

        # Normal rule: every voltage must match the exact master-data entry.
        if master_voltage == target:
            return item

        # Special fixed-template rule: ONLY 63.5V AC/DC uses the
        # variable-voltage LED family from the master database.
        if allow_variable and re.fullmatch(r'63\.5\s*V\s*(AC|DC)', target, re.I):
            if led_voltage_matches(target, master_voltage):
                return item
    return None


def led_master_match(description, details, designation):
    colour, voltage = extract_led_attributes(description, details, designation)
    return led_master_match_by_values(colour, voltage)


def led_spec_from_item(item):
    if not item:
        return [], None
    make = (item.get('manufacturer') or '').strip()
    desc = item.get('description','').strip()
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


def led_spec(description, details, designation):
    item = led_master_match(description, details, designation)
    return led_spec_from_item(item)


def led_specs_for_h7_h9(description, details, designation):
    """
    Fixed MSLD special case for H7/H8/H9:
    H7 = RED, H8 = YELLOW, H9 = BLUE.
    Each lamp is emitted as its own BOM row.
    For 63.5V only, use the variable 42-240V AC / 42-220V DC
    master-data family. Other voltages remain exact-match only.
    """
    text = clean_value(' '.join([description or '', details or '', designation or '']))
    colours = extract_led_colours(description, details, designation)

    # Preserve the fixed-template designation-to-colour mapping.
    expected = [('H7', 'RED'), ('H8', 'YELLOW'), ('H9', 'BLUE')]
    selected = []
    for des, colour in expected:
        if colour in colours:
            selected.append((des, colour))

    # If PDF extraction collapses/reorders the colour text, fall back to
    # the fixed H7/H8/H9 order rather than combining the three lamps.
    if not selected and re.search(r'\bH7/H8/H9\b', text, re.I):
        selected = expected

    voltage = first_match(r'\b(\d+(?:\.\d+)?)\s*V\s*(AC|DC)\b', text)
    if not voltage:
        return []

    results = []
    for des, colour in selected:
        item = led_master_match_by_values(
            colour,
            voltage,
            allow_variable=re.fullmatch(r'63\.5\s*V\s*(AC|DC)', voltage, re.I) is not None
        )
        lines, item = led_spec_from_item(item)
        if lines:
            results.append({'designation': des, 'specification_lines': lines, 'item': item})
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
 v=(v or '').upper().replace('KV','').strip(); return v if v in {'6.6','11','33'} else ''
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
 if 'LED LAMP' in s or re.search(r'\bLED\b',s):return 'LED'
 if 'CURRENT TRANSFORMER' in s:return 'CT'
 if 'POTENTIAL TRANSFORMER' in s:return 'PT'
 if '7SJ6611' in s or 'NUM. PROT. RELAY' in s or 'NUMERICAL PROTECTION RELAY' in s:return '7SJ6611'
 if 'DIGITAL MF METER' in s or 'EM6400NG' in s:return 'EM6400NG'
 if 'DIGITAL VOLTMETER' in s:return 'VOLTMETER'
 if 'DIGITAL AMMETER' in s:return 'AMMETER'
 if re.search(r'\bMCB\b',s):return 'MCB'
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

def extract_ct_make(dis_text):
    text = dis_search_text(dis_text)
    # Fixed DIS make may appear as "PRAGATI MAKE", "PRAGATI/ECS MAKE",
    # "CT MAKE: PRAGATI", or "MAKE OF CT: PRAGATI". Do not depend on one
    # exact PDF text layout.
    patterns = [
        r'\b(PRAGATI\s*/\s*ECS)\s+MAKE\b',
        r'\b(PRAGATI\s*/\s*ECS)\b',
        r'\b(PRAGATI)\s+MAKE\b',
        r'(?:CT\s*/?\s*MAKE|CT/PT\s+MAKE|MAKE\s+OF\s+CT)\s*[:\-]?\s*([A-Za-z][A-Za-z0-9.&/\-]*(?:\s*/\s*[A-Za-z][A-Za-z0-9.&/\-]*)?)',
        r'\bMAKE\s*[:\-]\s*([A-Za-z][A-Za-z0-9.&/\-]*(?:\s*/\s*[A-Za-z][A-Za-z0-9.&/\-]*)?)',
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

def ct_spec(ct_text, dis_text):
    ct_code=generate_ct_code(ct_text)
    ct_make=extract_ct_make(dis_text)
    # Annexure-2/master-data make for the fixed CT template. If the PDF
    # extraction does not expose the Annexure-2 table text, retain the
    # approved CT master value so the BOM does not lose the MAKE line.
    if not ct_make:
        ct_make = 'PRAGATI/ECS'
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
  if r['master_code']=='CT':
   ct_row_text = ' '.join([r.get('designation',''), r.get('description',''), r.get('details','')])
   spec_lines=ct_spec(ct_row_text,dis_text)
   spec='\n'.join(spec_lines)
   editable_fields=['INSULATION CLASS:-B','CT SECONDARY TERMINAL ON P2 SIDE']
   editable_indices=[8,14]
   rows.append({'sr':i,'specification':spec,'specification_lines':spec_lines,'editable_fields':editable_fields,'editable_indices':editable_indices,'designation':r['designation'],'feeder_name':feeder_name,'feeder_qty':feeder_qty,'total':(r['quantity'] or 1)*feeder_qty_num,'eqpt_qty':(r['quantity'] or 1),'mpd':'','amd':'','master_code':r['master_code']})
  elif r['master_code']=='LED':
   # H7/H8/H9 are three separate lamps in the fixed MSLD template.
   if re.fullmatch(r'H7/H8/H9', r.get('designation','').strip(), re.I):
    led_parts = led_specs_for_h7_h9(r.get('description',''), r.get('details',''), r.get('designation',''))
    if led_parts:
     for part in led_parts:
      spec='\n'.join(part['specification_lines'])
      rows.append({'sr':len(rows)+1,'specification':spec,'specification_lines':part['specification_lines'],'editable_fields':[],'editable_indices':[],'designation':part['designation'],'feeder_name':feeder_name,'feeder_qty':feeder_qty,'total':1*feeder_qty_num,'eqpt_qty':1,'mpd':'','amd':'','master_code':r['master_code']})
     continue
   spec_lines, led_item = led_spec(r.get('description',''), r.get('details',''), r.get('designation',''))
   spec='\n'.join(spec_lines) if spec_lines else r['description']+((' | '+r['details']) if r['details'] else '')
   spec_lines=spec_lines or ([clean(spec)] if spec else [])
   rows.append({'sr':len(rows)+1,'specification':spec,'specification_lines':spec_lines,'editable_fields':[],'editable_indices':[],'designation':r['designation'],'feeder_name':feeder_name,'feeder_qty':feeder_qty,'total':(r['quantity'] or 1)*feeder_qty_num,'eqpt_qty':(r['quantity'] or 1),'mpd':'','amd':'','master_code':r['master_code']})
  else:
   spec=r['description']+((' | '+r['details']) if r['details'] else '')
   spec_lines=[clean(spec)] if spec else []
   rows.append({'sr':len(rows)+1,'specification':spec,'specification_lines':spec_lines,'editable_fields':[],'editable_indices':[],'designation':r['designation'],'feeder_name':feeder_name,'feeder_qty':feeder_qty,'total':(r['quantity'] or 1)*feeder_qty_num,'eqpt_qty':(r['quantity'] or 1),'mpd':'','amd':'','master_code':r['master_code']})
 return rows,feeder_name,feeder_qty

def extract_from_files(msld_bytes,msld_name,dis_bytes,dis_name,client,sales,drawing,esd,wo,prep,voltage):
 msld_text=pdf_text(msld_bytes,msld_name); dis_text=pdf_text(dis_bytes,dis_name); same=bool(msld_text.strip()) and re.sub(r'\s+','',msld_text)==re.sub(r'\s+','',dis_text); feeder_info,records,warnings=extract_fixed_table(msld_bytes,msld_name)
 if same:warnings.append('The same PDF was supplied in both MSLD and DIS slots. It was processed once; DIS was used as the validation copy.')
 elif dis_text.strip():warnings.append('DIS supplied: used as the fixed specification source; MSLD remains the primary project-data source.')
 rows,feeder_name,feeder_qty=build_rows(feeder_info,records,msld_text,dis_text); h=header(msld_text,dis_text,client,sales,drawing,esd,wo,prep,voltage); ph=extract_header_from_page(msld_bytes,msld_name)
 for key in ('client','sales_ref','drawing','wo'):
  if ph.get(key):h[key]=ph[key]
 if ph.get('esd'):h['esd']=ph['esd']
 source=msld_text+'\n'+dis_text; q=first(r'\bQty\.\s*:\s*(\d+x?)\b',source) or first(r'\bQTY\.\s*:\s*(\d+x?)\b',source) or ph.get('qty',''); v=norm_voltage(voltage) or first(r'\b(6\.6|11|33)\s*KV\b',source); description=f'{v}kV SWITCHBOARD' if v else 'SWITCHBOARD'
 if not rows:warnings.append('No equipment rows were confidently extracted from the fixed MSLD table.')
 unmatched=[r['designation'] for r in records if r['master_code']=='UNMATCHED']
 if unmatched:warnings.append('Master Data match not available for: '+', '.join(unmatched)+'. Source description was preserved; no engineering item was guessed.')
 return {'header':h,'document_no':'SI EA/CS/FR/EG/015','revision':'1.0','effective_date':'17/07/2026','created_by':'EA CS ENGG','description':description,'rows':rows,'feeder_name':feeder_name,'feeder_qty':feeder_qty,'qty':q,'warnings':warnings,'feeder_details':feeder_info,'same_input_file':same}

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
