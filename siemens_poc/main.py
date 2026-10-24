"""Standalone Siemens MLFB lookup POC. Does not import ELEXORA's DB/auth/BOM app."""
from fastapi import FastAPI
from pydantic import BaseModel, Field
from backend.app.siemens_live import lookup_siemens_product

app = FastAPI(title="ELEXORA Siemens MLFB Verification POC", version="0.1.0")

class VerifyRequest(BaseModel):
    mlfb: str = Field(min_length=1, max_length=80)

@app.get("/health")
def health():
    return {"service": "ELEXORA Siemens MLFB POC", "status": "running", "database_required": False}

@app.post("/verify")
def verify(request: VerifyRequest):
    result = lookup_siemens_product(request.mlfb)
    status = result.get("status")
    if status == "product_page_found":
        result["warning"] = False
        result["user_message"] = "MLFB found on Siemens Industry Mall. Technical compatibility with MSLD must still be checked."
    elif status == "invalid_mlfb":
        result["warning"] = True
        result["user_message"] = "MLFB format is not supported by this POC. Check the MLFB extracted from the MSLD."
    elif status == "product_page_found_needs_confirmation":
        result["warning"] = True
        result["user_message"] = "Siemens page was found, but the page's canonical MLFB did not exactly match. Verify this relay before BOM approval."
    else:
        result["warning"] = True
        result["user_message"] = "Could not verify this MLFB on Siemens live pages. This is not proof that the MLFB is invalid; manual verification is required."
    return result
