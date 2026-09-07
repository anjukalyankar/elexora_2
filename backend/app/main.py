from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from datetime import datetime
import fitz, io, re
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, Border, Side
from openpyxl.utils import get_column_letter

app=FastAPI(title='ELEXORA 2 API',version='0.5.1')
app.add_middleware(CORSMiddleware,allow_origins=['*'],allow_credentials=True,allow_methods=['*'],allow_headers=['*'])

REFERENCE_FEEDERS=['INCOMER-1','INCOMER-2','MOT01-MOT03,MOT05,MOT06','MOT04_VCB','IC0G_VCU','TR','BPT','LPT','CAB01','CAB02']
MASTER={'CT2C-1A':'CURRENT TRANSFORMER EPOXY CAST RESIN (WOUND TYPE)','PT':'POTENTIAL TRANSFORMER (DRAWOUT TYPE)','7SJ6611':'NUMERICAL PROTECTION RELAY','EM6400NG':'DIGITAL MF METER','AMMETER':'DIGITAL AMMETER WITH BUILT IN SEL. S/W','VOLTMETER':'DIGITAL VOLTMETER WITH BUILT IN SEL. S/W','MCB':'MINIATURE CIRCUIT BREAKER'}

def pdf_pages(data,filename):
    if not filename.lower().endswith('.pdf'): return []
    return fitz.open(stream=data,filetype='pdf')
def pdf_text(data,filename): return '\n'.join(p.get_text('text') for p in pdf_pages(data,filename))
def first(pattern,text,default=''):
    m=re.search(pattern,text,re.I|re.M); return m.group(1).strip() if m else default
def norm_voltage(v):
    v=(v or '').upper().replace('KV','').strip(); return v if v in {'6.6','11','33'} else ''
def transform_word(page,w):
    pts=[fitz.Point(w[0],w[1])*page.rotation_matrix,fitz.Point(w[2],w[3])*page.rotation_matrix]
    return {'x':(pts[0].x+pts[1].x)/2,'y':(pts[0].y+pts[1].y)/2,'text':w[4]}
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
    for part in re.split(r'[,/]',des.upper()):
        part=part.strip(); m=re.fullmatch(r'[A-Z]+(\d+)-[A-Z]?(\d+)',part)
        if m: total+=max(1,int(m.group(2))-int(m.group(1))+1)
        elif re.fullmatch(r'[A-Z]+\d+',part): total+=1
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
        words=page_words(page)
        if page_no!=1: continue
        feeder_type=text_in_band(words,440,520,410,430); feeder_designation=text_in_band(words,440,520,395,415); feeder_rating=text_in_band(words,440,520,430,450); wiring=text_in_band(words,440,520,450,465)
        if feeder_type: feeders.append({'name':feeder_type,'designation':feeder_designation,'rating':feeder_rating,'wiring':wiring})
        left=rows_by_designation(words,275,320,490,790)
        for i,(y,des) in enumerate(left):
            next_y=left[i+1][0] if i+1<len(left) else 790; lo=max(490,y-9); hi=min(790,(y+next_y)/2); desc=text_in_band(words,50,275,lo,hi); details=text_in_band(words,320,615,lo,hi)
            if desc or details: records.append({'source_page':page_no,'description':desc,'designation':des,'details':details,'master_code':master_match(desc,details,des),'quantity':designation_qty(des),'section':'MSLD equipment'})
        for ymin,ymax in [(35,345),(350,725)]:
            right=rows_by_designation(words,840,880,ymin,ymax)
            for i,(y,des) in enumerate(right):
                next_y=right[i+1][0] if i+1<len(right) else ymax; lo=max(ymin,y-8); hi=min(ymax,(y+next_y)/2); desc=text_in_band(words,630,835,lo,hi); details=text_in_band(words,890,1175,lo,hi)
                if desc or details: records.append({'source_page':page_no,'description':desc,'designation':des,'details':details,'master_code':master_match(desc,details,des),'quantity':designation_qty(des),'section':'MSLD equipment'})
    return feeders,records,warnings
def header(msld,dis,client,sales,drawing,esd,wo,prep,voltage):
    s=msld+'\n'+dis
    return {'client':client.strip() or first(r'Client\s*:?\s*([^\n]+)',s),'sales_ref':sales.strip() or first(r'Sales\s*Ref\.?\s*:?\s*([^\n]+)',s),'drawing':drawing.strip() or first(r'Drg\.?\s*No\.?\s*:?\s*([^\n]+)',s),'esd':esd.strip() or first(r'ESD\s*No\.?\s*:?\s*([^\n]+)',s),'wo':wo.strip() or first(r'W\.?O\.?\s*No\.?\s*:?\s*([^\n]+)',s),'prep_by':prep.strip(),'voltage':norm_voltage(voltage)}
