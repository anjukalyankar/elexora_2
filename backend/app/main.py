from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from datetime import datetime
import fitz, io, re
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, Border, Side
from openpyxl.utils import get_column_letter

app=FastAPI(title='ELEXORA 2 API',version='0.4.0')
app.add_middleware(CORSMiddleware,allow_origins=['*'],allow_credentials=True,allow_methods=['*'],allow_headers=['*'])

# Fixed BOM FORMAT.pdf feeder headers. These are template columns, not inferred data.
FEEDERS=['INCOMER-1','INCOMER-2','MOT01-MOT03,MOT05,MOT06','MOT04_VCB','IC0G_VCU','TR','BPT','LPT','CAB01','CAB02']
MASTER={'CT2C-1A':'CURRENT TRANSFORMER EPOXY CAST RESIN (WOUND TYPE)','PT':'POTENTIAL TRANSFORMER (DRAWOUT TYPE)','7SJ6611':'NUMERICAL PROTECTION RELAY','EM6400NG':'DIGITAL MF METER','AMMETER':'DIGITAL AMMETER WITH BUILT IN SEL. S/W','VOLTMETER':'DIGITAL VOLTMETER WITH BUILT IN SEL. S/W','MCB':'MINIATURE CIRCUIT BREAKER'}

def pdf_text(data,filename):
    if not filename.lower().endswith('.pdf'): return ''
    doc=fitz.open(stream=data,filetype='pdf')
    return '\n'.join(p.get_text('text') for p in doc)

def first(pattern,text,default=''):
    m=re.search(pattern,text,re.I|re.M); return m.group(1).strip() if m else default

def norm_voltage(v):
    v=(v or '').upper().replace('KV','').strip(); return v if v in {'6.6','11','33'} else ''

def header(msld,dis,client,sales,drawing,esd,wo,prep,voltage):
    s=msld+'\n'+dis
    return {'client':client.strip() or first(r'Client\s*:\s*([^\n]+)',s),'sales_ref':sales.strip() or first(r'Sales\s*(?:Ref(?:erence)?|Order)\s*:?\s*([^\n]+)',s),'drawing':drawing.strip() or first(r'Drg\.?\s*No\.?\s*:?\s*([^\n]+)',s),'esd':esd.strip() or first(r'ESD\s*No\.?\s*:?\s*([^\n]+)',s),'wo':wo.strip() or first(r'W\.?O\.?\s*No\.?\s*:?\s*([^\n]+)',s),'prep_by':prep.strip(),'voltage':norm_voltage(voltage)}

def qty_from_block(block):
    m=re.search(r'\b(?:QTY\.?|QUANTITY|NOS?\.?)\s*:?\s*(\d+)\b',block,re.I); return int(m.group(1)) if m else None

def designation_before(text,position):
    # Fixed MSLD has FEEDER DESIGNATION / DESIG. fields. Search only the local equipment block.
    left=text[max(0,position-250):position]
    pats=[r'\bT\d+(?:\s*[-/]\s*T\d+)?\b',r'\bE\d{1,3}\b',r'\bF\d{1,3}(?:-F\d{1,3})?\b',r'\bP\d{1,3}\b',r'\bK\d{1,3}\b',r'\bS\d{1,3}\b',r'\bX\d{1,3}\b']
    out=[]
    for p in pats:
        for x in re.findall(p,left,re.I):
            x=re.sub(r'\s+','',x.upper())
            if x not in out: out.append(x)
    return ', '.join(out[-3:])

def structured_spec(block):
    b=re.sub(r'\s+',' ',block).strip()
    # Preserve engineering wording; only remove drawing-control noise.
    b=re.sub(r'\b(?:QTY\.?|QUANTITY|NOS?\.?)\s*:?\s*\d+\b','',b,flags=re.I)
    return b.strip(' -')

def rows_from_msld(msld,dis):
    s=msld+'\n'+dis
    patterns=[('CT2C-1A',r'CURRENT TRANSFORMER'),('PT',r'POTENTIAL TRANSFORMER'),('7SJ6611',r'7SJ6611|NUM\.?\s*PROT\.?\s*RELAY|NUMERICAL PROTECTION RELAY'),('EM6400NG',r'EM6400NG|DIGITAL MF METER'),('AMMETER',r'DIGITAL AMMETER'),('VOLTMETER',r'DIGITAL VOLTMETER'),('MCB',r'\bMCB\b')]
    rows=[]
    for code,pat in patterns:
        m=re.search(pat,s,re.I)
        if not m: continue
        start=max(0,m.start()-180); end=min(len(s),m.end()+650)
        block=s[start:end]
        # Prefer the designation closest to the equipment description.
        des=designation_before(s,m.start())
        if code=='CT2C-1A' and not des and re.search(r'\bT1-T3\b',s,re.I): des='T1-T3'
        if code=='PT' and not des and re.search(r'\bT20-T22\b',s,re.I): des='T20-T22'
        if code=='AMMETER' and not des: des='P1'
        if code=='VOLTMETER' and not des: des='P4'
        if code=='EM6400NG' and not des: des='P20'
        q=qty_from_block(block)
        rows.append({'sr':len(rows)+1,'specification':structured_spec(block),'designation':des,'total':q,'eqpt_qty':q,'feeder_qty':{f:'' for f in FEEDERS},'mpd':'','amd':'','master_code':code})
    return rows

