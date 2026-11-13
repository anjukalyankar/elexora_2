import React, { useEffect, useMemo, useState } from 'react';

const emptyPreview = null;

export default function AdminMlfb({ api }) {
  const [charts, setCharts] = useState([]);
  const [loadingCharts, setLoadingCharts] = useState(true);
  const [file, setFile] = useState(null);
  const [name, setName] = useState('');
  const [family, setFamily] = useState('');
  const [preview, setPreview] = useState(emptyPreview);
  const [extracting, setExtracting] = useState(false);
  const [saving, setSaving] = useState(false);
  const [selectedChart, setSelectedChart] = useState('');
  const [mlfb, setMlfb] = useState('');
  const [validating, setValidating] = useState(false);
  const [validation, setValidation] = useState(null);
  const [buildDescription, setBuildDescription] = useState('');
  const [buildMsldText, setBuildMsldText] = useState('');
  const [buildResult, setBuildResult] = useState(null);
  const [building, setBuilding] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');

  const token = () => localStorage.getItem('elexora_token');
  const headers = () => ({ Authorization: 'Bearer ' + token() });

  async function loadCharts() {
    setLoadingCharts(true);
    setError('');
    try {
      const response = await fetch(api + '/api/admin/selection-charts', { headers: headers() });
      const data = await response.json();
      if (!response.ok) throw Error(data.detail || 'Could not load selection charts.');
      setCharts(data.charts || []);
      if (!selectedChart && data.charts?.length) setSelectedChart(String(data.charts[0].id));
    } catch (err) {
      setError(err.message);
    } finally {
      setLoadingCharts(false);
    }
  }

  useEffect(() => { loadCharts(); }, []);

  const selected = useMemo(
    () => charts.find(chart => String(chart.id) === String(selectedChart)),
    [charts, selectedChart]
  );

  async function extractChart() {
    if (!file) { setError('Choose a PDF or Excel selection chart first.'); return; }
    if (!name.trim() || !family.trim()) { setError('Enter a chart name and equipment family.'); return; }
    setExtracting(true);
    setError('');
    setMessage('');
    setPreview(null);
    setValidation(null);
    try {
      const body = new FormData();
      body.append('file', file);
      const response = await fetch(api + '/api/admin/selection-charts/extract', {
        method: 'POST', headers: headers(), body
      });
      const data = await response.json();
      if (!response.ok) throw Error(data.detail || 'Selection-chart extraction failed.');
      setPreview(data);
    } catch (err) {
      setError(err.message);
    } finally {
      setExtracting(false);
    }
  }

  async function saveChart() {
    if (!preview) return;
    setSaving(true);
    setError('');
    setMessage('');
    try {
      const body = new FormData();
      body.append('name', name);
      body.append('equipment_family', family);
      body.append('file', file);
      const response = await fetch(api + '/api/admin/selection-charts', {
        method: 'POST', headers: headers(), body
      });
      const data = await response.json();
      if (!response.ok) throw Error(data.detail || 'Could not save selection chart.');
      setMessage(data.message || 'Selection chart saved.');
      setPreview(null);
      setFile(null);
      await loadCharts();
      if (data.chart?.id) setSelectedChart(String(data.chart.id));
    } catch (err) {
      setError(err.message);
    } finally {
      setSaving(false);
    }
  }

  async function downloadChart(chartId) {
    setError('');
    try {
      const response = await fetch(api + '/api/admin/selection-charts/' + chartId + '/download', { headers: headers() });
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw Error(data.detail || 'Could not download the original chart.');
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = selected?.source_filename || 'selection-chart';
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      setError(err.message);
    }
  }

  async function validateMlfb() {
    if (!mlfb.trim()) { setError('Enter an MLFB to validate.'); return; }
    setValidating(true);
    setError('');
    setValidation(null);
    try {
      const response = await fetch(api + '/api/admin/mlfb/validate', {
        method: 'POST',
        headers: { ...headers(), 'Content-Type': 'application/json' },
        body: JSON.stringify({ mlfb, chart_id: selectedChart ? Number(selectedChart) : null })
      });
      const data = await response.json();
      if (!response.ok) throw Error(data.detail || 'MLFB validation failed.');
      setValidation(data);
    } catch (err) {
      setError(err.message);
    } finally {
      setValidating(false);
    }
  }

  async function buildMlfb() {
    if (!buildDescription.trim() && !buildMsldText.trim()) {
      setError('Paste the Q10 equipment description or its MSLD text before building.');
      return;
    }
    setBuilding(true);
    setError('');
    setBuildResult(null);
    try {
      const response = await fetch(api + '/api/admin/mlfb/build', {
        method: 'POST',
        headers: { ...headers(), 'Content-Type': 'application/json' },
        body: JSON.stringify({
          equipment_family: '3AH3',
          designation: 'Q10',
          description: buildDescription,
          msld_text: buildMsldText
        })
      });
      const data = await response.json();
      if (!response.ok) throw Error(data.detail || 'MLFB build failed.');
      setBuildResult(data);
    } catch (err) {
      setError(err.message);
    } finally {
      setBuilding(false);
    }
  }

  const statusLabel = value => String(value || 'review_required').replaceAll('_', ' ');

  return <section className="card admin-mlfb-module">
    <div className="bomhead">
      <div><h2>Selection Chart &amp; MLFB Builder</h2>
        <p>Upload a manufacturer selection chart, review extracted content, save a chart version and validate MLFB candidates.</p></div>
      <span className="master-source-tag">Admin-only · BOM source protected</span>
    </div>
    {error && <div className="error">{error}</div>}
    {message && <div className="auth-success">{message}</div>}

    <div className="admin-mlfb-grid">
      <div className="admin-mlfb-panel">
        <h3>1. Upload selection chart</h3>
        <p>Supported: searchable PDF and .xlsx. Maximum file size: 12 MB.</p>
        <label>Chart name<input value={name} onChange={event => setName(event.target.value)} placeholder="e.g. Siemens 7SJ66 selection chart"/></label>
        <label>Equipment family<input value={family} onChange={event => setFamily(event.target.value)} placeholder="e.g. 7SJ66, 3AH3, 3VA"/></label>
        <label className="upload">Choose PDF / Excel chart
          <input type="file" accept=".pdf,.xlsx,application/pdf,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" onChange={event => { setFile(event.target.files?.[0] || null); setPreview(null); setError(''); }}/>
          <span>{file?.name || 'Select a selection chart'}</span>
        </label>
        <div className="master-actions">
          <button onClick={extractChart} disabled={extracting || !file}>{extracting ? 'Extracting…' : 'Extract & Preview'}</button>
          <button className="secondary-button" onClick={() => { setFile(null); setPreview(null); setError(''); }} disabled={extracting}>Clear</button>
        </div>
      </div>

      <div className="admin-mlfb-panel">
        <h3>2. Saved chart library</h3>
        {loadingCharts ? <p>Loading saved charts…</p> : charts.length ? <>
          <label>Select chart
            <select value={selectedChart} onChange={event => { setSelectedChart(event.target.value); setValidation(null); }}>
              {charts.map(chart => <option key={chart.id} value={chart.id}>{chart.name} · {chart.equipment_family}</option>)}
            </select>
          </label>
          {selected && <div className="admin-mlfb-chart-meta">
            <strong>{selected.source_filename}</strong>
            <span>{selected.source_type.toUpperCase()} · {selected.candidate_mlfbs?.length || 0} extracted candidates</span>
            <small>Uploaded by {selected.uploaded_by} · {selected.created_at ? new Date(selected.created_at).toLocaleString() : '—'}</small>
            <button type="button" className="secondary-button" onClick={() => downloadChart(selected.id)}>Download original chart</button>
          </div>}
        </> : <p>No charts saved yet. Upload and save the first chart to begin validation.</p>}
        <button className="secondary-button" onClick={loadCharts} disabled={loadingCharts}>Refresh chart library</button>
      </div>
    </div>

    {preview && <div className="admin-mlfb-preview">
      <div className="bomhead"><div><h3>Extraction preview</h3><p>{preview.source_filename} · {(preview.file_size / 1024).toFixed(1)} KB · SHA-256 recorded</p></div>
        <span className="proposal-status pending">{preview.candidate_count} candidates</span></div>
      <div className="bulk-summary">
        <div><strong>{preview.page_count || 0}</strong><span>PDF pages</span></div>
        <div><strong>{preview.sheet_count || 0}</strong><span>Excel sheets</span></div>
        <div><strong>{preview.extracted_data?.rows?.length || 0}</strong><span>Structured rows</span></div>
        <div><strong>{preview.candidate_count || 0}</strong><span>MLFB candidates</span></div>
      </div>
      {preview.warnings?.map((warning, index) => <div className="bulk-validation warning" key={index}>{warning}</div>)}
      <div className="admin-mlfb-candidates">
        <strong>Detected MLFB candidates</strong>
        {preview.candidate_mlfbs?.length ? <div>{preview.candidate_mlfbs.map(candidate =>
          <button type="button" className="secondary-button" key={candidate} onClick={() => setMlfb(candidate)}>{candidate}</button>
        )}</div> : <p>No candidate MLFB strings were detected. You can still save the extracted source and inspect it manually.</p>}
      </div>
      <details><summary>Extracted text preview</summary><pre>{(preview.extracted_text || '').slice(0, 12000) || 'No selectable text extracted.'}</pre></details>
      <div className="master-actions">
        <button onClick={saveChart} disabled={saving}>{saving ? 'Saving…' : 'Save chart to library'}</button>
        <button className="secondary-button" onClick={() => setPreview(null)} disabled={saving}>Discard preview</button>
      </div>
      <p>Saving stores a versioned source record and extraction metadata only. It does not publish or alter the live BOM master data.</p>
    </div>}

    <div className="admin-mlfb-validator admin-mlfb-builder">
      <h3>3. MLFB builder — Siemens Q10 / 3AH3</h3>
      <p>Uses the existing deterministic 3AH3 selection-chart mapping. Enter the extracted Q10 row and continuation text; unknown chart options will block MLFB generation instead of guessing.</p>
      <label>Q10 equipment row / description
        <textarea rows="3" value={buildDescription} onChange={event => setBuildDescription(event.target.value)} placeholder="11kV, 3AH3, 1250A, 31.5kA/3Sec, 12NO + 12NC, 64 way plug…"/>
      </label>
      <label>MSLD continuation text (motor / closing coil / trip coil)
        <textarea rows="3" value={buildMsldText} onChange={event => setBuildMsldText(event.target.value)} placeholder="MOTOR: 110V DC, C.C: 110V DC, T.C: 110V DC"/>
      </label>
      <div className="master-actions"><button onClick={buildMlfb} disabled={building}>{building ? 'Building…' : 'Build Q10 MLFB preview'}</button><button type="button" className="secondary-button" onClick={() => {setBuildDescription('');setBuildMsldText('');setBuildResult(null);}}>Clear</button></div>
      {buildResult && <div className="admin-mlfb-result">
        <div className="bomhead"><strong>Generated MLFB: {buildResult.mlfb || 'Not generated'}</strong><span className="proposal-status">{statusLabel(buildResult.status)}</span></div>
        <pre>{(buildResult.description_lines || []).join('\n')}</pre>
        {buildResult.validation?.warnings?.map((warning, index) => <div className="bulk-validation warning" key={index}>{warning}</div>)}
        <p>{buildResult.verification_note}</p>
      </div>}
    </div>

    <div className="admin-mlfb-validator">
      <h3>4. MLFB validation</h3>
      <p>Validation reuses the existing 7SJ66 decoder where applicable. Other families are checked against extracted chart candidates and receive a review warning when no exact candidate is found.</p>
      <div className="admin-mlfb-validate-row">
        <label>MLFB<input value={mlfb} onChange={event => setMlfb(event.target.value)} placeholder="Enter or select an MLFB"/></label>
        <button onClick={validateMlfb} disabled={validating}>{validating ? 'Checking…' : 'Validate MLFB'}</button>
      </div>
      {validation && <div className="admin-mlfb-result">
        <div className="bomhead"><strong>{validation.mlfb}</strong><span className="proposal-status">{statusLabel(validation.status)}</span></div>
        <p>{validation.verification_scope}</p>
        {validation.checks?.length > 0 && <div><strong>Checks</strong>{validation.checks.map((item, index) => <div className="admin-mlfb-check" key={index}><b>{item.item}</b><span>{item.status}</span><small>{item.detail}</small></div>)}</div>}
        {validation.errors?.map((item, index) => <div className="bulk-validation error" key={index}>{item}</div>)}
        {validation.warnings?.map((item, index) => <div className="bulk-validation warning" key={index}>{item}</div>)}
        {validation.decoder?.position_validation?.checks?.length > 0 && <details><summary>7SJ66 position-by-position chart checks</summary>
          {validation.decoder.position_validation.checks.map((item, index) => <div className="admin-mlfb-check" key={index}><b>{item.item}</b><span>{item.status}</span><small>{item.detail}</small></div>)}
        </details>}
      </div>}
    </div>
  </section>;
}
