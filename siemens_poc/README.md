# ELEXORA Siemens MLFB POC

Standalone test service for Siemens MLFB lookup plus optional Serper Google-search discovery. It does not import ELEXORA's main FastAPI app, authentication, BOM generation, or database initialization.

## Render settings
- Branch: `poc/siemens-live-mlfb-lookup`
- Root Directory: leave blank
- Build Command: `pip install -r siemens_poc/requirements.txt`
- Start Command: `uvicorn siemens_poc.main:app --host 0.0.0.0 --port $PORT`

No `DATABASE_URL` environment variable is required.

## Enable web search with Serper
1. Create/sign in to a Serper account at https://serper.dev/ and obtain an API key from its dashboard.
2. In the Render dashboard, open the **elexora-siemens-poc** service.
3. Add environment variable `SERPER_API_KEY` with your key value.
4. Save changes and redeploy/restart the POC service.

Keep the key only in Render environment variables. Do not commit it to GitHub or put it in frontend code. Check the current free-query allowance and account terms in the Serper dashboard before relying on a quota.

## Endpoints
- `GET /health` — confirms whether Serper search is configured (does not expose the key).
- `POST /verify` with JSON `{"mlfb":"7SJ6611-5DB90-1FG4+L0R"}` — runs Siemens page lookup and adds search candidates.
- `POST /search` with the same JSON — tests web-search discovery independently.

## How to interpret results
- Serper Google search is a discovery layer. A result/snippet is not proof that the MLFB exists or that its configuration is correct.
- Prefer Siemens official pages, catalogues and manuals. Use distributor pages as secondary evidence only.
- Search returns links, titles, snippets, and a count of Siemens-domain results for manual review.
- If Siemens pages return no readable product details, that is not proof the MLFB is invalid.
- A matching product page verifies product identity only. CT, auxiliary supply, I/O, mounting, communication, and protection compatibility must still be checked against the official selection chart and MSLD.
- Search/authentication/provider errors must produce review warnings, not a false invalid-MLFB result.

The service is a POC only. Production ELEXORA BOM/authentication logic is not changed.


## Technical validation (POC stage)

`POST /verify` optionally accepts two dictionaries:
- `selection_chart_specs`: parameters decoded from the official Siemens selection chart for the submitted MLFB.
- `msld_specs`: corresponding requirements extracted from the MSLD.

Example request body:

```json
{
  "mlfb": "7SJ6611-5DB90-1FG4+L0R",
  "selection_chart_specs": {
    "ct_secondary": "1A",
    "auxiliary_supply": "110V DC",
    "mounting": "flush"
  },
  "msld_specs": {
    "ct_secondary": "1A",
    "auxiliary_supply": "110V DC",
    "mounting": "flush"
  }
}
```

The endpoint returns `technical_assessment.status`:
- `DEVIATION_FOUND`: at least one compared parameter differs; details are in `deviations`.
- `MLFB_RECONFIRMATION_REQUIRED`: specs are missing, or exact official Siemens MLFB evidence is unavailable.
- `VERIFIED`: supplied parameters match and exact official Siemens evidence was found.

Important: this first implementation compares supplied chart/MSLD fields; it does **not yet automatically decode every MLFB position from the PDF selection chart or extract specs from an uploaded MSLD**. Do not mark a relay verified unless those two dictionaries were genuinely derived from the chart and MSLD. Search result counts alone never approve or reject an MLFB.


## 7SJ66 selection-chart decoder

The POC now returns `selection_chart_decode` from `POST /verify`. It decodes the 16 numbered MLFB positions using the uploaded **7SJ66 1 edited.pdf** (EA Products price list, December 2023, printed pages 82-85), including housing/I/O, CT input, auxiliary voltage, construction/display, region/language, system/service port, protection variant, and position-16 functions. It reports unmapped codes and chart compatibility issues rather than silently assuming a code is valid.

The decoder's `comparison_specs` are a first-pass normalized subset for the technical comparison. The request can still supply `msld_specs`, for example:

```json
{
  "mlfb": "7SJ6611-5DB90-1FA0+L0R",
  "msld_specs": {
    "ct_secondary": "1A",
    "protection_variant": "Basic version"
  }
}
```

Important: selection-chart values are the source transcription; exact MSLD value normalization and automated PDF/MSLD extraction are still separate work. For final POC testing, inspect `selection_chart_decode.positions`, `unknown_codes`, `compatibility_issues`, and `technical_assessment` together. A chart decode is not independent proof that a complete MLFB is currently orderable from Siemens.


## MSLD text input and technical comparison

`POST /verify` accepts `msld_text` containing OCR/text already extracted from an MSLD, or `msld_specs` as a structured dictionary. The response includes `msld_extracted_specs`, `selection_chart_decode`, and `technical_assessment`. Extraction is deliberately conservative: only recognized values are populated, and the operator must check them against the original drawing.

Example:
```json
{
  "mlfb": "7SJ6611-5EB90-1FE0-L0R",
  "msld_text": "K55 7SJ6611-5EB90-1FE0-L0R CT SEC: 1A AUX 110V DC 4CT 4VT 16BI+7BO FLUSH MOUNTING IEC 61850 RJ45 50 51 50N 51N 27 59 74TC"
}
```

Comparison handles chart supply ranges and protection capability as a set of available functions rather than requiring every available function to be used. It does not yet OCR PDF/image uploads directly. Exact Siemens orderability remains a separate confirmation; a chart-compatible result without official exact MLFB confirmation remains `MLFB_RECONFIRMATION_REQUIRED`.
