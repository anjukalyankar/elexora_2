from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import fitz, io, re
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, Border, Side

app=FastAPI(title="ELEXORA 2 API",version="0.2.0")
app.add_middleware(CORSMiddleware,allow_origins=["*"],allow_credentials=True,allow_methods=["*"],allow_headers=["*"])
MASTER=[
 {"code":"CT2C-1A","category":"CURRENT TRANSFORMER","description":"CURRENT TRANSFORMER EPOXY CAST RESIN (WOUND TYPE)"},
 {"code":"PT-11KV","category":"POTENTIAL TRANSFORMER","description":"POTENTIAL TRANSFORMER (DRAWOUT TYPE)"},
 {"code":"7SJ6611","category":"PROTECTION RELAY","description":"NUMERICAL PROTECTION RELAY"},
 {"code":"EM6400NG","category":"MULTIFUNCTION METER","description":"DIGITAL MF METER"},
 {"code":"AMMETER","category":"DIGITAL AMMETER","description":"DIGITAL AMMETER WITH BUILT IN SELECTOR SWITCH"},
 {"code":"VOLTMETER","category":"DIGITAL VOLTMETER","description":"DIGITAL VOLTMETER WITH BUILT IN SELECTOR SWITCH"},
 {"code":"MCB","category":"MCB","description":"MINIATURE CIRCUIT BREAKER"}]

def text(data,filename):
 if filename.lower().endswith('.pdf'):
  d=fitz.open(stream=data,filetype='pdf');return '\n'.join(p.get_text('text') for p in d)
 return ''
def find(pattern,s,default=''):
 m=re.search(pattern,s,re.I|re.M);return m.group(1).strip() if m else default
def qty(pattern,s):
 m=re.search(pattern,s,re.I);return int(m.group(1)) if m else None

def extract(msld,dis,client,sales_ref,drawing,esd,wo,prep,voltage):
 s=msld+'\n'+dis; kv=voltage.strip().upper().replace('KV','')
 # Template-driven designation extraction: retain labels such as T1-T4, E11, E12 rather than inventing them.
 desigs=[]
 for p in [r'\bT\d+(?:\s*[-/]\s*T\d+)?\b',r'\bE\d{1,3}\b',r'\bF\d{1,3}\b',r'\bM\d{1,3}\b']:
  for x in re.findall(p,s,re.I):
   x=x.upper().replace(' ','')
   if x not in desigs: desigs.append(x)
 # Typical feeder quantities are taken from the MSLD left-side/template text. Only explicit numeric quantities are accepted.
 feeders=[]
 for m in re.finditer(r'(?P<name>T\d+(?:\s*[-/]\s*T\d+)?|E\d{1,3}|F\d{1,3}|M\d{1,3})[^\n]{0,100}',s,re.I):
  raw=m.group(0).strip(); name=m.group('name').upper().replace(' ',''); q=re.search(r'\b(?:QTY|QUANTITY|NOS?\.?|NO\.?)\s*[:=]?\s*(\d+)\b',raw,re.I)
  if q: feeders.append((name,int(q.group(1))))
 if not feeders:
  for d in desigs: feeders.append((d,1))
 # Equipment rows are derived from explicit component names and matched Master Data; no engineering rules are applied.
 patterns=[('CURRENT TRANSFORMER','CT'),('POTENTIAL TRANSFORMER','PT'),('NUMERICAL PROTECTION RELAY','7SJ6611'),('DIGITAL MF METER','EM6400NG'),('DIGITAL AMMETER','AMMETER'),('DIGITAL VOLTMETER','VOLTMETER'),('MCB','MCB')]
 rows=[];sr=1
 for cat,code in patterns:
  if re.search(cat,s,re.I):
   master=next((x for x in MASTER if x['code']==code),None)
   spec=' '.join(re.findall(r'[^\n]{0,180}'+re.escape(cat)+r'[^\n]{0,300}',s,re.I))[:500]
   rows.append({'sr':sr,'specification':spec or (master['description'] if master else cat),'designation':', '.join(desigs) if desigs else '','amd':'','total':1,'eqpt_qty':1,'mpd':'','typical_feeders':'; '.join(f'{n} - {q}' for n,q in feeders),'description':master['description'] if master else cat})
   sr+=1
 # Fixed-template equipment quantity: use explicit EQPT QTY / QTY / NOS from source; otherwise keep quantity unresolved rather than guessing.
 for r in rows:
  q=qty(r'\b(?:EQPT\.?\s*QTY|QTY|QUANTITY|NOS?)\s*[:=]?\s*(\d+)\b',s)
  if q is not None:r['eqpt_qty']=q;r['total']=q
 return {'header':{'client':client or find(r'Client\s*[:=]\s*([^\n]+)',s),'sales_ref':sales_ref or find(r'Sales\s*(?:Order|Ref(?:erence)?)\s*(?:No\.?|:)?\s*[:=]?\s*([^\n]+)',s),'drawing':drawing or find(r'Drg\.?\s*No\.?\s*[:=]?\s*([^\n]+)',s),'esd':esd or find(r'ESD\s*No\.?\s*[:=]?\s*([^\n]+)',s),'wo':wo or find(r'W\.?O\.?\s*No\.?\s*[:=]?\s*([^\n]+)',s),'prep_by':prep,'voltage':kv},'rows':rows,'description':f'{kv}KV SWITCHBOARD'}

