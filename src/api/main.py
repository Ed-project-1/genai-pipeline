"""
api/main.py
───────────
FastAPI application — deployed as a Lambda Container Image.
Mangum adapter wraps the ASGI app for API Gateway / Lambda Proxy.

Endpoints:
  POST /query          — RAG Q&A
  POST /summarize      — Full-document map-reduce summary
  GET  /health         — Health check
  POST /upload-url     — Generate S3 presigned upload URL
"""

import os
import logging
import uuid
from typing import Optional

import boto3
from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from mangum import Mangum

from src.retrieval.chain import query_with_sources
from src.agents.summarizer import summarize_document

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# ── Config ────────────────────────────────────────────────────────────────────
RAW_BUCKET    = os.environ["RAW_DOCUMENTS_BUCKET"]
AWS_REGION    = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
ALLOWED_EXTS  = {"txt", "pdf", "docx"}

s3_client = boto3.client("s3", region_name=AWS_REGION)

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Project Meridian — Document Intelligence API",
    description="RAG-powered Q&A over company documents using AWS Bedrock + LangChain",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],        # Tighten to Cognito-authenticated domain in prod
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────────────────────────

class QueryRequest(BaseModel):
    question:           str          = Field(..., min_length=3, max_length=2000)
    classification:     str          = Field("INTERNAL", description="User's clearance level")
    conversation_id:    Optional[str] = Field(None, description="Session ID for multi-turn")

class QueryResponse(BaseModel):
    answer:          str
    sources:         list[dict]
    model:           str
    conversation_id: str

class SummarizeRequest(BaseModel):
    s3_key:          str   = Field(..., description="S3 key of document to summarize")
    classification:  str   = Field("INTERNAL")

class SummarizeResponse(BaseModel):
    summary:         str
    key_decisions:   list[str]
    action_items:    list[str]
    model:           str

class UploadUrlRequest(BaseModel):
    filename:        str
    classification:  str   = Field("INTERNAL")
    uploaded_by:     str   = Field("unknown")
    document_date:   str   = Field("")

class UploadUrlResponse(BaseModel):
    upload_url:      str
    s3_key:          str
    expires_in:      int


# ─────────────────────────────────────────────────────────────────────────────
# Auth dependency — validates Cognito JWT (simplified; use aws-jwt-verify in prod)
# ─────────────────────────────────────────────────────────────────────────────

async def get_current_user(
    authorization: Optional[str] = Header(None)
) -> dict:
    """
    In production: decode and verify the Cognito JWT from Authorization header.
    Here we return a mock user for local development.
    """
    if not authorization:
        # During local dev, allow unauthenticated access
        return {"user_id": "dev-user", "classification": "INTERNAL"}

    # TODO: Implement full JWT verification with cognito-jwt-decode
    # token = authorization.replace("Bearer ", "")
    # claims = verify_cognito_token(token)
    # return {"user_id": claims["sub"], "classification": claims.get("custom:classification", "INTERNAL")}

    return {"user_id": "authenticated-user", "classification": "INTERNAL"}


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/health")
def health_check():
    return {"status": "ok", "service": "meridian-api", "version": "1.0.0"}


@app.post("/query", response_model=QueryResponse)
async def query_documents(
    request: QueryRequest,
    current_user: dict = Depends(get_current_user),
):
    """
    Perform a RAG query against the document corpus.
    Returns an LLM-generated answer with source citations.
    """
    conversation_id = request.conversation_id or str(uuid.uuid4())

    # Use the lower of the requested and the user's actual classification
    classification = _min_classification(
        request.classification,
        current_user["classification"],
    )

    try:
        result = query_with_sources(
            question=request.question,
            user_classification=classification,
        )
    except Exception as exc:
        logger.exception("RAG query failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Query failed: {str(exc)}")

    return QueryResponse(
        answer=result["answer"],
        sources=result["sources"],
        model=result["model"],
        conversation_id=conversation_id,
    )


@app.post("/summarize", response_model=SummarizeResponse)
async def summarize(
    request: SummarizeRequest,
    current_user: dict = Depends(get_current_user),
):
    """
    Generate a structured summary of a specific document from S3.
    Extracts key decisions and action items.
    """
    try:
        result = await summarize_document(
            s3_key=request.s3_key,
            bucket=RAW_BUCKET,
            classification=request.classification,
        )
    except Exception as exc:
        logger.exception("Summarization failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Summarization failed: {str(exc)}")

    return SummarizeResponse(**result)


@app.post("/upload-url", response_model=UploadUrlResponse)
async def get_upload_url(
    request: UploadUrlRequest,
    current_user: dict = Depends(get_current_user),
):
    """
    Generate a pre-signed S3 URL for direct browser-to-S3 upload.
    The S3 event trigger on the bucket will kick off ingestion automatically.
    """
    ext = request.filename.rsplit(".", 1)[-1].lower()
    if ext not in ALLOWED_EXTS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '.{ext}'. Allowed: {ALLOWED_EXTS}",
        )

    s3_key = f"uploads/{uuid.uuid4()}/{request.filename}"

    presigned_url = s3_client.generate_presigned_url(
        "put_object",
        Params={
            "Bucket":      RAW_BUCKET,
            "Key":         s3_key,
            "ContentType": f"application/{ext}",
            "Metadata": {
                "classification": request.classification,
                "uploaded_by":    current_user["user_id"],
                "document_date":  request.document_date,
            },
        },
        ExpiresIn=3600,
    )

    return UploadUrlResponse(
        upload_url=presigned_url,
        s3_key=s3_key,
        expires_in=3600,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────────────────────────────────────

def _min_classification(requested: str, actual: str) -> str:
    """Return the lower of two classification levels."""
    order = {"INTERNAL": 0, "CONFIDENTIAL": 1, "SECRET": 2}
    r, a  = order.get(requested, 0), order.get(actual, 0)
    return requested if r <= a else actual


# ── Lambda entry point ────────────────────────────────────────────────────────
handler = Mangum(app, lifespan="off")
