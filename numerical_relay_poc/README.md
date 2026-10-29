# Numerical Relay Hybrid Verification POC

**Isolated experiment only.** This folder is not imported by the production BOM flow and does not change the existing 7SJ66 selection-chart implementation.

## Approach

- **7SJ66:** return `delegated`; continue using the existing, already-working selection-chart path.
- **Other configured families (initially 7SR and 7SD):** use Serper for web discovery, label results from configured official manufacturer domains, and compare supplied MSLD fields against the available official-source search text.
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
- `POST /verify` — evidence-based candidate check.

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
- `mismatch`: reserved for explicit technical contradictions when structured official evidence is added; current snippet-only POC deliberately avoids guessing mismatches.

## Important limitation

Serper returns search results/snippets; it does not guarantee full datasheet content. This first POC is a discovery/evidence gate, not a replacement for official catalog/manual review. A production-grade verifier should fetch and parse the actual official PDF/product pages and track document revision before claiming technical compatibility.