def extract(msld,dis,client,sales,drawing,esd,wo,prep,voltage):
    h=header(msld,dis,client,sales,drawing,esd,wo,prep,voltage)
    rows=rows_from_msld(msld,dis)
    source=msld+'\n'+dis
    q=first(r'\bQTY\.\s*[:]?\s*(\d+x?)\b',source)
    if not q: q=first(r'\bQty\.?\s*:\s*(\d+x?)\b',source)
    desc=f"{h['voltage']}kV SWITCHBOARD" if h['voltage'] else 'SWITCHBOARD'
    warnings=[]
    if not rows: warnings.append('No equipment rows were confidently extracted from the fixed MSLD/DIS template.')
    if any(r['total'] is None or r['eqpt_qty'] is None for r in rows): warnings.append('One or more equipment quantities were not explicitly readable and were left blank; no quantity was guessed.')
    return {'header':h,'document_no':'SI EA/CS/FR/EG/015','revision':'1.0','effective_date':'17/07/2026','created_by':'EA CS ENGG','description':desc,'rows':rows,'feeders':FEEDERS,'qty':q,'warnings':warnings}

@app.get('/health')
def health(): return {'status':'ok'}

@app.post('/api/bom/preview')
async def preview(client:str=Form(''),sales_ref:str=Form(''),drawing:str=Form(''),esd:str=Form(''),wo:str=Form(''),prep_by:str=Form(''),voltage:str=Form(''),msld:UploadFile=File(...),dis:UploadFile=File(...)):
    m,d=await msld.read(),await dis.read(); return extract(pdf_text(m,msld.filename),pdf_text(d,dis.filename),client,sales_ref,drawing,esd,wo,prep_by,voltage)

def apply_border(ws,cell,border): ws[cell].border=border

