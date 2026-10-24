# ELEXORA Siemens MLFB POC

Standalone test service for live Siemens Industry Mall lookup. It does not import ELEXORA's main FastAPI app, authentication, BOM generation, or database initialization.

## Render settings
- Branch: `poc/siemens-live-mlfb-lookup`
- Root Directory: leave blank
- Build Command: `pip install -r siemens_poc/requirements.txt`
- Start Command: `uvicorn siemens_poc.main:app --host 0.0.0.0 --port $PORT`

No `DATABASE_URL` environment variable is required.

## Endpoints
- `GET /health`
- `POST /verify` with JSON `{"mlfb":"7SJ6611-5DB90-1FG4+L0R"}`

Important: a matching Siemens product page verifies that the MLFB is listed, not that its CT, auxiliary supply, I/O, mounting, communications, or protection functions match the MSLD. That comparison is a later integration step. Source unavailability must produce a review warning, not a claim that the MLFB is invalid.
