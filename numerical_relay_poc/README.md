# Numerical Relay Hybrid Verification POC

**Isolated experiment only.** This folder is not imported by the production BOM flow and does not change the existing 7SJ66 selection-chart implementation.

## Approach

- **7SJ66:** return `delegated`; continue using the existing, already-working selection-chart path.
- **Other configured families (initially 7SR and 7SD):** use Serper for discovery, then fetch bounded HTML/PDF content hosted on configured official Siemens domains. Extract text and compare supplied MSLD fields against official-source snippets and parsed document text. Redirects outside Siemens domains are blocked. Per-field responses include an evidence excerpt.
- **Conservative result:** return `insufficient_evidence` if an official result or exact MLFB evidence is missing, or any supplied field cannot be explicitly confirmed. Search snippets are not sufficient for final engineering approval.
- **Future products:** add family prefixes, query templates, official domains, and field aliases to `relay_catalog.json`. New MLFB encoding rules can require a family-specific adapter, but ordinary query/alias changes do not require changing the verifier.
- **No database dependency.** Only the optional `SERPER_API_KEY` environment variable is required for live searches.

## Run locally

From repository root:

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -r numerical_relay_poc/requirements.txt
export SERPER_API_KEY="your-serper-api-key"  # PowerShell: $env:SERPER_API_KEY="..."
uvicorn main:app --app-dir numerical_relay_poc --reload --port 8010
```

Then open `http://127.0.0.1:8010/docs`.

## Endpoints

- `GET /health` — service status and whether Serper is configured.
- `POST /search` — discovery-only search results; not a validation decision.
- `POST /verify` — evidence-based candidate check for one MLFB and supplied MSLD fields.
- `POST /verify-msld` — multipart PDF upload; extracts supported Siemens 7SJ/7SR/7SD relay MLFBs and nearby specifications, then verifies each relay. Upload field name: `file`.

PDF upload example:

```bash
curl -X POST "http://127.0.0.1:8010/verify-msld" \\
  -F "file=@MSLD_2(1).pdf"
```

The endpoint accepts selectable-text PDFs up to 20 MB and 100 pages. Scanned/image-only PDFs return a clear OCR-required response; OCR is not enabled. 7SJ66 remains delegated to the existing selection-chart workflow.

Example request:

```json
{
  "mlfb": "7SR1003-1JA20-2CA0",
  "msld_specs": {
    "ct_secondary": "1A",
    "auxiliary_supply": "110V DC"
  },
  "msld_text": "Optional MSLD context"
}
```

Example is illustrative, not a statement that the sample MLFB is valid.

## Status meanings

- `delegated`: 7SJ66 should be handled by the existing parser/selection-chart flow.
- `candidate_confirmed`: exact MLFB and all submitted fields were seen in official-source search text. This is **not** final engineering approval.
- `insufficient_evidence`: evidence was missing or did not explicitly confirm all supplied fields.
- `mismatch`: an explicit technical contradiction was found beside a relevant field label. Complex protection/hardware contradictions are only detected when values are explicitly stated in local field context.

## Important limitation

The POC now attempts bounded retrieval of official Siemens HTML/PDF pages linked by search results. PDF text extraction can miss scanned/image-only tables; OCR and document-revision/product-variant resolution are not implemented. Parsed official content is a review aid, not final engineering approval.