def build_rows(feeder_info,records):
    # BOM FORMAT has a fixed grouped-feeder header. Never shrink the output schema to the feeder count found on one MSLD page.
    names=REFERENCE_FEEDERS[:]; rows=[]
    extracted_name=(feeder_info[0].get('name','').upper() if feeder_info else '')
    target='INCOMER-1' if extracted_name=='INCOMER' else None
    for i,r in enumerate(records,1):
        spec=r['description']+(' | '+r['details'] if r['details'] else ''); fq={n:'' for n in names}
        if target: fq[target]=r['quantity'] if r['quantity'] is not None else ''
        rows.append({'sr':i,'specification':clean(spec),'designation':r['designation'],'total':r['quantity'],'eqpt_qty':r['quantity'],'feeder_qty':fq,'mpd':'','amd':'','master_code':r['master_code']})
    return names,rows
def extract_from_files(msld_bytes,msld_name,dis_bytes,dis_name,client,sales,drawing,esd,wo,prep,voltage):
    msld_text=pdf_text(msld_bytes,msld_name); dis_text=pdf_text(dis_bytes,dis_name); same=(re.sub(r'\s+','',msld_text)==re.sub(r'\s+','',dis_text)); feeder_info,records,warnings=extract_fixed_table(msld_bytes,msld_name)
    if same: warnings.append('The same PDF was supplied in both MSLD and DIS slots. It was processed once to prevent duplicate BOM rows.')
    elif dis_text.strip(): warnings.append('DIS supplied: used as validation source; MSLD remains the primary fixed-template equipment source.')
    names,rows=build_rows(feeder_info,records); h=header(msld_text,dis_text,client,sales,drawing,esd,wo,prep,voltage); pages=pdf_pages(msld_bytes,msld_name)
    if pages:
        pw=page_words(pages[0]); fv=lambda a,b,c,d:text_in_band(pw,a,b,c,d)
        h['client']=client.strip() or fv(345,440,775,795) or h['client']; h['sales_ref']=sales.strip() or fv(905,960,782,793) or h['sales_ref']; h['drawing']=drawing.strip() or fv(980,1110,805,830) or h['drawing']; h['wo']=wo.strip() or fv(900,960,793,805) or h['wo']
    source=msld_text+'\n'+dis_text; q=first(r'\bQty\.\s*:\s*(\d+x?)\b',source) or first(r'\bQTY\.\s*:\s*(\d+x?)\b',source); desc=f"{norm_voltage(voltage)}kV SWITCHBOARD" if norm_voltage(voltage) else 'SWITCHBOARD'
    if not rows:warnings.append('No equipment rows were confidently extracted from the fixed MSLD table.')
    unmatched=[r['designation'] for r in records if r['master_code']=='UNMATCHED']
    if unmatched:warnings.append('Master Data match not available for: '+', '.join(unmatched)+'. Their source description was preserved; no engineering item was guessed.')
    return {'header':h,'document_no':'SI EA/CS/FR/EG/015','revision':'1.0','effective_date':'17/07/2026','created_by':'EA CS ENGG','description':desc,'rows':rows,'feeders':names,'qty':q,'warnings':warnings,'feeder_details':feeder_info,'same_input_file':same}
@app.get('/health')
def health(): return {'status':'ok'}
@app.post('/api/bom/preview')
async def preview(client:str=Form(''),sales_ref:str=Form(''),drawing:str=Form(''),esd:str=Form(''),wo:str=Form(''),prep_by:str=Form(''),voltage:str=Form(''),msld:UploadFile=File(...),dis:UploadFile=File(...)):
    m,d=await msld.read(),await dis.read(); return extract_from_files(m,msld.filename,d,dis.filename,client,sales_ref,drawing,esd,wo,prep_by,voltage)
