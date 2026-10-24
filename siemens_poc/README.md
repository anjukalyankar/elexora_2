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
