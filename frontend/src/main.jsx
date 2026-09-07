import React,{useState} from 'react';
import{createRoot}from'react-dom/client';
import'./style.css';

const API='http://localhost:8000';
const columns=['EQPT. NO.','SPECIFICATION','DESIGNATION','TYPICAL FEEDERS','TOTAL','EQPT QTY','MPD','AMD'];

function Login({onLogin}){const[u,setU]=useState(''),[p,setP]=useState('');return <div className="login"><div className="loginbox"><div className="brand dark">ELEXORA</div><p>Electrical Engineering Automation</p><h1>Sign in</h1><input placeholder="User name" value={u} onChange={e=>setU(e.target.value)}/><input type="password" placeholder="Password" value={p} onChange={e=>setP(e.target.value)}/><button onClick={()=>u&&p&&onLogin(u)}>Sign in</button><small>MVP authentication screen</small></div></div>}

function BomDocument({bom}){const h=bom.header||{};return <div className="bom-document">
  <div className="doc-title">EQUIPMENT LIST <span>Doc.No.: {bom.document_no}</span></div>
  <div className="doc-subtitle">Rev.No.: {bom.revision}, Eff.Dt: 17/07/2026 <span>Created By: {bom.created_by}</span></div>
  <div className="doc-meta">
    <div><b>Item No.</b><span>100</span></div><div><b>Drg. No.</b><span>{h.drawing}</span></div>
    <div><b>Client</b><span>{h.client}</span></div><div><b>PRE.BY</b><span>{h.prep_by}</span></div>
    <div><b>Sales Ref No.</b><span>{h.sales_ref}</span></div><div><b>Qty.</b><span>{bom.qty}</span></div>
    <div><b>DATE</b><span>{new Date().toLocaleDateString('en-GB').replaceAll('/','.')}</span></div><div><b>ESD No.</b><span>{h.esd}</span></div>
    <div className="description"><b>Description</b><span>{bom.description}</span></div><div><b>W.O. No.</b><span>{h.wo}</span></div>
  </div>
  <table className="bom-table"><thead><tr>{columns.map(c=><th key={c}>{c}</th>)}</tr></thead><tbody>{bom.rows?.length?bom.rows.map((r,i)=><tr key={i}><td>{r.sr}</td><td className="spec">{r.specification}</td><td>{r.designation}</td><td>{r.typical_feeders}</td><td>{r.total??''}</td><td>{r.eqpt_qty??''}</td><td></td><td></td></tr>):<tr><td colSpan="8" className="empty">No equipment rows were confidently extracted from the supplied fixed template.</td></tr>}</tbody></table>
  {bom.warnings?.length>0&&<div className="warning">{bom.warnings.join(' ')}</div>}
  <div className="page-no">1 of 1</div>
</div>}

function App(){const[user,setUser]=useState(null);const[form,setForm]=useState({client:'',sales_ref:'',drawing:'',esd:'',wo:'',voltage:''});const[msld,setMsld]=useState(null),[dis,setDis]=useState(null),[bom,setBom]=useState(null),[loading,setLoading]=useState(false),[error,setError]=useState('');
 if(!user)return <Login onLogin={setUser}/>;
 async function generate(){if(!msld||!dis){setError('Please upload both MSLD and DIS.');return}if(!['6.6','11','33'].includes(form.voltage)){setError('Select a voltage level: 6.6 kV, 11 kV or 33 kV.');return}setLoading(true);setError('');const f=new FormData();Object.entries(form).forEach(([k,v])=>f.append(k,v));f.append('prep_by',user);f.append('msld',msld);f.append('dis',dis);try{const r=await fetch(API+'/api/bom/preview',{method:'POST',body:f});if(!r.ok)throw Error('BOM generation failed');setBom(await r.json())}catch(e){setError(e.message+'. Start the FastAPI backend on port 8000.')}finally{setLoading(false)}}
 async function exportBom(){if(!bom)return;const r=await fetch(API+'/api/bom/export',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(bom)});if(!r.ok){setError('Excel export failed.');return}const b=await r.blob();const a=document.createElement('a');a.href=URL.createObjectURL(b);a.download='ELEXORA_BOM.xlsx';a.click();URL.revokeObjectURL(a.href)}
 return <div className="app"><header><div className="brand">ELEXORA</div><div className="tag">Electrical Engineering Automation</div><button className="signout" onClick={()=>setUser(null)}>Sign out</button></header><main><section className="hero"><h1>MSLD + DIS → BOM</h1><p>Fixed-template extraction and BOM generation. The preview follows the supplied BOM format.</p></section><section className="card"><h2>Project & Documents</h2><div className="grid">{[['client','Customer Name'],['sales_ref','Sales Order / Ref No.'],['drawing','Drawing No.'],['esd','ESD No.'],['wo','W.O. No.']].map(([k,l])=><label key={k}>{l}<input value={form[k]} onChange={e=>setForm({...form,[k]:e.target.value})} placeholder={l}/></label>)}<label>Voltage Level<select value={form.voltage} onChange={e=>setForm({...form,voltage:e.target.value})}><option value="">Select voltage</option><option value="6.6">6.6 kV</option><option value="11">11 kV</option><option value="33">33 kV</option></select></label></div><div className="uploads"><label className="upload">MSLD<input type="file" accept=".pdf" onChange={e=>setMsld(e.target.files[0])}/><span>{msld?.name||'Choose fixed-template MSLD PDF'}</span></label><label className="upload">DIS<input type="file" accept=".pdf" onChange={e=>setDis(e.target.files[0])}/><span>{dis?.name||'Choose fixed-template DIS PDF'}</span></label></div>{error&&<div className="error">{error}</div>}<button onClick={generate} disabled={loading}>{loading?'Extracting & Generating…':'Generate BOM Preview'}</button></section>{bom&&<section className="card preview-card"><div className="bomhead"><div><h2>BOM Preview</h2><p>Reference-format preview · {bom.description}</p></div><button onClick={exportBom}>Export Excel</button></div><div className="preview-scroll"><BomDocument bom={bom}/></div></section>}</main></div>}

createRoot(document.getElementById('root')).render(<App/>);
