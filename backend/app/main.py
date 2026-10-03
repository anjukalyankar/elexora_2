from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from datetime import datetime
import fitz, io, re
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, Border, Side
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
  words=page_words(page); feeder_type=text_in_band(words,440,520,410,430); feeder_designation=text_in_band(words,440,520,395,415); feeder_rating=text_in_band(words,440,520,430,450); wiring=text_in_band(words,440,520,450,465)
  if feeder_type:feeders.append({'name':feeder_type,'designation':feeder_designation,'rating':feeder_rating,'wiring':wiring,'quantity':1})
  left=rows_by_designation(words,275,320,490,790)
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
    return f'CURRENT TRANSFORMER {t}' if t else 'CURRENT TRANSFORMER'

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
    # Fixed DIS make field. Allow spaces around "/" because PDF extraction
    # commonly turns "PRAGATI/ECS" into "PRAGATI / ECS".
    patterns = [
        r'(?:CT\s*/?\s*MAKE|CT/PT\s+MAKE|MAKE\s+OF\s+CT)\s*[:\-]?\s*([A-Za-z][A-Za-z0-9.&/\-]*(?:\s*/\s*[A-Za-z][A-Za-z0-9.&/\-]*)?)',
        r'\bMAKE\s*[:\-]\s*([A-Za-z][A-Za-z0-9.&/\-]*(?:\s*/\s*[A-Za-z][A-Za-z0-9.&/\-]*)?)',
        r'\b(PRAGATI\s*/\s*ECS)\b',
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
    text = dis_search_text(dis_text)
    # The fixed DIS can contain "8BK80 (OD/RD) - 800mm WIDTH PANEL".
    # Ignore the bracketed variant in the BOM and keep the required
    # designation + panel width format.
    m = re.search(r'\b(8BK80)\s*(?:\((?:OD|RD)\))?\s*[-–]?\s*(\d{3,4})\s*mm\s*WIDTH\s*PANEL\b', text, re.I)
    if m:
        return f'{m.group(1).upper()}-{m.group(2)}mm WIDTH PANEL'
    m = re.search(r'\b([A-Z0-9+]+)\s*(?:\([A-Z0-9]+\))?\s*[-–]?\s*(\d{3,4})\s*mm\s*WIDTH\s*PANEL\b', text, re.I)
    if m:
        return f'{m.group(1).upper()}-{m.group(2)}mm WIDTH PANEL'
    return ''

def ct_spec(ct_text, dis_text):
    ct_code=generate_ct_code(ct_text)
    ct_make=extract_ct_make(dis_text)
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
 feeder_name=feeder_info[0]['name'] if feeder_info else ''; feeder_qty=feeder_info[0].get('quantity',1) if feeder_info else ''; rows=[]
 for i,r in enumerate(records,1):
  if r['master_code']=='CT':
   ct_row_text = ' '.join([r.get('designation',''), r.get('description',''), r.get('details','')])
   spec_lines=ct_spec(ct_row_text,dis_text); spec='\n'.join(spec_lines); editable_fields=['INSULATION CLASS:-B','CT SECONDARY TERMINAL ON P2 SIDE'],editable_indices=[8,14]
  else:spec=r['description']+((' | '+r['details']) if r['details'] else ''); spec_lines=[clean(spec)] if spec else []; editable_fields=[]
  rows.append({'sr':i,'specification':spec,'specification_lines':spec_lines,'editable_fields':editable_fields,'designation':r['designation'],'feeder_name':feeder_name,'feeder_qty':feeder_qty,'total':r['quantity'],'eqpt_qty':r['quantity'],'mpd':'','amd':'','master_code':r['master_code']})
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
 ws.merge_cells('A4:A5'); ws.merge_cells('B4:B5'); ws.merge_cells('C4:C5'); ws.merge_cells('D4:E4'); ws.merge_cells('F4:F5'); ws.merge_cells('G4:G5'); ws.merge_cells('H4:H5'); ws['D4']='FEEDER TYPICAL'; ws['D5']='QTY.'; ws['E5']=payload.get('feeder_qty','')
 for row in range(4,6):
  for col in range(1,9):ws.cell(row,col).border=border; ws.cell(row,col).alignment=Alignment(horizontal='center',vertical='center',wrap_text=True); ws.cell(row,col).font=Font(name='Courier New',size=10 if row==4 else 9,bold=True)
 ws.row_dimensions[4].height=23.25; ws.row_dimensions[5].height=21.75
 for i,row in enumerate(rows[:64],6):
  vals=[row.get('sr',''),row.get('specification',''),row.get('designation',''),'','',row.get('total') if row.get('total') is not None else '','','']
  for col,val in enumerate(vals,1):ws.cell(i,col,val).border=border; ws.cell(i,col).font=Font(name='Courier New',size=7); ws.cell(i,col).alignment=Alignment(horizontal='center' if col!=2 else 'left',vertical='center',wrap_text=True if col==2 else False)
  ws.row_dimensions[i].height=max(15.6,min(180,15.6*max(1,len(row.get('specification_lines',[])))))
 for i in range(6+len(rows[:64]),70):
  for col in range(1,9):ws.cell(i,col).border=border; ws.cell(i,col).font=Font(name='Courier New',size=7)
  ws.row_dimensions[i].height=15.6
 fr=72; footer_left=[('Item No.','100'),('Client :',h.get('client','')),('Sales Ref No.:',h.get('sales_ref','')),('DATE :',datetime.now().strftime('%d.%m.%Y'))]; footer_mid=[('Description :',payload.get('description','')),('W.O. No.:',h.get('wo','')),('Drg. No.:',h.get('drawing',''))]; footer_right=[('PRE.BY :',h.get('prep_by','')),('Qty.:',payload.get('qty','')),('ESD No.:',h.get('esd','')),('','1 of 1')]
 for i,(label,value) in enumerate(footer_left):ws.cell(fr+i,1,label).font=Font(name='Arial',size=7);ws.cell(fr+i,2,value).font=Font(name='Arial',size=7)
 for i,(label,value) in enumerate(footer_mid):ws.cell(fr+i,4,label).font=Font(name='Arial',size=7);ws.cell(fr+i,5,value).font=Font(name='Arial',size=7)
 for i,(label,value) in enumerate(footer_right):ws.cell(fr+i,7,label).font=Font(name='Arial',size=7);ws.cell(fr+i,8,value).font=Font(name='Arial',size=7);ws.cell(fr+i,8).alignment=Alignment(horizontal='right')
 ws.print_area='A1:H76'; return wb
@app.post('/api/bom/export')
async def export(payload:dict, user:str=Depends(current_user)):
 wb=export_book(payload);out=io.BytesIO();wb.save(out);out.seek(0);return StreamingResponse(out,media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',headers={'Content-Disposition':'attachment; filename=ELEXORA_BOM.xlsx'})