def export_book(payload):
    wb=Workbook(); ws=wb.active; ws.title='BOM'; thin=Side(style='thin'); border=Border(left=thin,right=thin,top=thin,bottom=thin); h=payload.get('header',{}); feeders=payload.get('feeders') or REFERENCE_FEEDERS; rows=payload.get('rows',[])
    ws.page_setup.orientation='landscape'; ws.page_setup.paperSize=ws.PAPERSIZE_A4; ws.page_setup.fitToWidth=1; ws.page_setup.fitToHeight=0; ws.sheet_properties.pageSetUpPr.fitToPage=True; ws.page_margins.left=.15; ws.page_margins.right=.15; ws.page_margins.top=.25; ws.page_margins.bottom=.25
    last_col=3+len(feeders)+4; widths=[9,55,13]+[11]*len(feeders)+[9,9,8,8]
    for i,w in enumerate(widths,1): ws.column_dimensions[get_column_letter(i)].width=w
    ws.merge_cells(start_row=1,start_column=1,end_row=1,end_column=last_col); ws.cell(1,1,f"EQUIPMENT LIST                                      Doc.No.: {payload.get('document_no','SI EA/CS/FR/EG/015')}"); ws['A1'].font=Font(name='Arial',size=9,bold=True); ws['A1'].alignment=Alignment(horizontal='right')
    ws.merge_cells(start_row=2,start_column=1,end_row=2,end_column=last_col); ws.cell(2,1,f"Rev.No.: {payload.get('revision','1.0')}, Eff.Dt: {payload.get('effective_date','17/07/2026')}                                      Created By: {payload.get('created_by','EA CS ENGG')}"); ws['A2'].font=Font(name='Arial',size=7); ws['A2'].alignment=Alignment(horizontal='right')
    for c,v in enumerate(['EQPT. NO.','SPECIFICATION','DESIGNATION'],1): ws.cell(4,c,v)
    feeder_start=4; feeder_end=3+len(feeders); ws.merge_cells(start_row=4,start_column=feeder_start,end_row=4,end_column=feeder_end); ws.cell(4,feeder_start,'TYPICAL FEEDERS'); total_col=feeder_end+1; eq_col=feeder_end+2; mpd_col=feeder_end+3; amd_col=feeder_end+4
    for c,v in [(total_col,'TOTAL'),(eq_col,'EQPT QTY'),(mpd_col,'MPD'),(amd_col,'AMD')]: ws.cell(4,c,v)
    for c in range(1,amd_col+1): ws.cell(4,c).font=Font(name='Arial',size=6,bold=True); ws.cell(4,c).alignment=Alignment(horizontal='center',vertical='center',wrap_text=True); ws.cell(4,c).border=border
    for c in range(1,4): ws.merge_cells(start_row=4,start_column=c,end_row=5,end_column=c); ws.cell(4,c).border=border
    for i,f in enumerate(feeders,4): ws.cell(5,i,f); ws.cell(5,i).font=Font(name='Arial',size=5,bold=True); ws.cell(5,i).alignment=Alignment(horizontal='center',vertical='center',wrap_text=True); ws.cell(5,i).border=border
    for c in [total_col,eq_col,mpd_col,amd_col]: ws.merge_cells(start_row=4,start_column=c,end_row=5,end_column=c); ws.cell(4,c).border=border
    for rr,row in enumerate(rows,6):
        for c,v in enumerate([row.get('sr',''),row.get('specification',''),row.get('designation','')],1): ws.cell(rr,c,v)
        fq=row.get('feeder_qty',{});
        for i,f in enumerate(feeders,4): ws.cell(rr,i,fq.get(f,''))
        ws.cell(rr,total_col,row.get('total') if row.get('total') is not None else ''); ws.cell(rr,eq_col,row.get('eqpt_qty') if row.get('eqpt_qty') is not None else ''); ws.cell(rr,mpd_col,''); ws.cell(rr,amd_col,'')
        for c in range(1,amd_col+1): ws.cell(rr,c).font=Font(name='Arial',size=6); ws.cell(rr,c).alignment=Alignment(horizontal='center' if c!=2 else 'left',vertical='top',wrap_text=True); ws.cell(rr,c).border=border
        ws.row_dimensions[rr].height=48
    fr=max(22,6+len(rows)+2); lf=[('Item No.','100'),('Client :',h.get('client','')),('Sales Ref No.:',h.get('sales_ref','')),('DATE :',datetime.now().strftime('%d.%m.%Y')),('Description :',payload.get('description','')),('W.O. No.:',h.get('wo',''))]; rf=[('Drg. No.:',h.get('drawing','')),('PRE.BY :',h.get('prep_by','')),('Qty.:',payload.get('qty','')),('ESD No.:',h.get('esd',''))]
    for i,(label,val) in enumerate(lf): ws.cell(fr+i,1,label); ws.cell(fr+i,2,val); ws.cell(fr+i,1).font=Font(name='Arial',size=6,bold=True); ws.cell(fr+i,2).font=Font(name='Arial',size=6); ws.cell(fr+i,1).border=border; ws.cell(fr+i,2).border=border
    for i,(label,val) in enumerate(rf): ws.cell(fr+i,5,label); ws.cell(fr+i,6,val); ws.cell(fr+i,5).font=Font(name='Arial',size=6,bold=True); ws.cell(fr+i,6).font=Font(name='Arial',size=6); ws.cell(fr+i,5).border=border; ws.cell(fr+i,6).border=border
    ws.print_area=f'A1:{get_column_letter(last_col)}{fr+5}'; return wb
@app.post('/api/bom/export')
async def export(payload:dict):
    wb=export_book(payload); out=io.BytesIO(); wb.save(out); out.seek(0); return StreamingResponse(out,media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',headers={'Content-Disposition':'attachment; filename=ELEXORA_BOM.xlsx'})
