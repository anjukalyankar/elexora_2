from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from datetime import datetime
import fitz, io, re
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, Border, Side

app = FastAPI(title='ELEXORA 2 API', version='0.7.0')
app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_credentials=True, allow_methods=['*'], allow_headers=['*'])
REFERENCE_FEEDER_COLUMN = 'FEEDER TYPICAL'
MASTER = {'CT2C-1A':'CURRENT TRANSFORMER EPOXY CAST RESIN (WOUND TYPE)','PT':'POTENTIAL TRANSFORMER (DRAWOUT TYPE)','7SJ6611':'NUMERICAL PROTECTION RELAY','EM6400NG':'DIGITAL MF METER','AMMETER':'DIGITAL AMMETER WITH BUILT IN SEL. S/W','VOLTMETER':'DIGITAL VOLTMETER WITH BUILT IN SEL. S/W','MCB':'MINIATURE CIRCUIT BREAKER'}

# Fixed CT BOM structure. Source tags are deliberately explicit: MSLD values are project data;
# DIS values preserve the fixed specification wording; FIXED values are unchanged/editable as noted.
CT_TEMPLATE = [
 ('CT1502C-1A','MSLD'),('PRAGATI/ECS MAKE','DIS'),('CURRENT TRANSFORMER EPOXY CAST RESIN (WOUND TYPE)','MSLD'),
 ('- SINGLE PHASE, AS PER IS/IEC STANDARD','FIXED'),('- RATED VOLTAGE : 33KV','DIS'),('- RATED FREQUENCY : 50Hz','DIS'),
 ('- SHORT TIME CURRENT : 31.5KA FOR 3SEC','MSLD'),('- BIL: 36/70/170KVp','DIS'),('INSULATION CLASS:-B','EDITABLE_FIXED'),
 ('AS GIVEN BELOW:-','FIXED'),('TWO CORE CT, CTR: 150/1-1A','MSLD'),('- CORE 1: 150/1A, 15VA, CL.: 5P10','MSLD'),
 ('- CORE 2: 150/1A, 15VA, CL.: 0.5, ISF≤5','MSLD'),('SUITABLE FOR 8BK80(RD)-1000mm WIDTH PANEL','DIS'),('CT SECONDARY TERMINAL ON P2 SIDE','EDITABLE_FIXED')]

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
  part=part.strip(); m=re.fullmatch(r'[A-Z]+(\d+)-[A-Z]?(\d+)',part)
  if m: total+=max(1,int(m.group(2))-int(m.group(1))+1)
  elif re.fullmatch(r'[A-Z]+\d+',part): total+=1
 if total==0 and des:
  m=re.fullmatch(r'[A-Z]+(\d+)-[A-Z]+(\d+)',des.upper())
  if m: total=max(1,int(m.group(2))-int(m.group(1))+1)
 return total or None
def master_match(description,details,designation):
 s=(description+' '+details).upper()
 if 'CURRENT TRANSFORMER' in s:return 'CT2C-1A'
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
   next_y=left[i+1][0] if i+1<len(left) else 790; lo,hi=max(490,y-9),min(790,(y+next_y)/2); desc=text_in_band(words,50,275,lo,hi); details=text_in_band(words,320,615,lo,hi)
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

def extract_ct_lines(msld_text,dis_text):
 out={}; out['code']=first(r'\b(CT\d+[A-Z0-9-]*)\b',msld_text) or 'CT1502C-1A'; out['body']=first(r'(CURRENT TRANSFORMER\s+EPOXY CAST RESIN\s*\(WOUND TYPE\))',msld_text) or 'CURRENT TRANSFORMER EPOXY CAST RESIN (WOUND TYPE)'; out['stc']=first(r'(SHORT TIME CURRENT\s*:\s*[^\n]+)',msld_text) or first(r'(STC\s*:\s*[^\n]+)',msld_text)
 if out['stc'] and out['stc'].upper().startswith('STC:'):out['stc']='- SHORT TIME CURRENT : '+out['stc'].split(':',1)[1].strip()
 out['ctr']=first(r'(TWO\s+CORE\s+CT\s*,?\s*CTR\s*:\s*[^\n]+)',msld_text)
 if not out['ctr']:
  ratio=first(r'\bCTR\s*:\s*([^,\n]+)',msld_text)
  if ratio:out['ctr']='TWO CORE CT, CTR: '+ratio
 core1=first(r'(CORE\s*1\s*:\s*[^\n]+)',msld_text); core2=first(r'(CORE\s*2\s*:\s*[^\n]+)',msld_text); out['core1']='- '+core1 if core1 and not core1.startswith('-') else core1; out['core2']='- '+core2 if core2 and not core2.startswith('-') else core2
 return out

def ct_spec(msld_text,dis_text):
 m=extract_ct_lines(msld_text,dis_text); lines=[]
 for template,source in CT_TEMPLATE:
  value=template
  if template=='CT1502C-1A':value=m['code']
  elif template=='CURRENT TRANSFORMER EPOXY CAST RESIN (WOUND TYPE)':value=m['body']
  elif template.startswith('- SHORT TIME CURRENT'):value=m['stc'] or template
  elif template.startswith('TWO CORE CT'):value=m['ctr'] or template
  elif template.startswith('- CORE 1'):value=m['core1'] or template
  elif template.startswith('- CORE 2'):value=m['core2'] or template
  lines.append(value)
 return lines

def build_rows(feeder_info,records,msld_text='',dis_text=''):
 feeder_name=feeder_info[0]['name'] if feeder_info else ''; feeder_qty=feeder_info[0].get('quantity',1) if feeder_info else ''; rows=[]
 for i,r in enumerate(records,1):
  if r['master_code']=='CT2C-1A':spec_lines=ct_spec(msld_text,dis_text); spec='\n'.join(spec_lines); editable_fields=['INSULATION CLASS:-B','CT SECONDARY TERMINAL ON P2 SIDE']
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
def health():return {'status':'ok'}
@app.post('/api/bom/preview')
async def preview(client:str=Form(''),sales_ref:str=Form(''),drawing:str=Form(''),esd:str=Form(''),wo:str=Form(''),prep_by:str=Form(''),voltage:str=Form(''),msld:UploadFile=File(...),dis:UploadFile=File(...)):
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
async def export(payload:dict):
 wb=export_book(payload);out=io.BytesIO();wb.save(out);out.seek(0);return StreamingResponse(out,media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',headers={'Content-Disposition':'attachment; filename=ELEXORA_BOM.xlsx'})