def export_book(payload):
    wb=Workbook(); ws=wb.active; ws.title='BOM'
    thin=Side(style='thin'); border=Border(left=thin,right=thin,top=thin,bottom=thin)
    h=payload.get('header',{}); feeders=payload.get('feeders',FEEDERS); rows=payload.get('rows',[])
    # Landscape A4 and print geometry follows the one-page reference sheet.
    ws.page_setup.orientation='landscape'; ws.page_setup.paperSize=ws.PAPERSIZE_A4; ws.page_setup.fitToWidth=1; ws.page_setup.fitToHeight=1; ws.sheet_properties.pageSetUpPr.fitToPage=True
    ws.page_margins.left=.15; ws.page_margins.right=.15; ws.page_margins.top=.25; ws.page_margins.bottom=.25; ws.page_margins.header=.1; ws.page_margins.footer=.1
    ws.print_area='A1:Q30'; ws.freeze_panes='A11'
    widths=[9,52,13]+[10]*len(feeders)+[9,9,8,8]
    for i,w in enumerate(widths,1): ws.column_dimensions[get_column_letter(i)].width=w
    # Top right document control, same wording as reference.
    ws.merge_cells('A1:Q1'); ws['A1']=f"EQUIPMENT LIST                                      Doc.No.: {payload.get('document_no','SI EA/CS/FR/EG/015')}"; ws['A1'].font=Font(name='Arial',size=9,bold=True); ws['A1'].alignment=Alignment(horizontal='right')
    ws.merge_cells('A2:Q2'); ws['A2']=f"Rev.No.: {payload.get('revision','1.0')}, Eff.Dt: {payload.get('effective_date','17/07/2026')}                                      Created By: {payload.get('created_by','EA CS ENGG')}"; ws['A2'].font=Font(name='Arial',size=7); ws['A2'].alignment=Alignment(horizontal='right')
    # BOM table starts at row 4, with grouped TYPICAL FEEDERS header.
    r0=4
    labels=['EQPT. NO.','SPECIFICATION','DESIGNATION']
    for c,v in enumerate(labels,1): ws.cell(r0,c,v)
    feeder_start=4; feeder_end=3+len(feeders)
    ws.merge_cells(start_row=r0,start_column=feeder_start,end_row=r0,end_column=feeder_end); ws.cell(r0,feeder_start,'TYPICAL FEEDERS')
    total_col=feeder_end+1; eq_col=feeder_end+2; mpd_col=feeder_end+3; amd_col=feeder_end+4
    ws.cell(r0,total_col,'TOTAL'); ws.cell(r0,eq_col,'EQPT QTY'); ws.cell(r0,mpd_col,'MPD'); ws.cell(r0,amd_col,'AMD')
    for c in range(1,amd_col+1):
        x=ws.cell(r0,c); x.font=Font(name='Arial',size=6,bold=True); x.alignment=Alignment(horizontal='center',vertical='center',wrap_text=True); x.border=border
    ws.row_dimensions[r0].height=22
    r1=5
    # Second header row contains the feeder names, as in the supplied BOM.
    for c in range(1,4): ws.cell(r1,c).border=border
    for i,f in enumerate(feeders,feeder_start):
        ws.cell(r1,i,f); ws.cell(r1,i).font=Font(name='Arial',size=5,bold=True); ws.cell(r1,i).alignment=Alignment(horizontal='center',vertical='center',wrap_text=True); ws.cell(r1,i).border=border
    for c in [total_col,eq_col,mpd_col,amd_col]: ws.merge_cells(start_row=r0+1,start_column=c,end_row=r1,end_column=c); ws.cell(r0+1,c).border=border
    for c in range(1,4): ws.merge_cells(start_row=r0,start_column=c,end_row=r1,end_column=c); ws.cell(r0,c).border=border
    ws.row_dimensions[r1].height=35
    for rr,row in enumerate(rows,r1+1):
        vals=[row.get('sr',''),row.get('specification',''),row.get('designation','')]
        for c,v in enumerate(vals,1): ws.cell(rr,c,v)
        fq=row.get('feeder_qty',{})
        for i,f in enumerate(feeders,feeder_start): ws.cell(rr,i,fq.get(f,''))
        ws.cell(rr,total_col,row.get('total') if row.get('total') is not None else '')
        ws.cell(rr,eq_col,row.get('eqpt_qty') if row.get('eqpt_qty') is not None else '')
        ws.cell(rr,mpd_col,''); ws.cell(rr,amd_col,'')
        for c in range(1,amd_col+1):
            x=ws.cell(rr,c); x.font=Font(name='Arial',size=6); x.alignment=Alignment(horizontal='center' if c!=2 else 'left',vertical='top',wrap_text=True); x.border=border
        ws.row_dimensions[rr].height=100
    # Footer is fixed and kept at the bottom area of the one-page sheet.
    fr=max(r1+len(rows)+1,22)
    fields=[('A'+str(fr),'Item No.','B'+str(fr),'100'),('A'+str(fr+1),'Client :','B'+str(fr+1),h.get('client','')),('A'+str(fr+2),'Sales Ref No.:','B'+str(fr+2),h.get('sales_ref','')),('A'+str(fr+3),'DATE :','B'+str(fr+3),datetime.now().strftime('%d.%m.%Y')),('A'+str(fr+4),'Description :','B'+str(fr+4),payload.get('description','')),('A'+str(fr+5),'W.O. No.:','B'+str(fr+5),h.get('wo','')),('E'+str(fr),'Drg. No.:','F'+str(fr),h.get('drawing','')),('E'+str(fr+1),'PRE.BY :','F'+str(fr+1),h.get('prep_by','')),('E'+str(fr+2),'Qty.:','F'+str(fr+2),payload.get('qty','')),('E'+str(fr+3),'ESD No.:','F'+str(fr+3),h.get('esd',''))]
    for lc,l,vc,v in fields: ws[lc]=l; ws[vc]=v; ws[lc].font=Font(name='Arial',size=6,bold=True); ws[vc].font=Font(name='Arial',size=6); ws[lc].border=border; ws[vc].border=border; ws[vc].alignment=Alignment(wrap_text=True)
    ws.oddFooter.left.text='Item No.: 100\nClient : '+str(h.get('client',''))+'\nSales Ref No.: '+str(h.get('sales_ref',''))+'\nDATE : '+datetime.now().strftime('%d.%m.%Y')
    ws.oddFooter.center.text='Description : '+str(payload.get('description',''))+'\nW.O. No.: '+str(h.get('wo',''))+'\nDrg. No.: '+str(h.get('drawing',''))
    ws.oddFooter.right.text='PRE.BY : '+str(h.get('prep_by',''))+'\nQty.: '+str(payload.get('qty',''))+'\nESD No.: '+str(h.get('esd',''))+'\n1 of 1'
    return wb

@app.post('/api/bom/export')
async def export(payload:dict):
    wb=export_book(payload); out=io.BytesIO(); wb.save(out); out.seek(0)
    return StreamingResponse(out,media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',headers={'Content-Disposition':'attachment; filename=ELEXORA_BOM.xlsx'})
