# ELEXORA 2 — MSLD/DIS to BOM MVP

ELEXORA converts MSLD and DIS documents into a BOM preview and Excel BOM using extracted document data plus a configurable material master.

## MVP flow
1. Create project metadata.
2. Upload MSLD and DIS (PDF/PNG/JPG).
3. Extract text/data from the documents.
4. Match extracted equipment against the material master.
5. Generate a BOM directly — no extracted-data review screen.
6. Preview and export the BOM as Excel.

Engineering rule/selection logic is intentionally outside this MVP. Missing or low-confidence values are surfaced as `REVIEW` rather than invented.

## Run backend
```bash
cd backend
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

## Run frontend
```bash
cd frontend
npm install
npm run dev
```

The frontend expects the API at `http://localhost:8000`.