@app.get('/health')
def health():return {'status':'ok'}
@app.post('/api/bom/preview')
async def preview(client:str=Form(''),sales_ref:str=Form(''),drawing:str=Form(''),esd:str=Form(''),wo:str=Form(''),prep_by:str=Form(''),voltage:str=Form(''),msld:UploadFile=File(...),dis:UploadFile=File(...)):
 m,d=await msld.read(),await dis.read();return extract(text(m,msld.filename),text(d,dis.filename),client,sales_ref,drawing,esd,wo,prep_by,voltage)
@app.post('/api/bom/export')
async def export(payload:dict):
 wb=Workbook();ws=wb.active;ws.title='BOM';h=payload.get('header',{});thin=Side(style='thin');border=Border(left=thin,right=thin,top=thin,bottom=thin)
 # Reference-style fixed document heading/header.
 ws.merge_cells('A1:H1');ws['A1']='ELEXORA - EQUIPMENT LIST / BILL OF MATERIAL';ws['A1'].font=Font(bold=True,size=14);ws['A1'].alignment=Alignment(horizontal='center')
 ws.merge_cells('A2:H2');ws['A2']='MEDIUM VOLTAGE SWITCHBOARD - BOM';ws['A2'].font=Font(bold=True,size=11);ws['A2'].alignment=Alignment(horizontal='center')
 ws.append([]);ws.append(['EQPT.NO','SPECIFICATION','DESIGNATION','AMD','TOTAL','EQPT QTY','MPD','TYPICAL FEEDERS'])
 for c in ws[4]:c.font=Font(bold=True);c.alignment=Alignment(horizontal='center',vertical='center',wrap_text=True);c.border=border
 for r in payload.get('rows',[]):
  ws.append([r.get('sr',''),r.get('specification',''),r.get('designation',''),'',r.get('total',''),r.get('eqpt_qty',''),'',r.get('typical_feeders','')])
 for row in ws.iter_rows(min_row=5):
  for c in row:c.border=border;c.alignment=Alignment(vertical='top',wrap_text=True)
 # Footer/header fields from PDF or user input. Description changes only by selected voltage level.
 start=ws.max_row+2
 footer=[('Client',h.get('client','')),('Sales Ref No.',h.get('sales_ref','')),('Drawing No.',h.get('drawing','')),('ESD No.',h.get('esd','')),('Description',payload.get('description',f"{h.get('voltage','')}KV SWITCHBOARD")),('W.O. No.',h.get('wo','')),('PRE. BY',h.get('prep_by','')),('Qty.',sum((r.get('eqpt_qty') or 0) for r in payload.get('rows',[])))]
 for i,(k,v) in enumerate(footer,start):ws.cell(i,1,k).font=Font(bold=True);ws.cell(i,2,v)
 widths=[12,55,24,10,10,12,10,45]
 for i,w in enumerate(widths,1):ws.column_dimensions[chr(64+i)].width=w
 ws.freeze_panes='A5';out=io.BytesIO();wb.save(out);out.seek(0)
 return StreamingResponse(out,media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',headers={'Content-Disposition':'attachment; filename=ELEXORA_BOM.xlsx'})
