"""
tests/test_api.py
"""

import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    with patch.dict("os.environ", {
        "OPENSEARCH_ENDPOINT":  "https://mock.us-east-1.aoss.amazonaws.com",
        "OPENSEARCH_INDEX":     "test-index",
        "BEDROCK_REGION":       "us-east-1",
        "RAW_DOCUMENTS_BUCKET": "test-bucket",
        "SESSION_TABLE":        "test-sessions",
        "PRIMARY_MODEL_ID":     "anthropic.claude-3-5-sonnet-20241022-v2:0",
        "FALLBACK_MODEL_ID":    "meta.llama3-1-70b-instruct-v1:0",
        "DAILY_TOKEN_BUDGET":   "100000",
    }):
        from src.api.main import app
        return TestClient(app)


def test_health_check(client):
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["service"] == "meridian-api"


@patch("src.api.main.query_with_sources")
def test_query_endpoint(mock_query, client):
    mock_query.return_value = {
        "answer":  "The budget approved was $2.4M for H2 2024.",
        "sources": [
            {
                "filename":    "meeting_memo.txt",
                "chunk_index": 3,
                "excerpt":     "H2 2024 AI Infrastructure Budget — Approved: $2,400,000",
            }
        ],
        "model": "anthropic.claude-3-5-sonnet-20241022-v2:0",
    }

    response = client.post(
        "/query",
        json={"question": "What was the approved budget?", "classification": "INTERNAL"},
    )
    assert response.status_code == 200
    data = response.json()
    assert "$2.4M" in data["answer"]
    assert len(data["sources"]) == 1
    assert "conversation_id" in data


def test_query_too_short(client):
    response = client.post("/query", json={"question": "Hi"})
    assert response.status_code == 422    # Pydantic validation error (min_length=3)


@patch("src.api.main.s3_client")
def test_upload_url_endpoint(mock_s3, client):
    mock_s3.generate_presigned_url.return_value = "https://s3.presigned.url/example"

    response = client.post(
        "/upload-url",
        json={"filename": "meeting_memo.txt", "classification": "INTERNAL"},
    )
    assert response.status_code == 200
    data = response.json()
    assert "upload_url" in data
    assert data["expires_in"] == 3600


def test_upload_url_bad_extension(client):
    response = client.post(
        "/upload-url",
        json={"filename": "malware.exe"},
    )
    assert response.status_code == 400
    assert "Unsupported file type" in response.json()["detail"]
