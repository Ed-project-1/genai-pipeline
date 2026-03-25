"""
tests/test_ingestion.py
"""

import json
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from src.ingestion.handler import parse_document, chunk_document, extract_metadata


# ─────────────────────────────────────────────────────────────────────────────
# parse_document
# ─────────────────────────────────────────────────────────────────────────────

def test_parse_txt():
    content = b"Hello world\nSecond line"
    result = parse_document(content, "test.txt")
    assert "Hello world" in result
    assert "Second line" in result


def test_parse_unsupported_extension():
    content = b"some content"
    result = parse_document(content, "file.xyz")
    assert isinstance(result, str)


# ─────────────────────────────────────────────────────────────────────────────
# chunk_document
# ─────────────────────────────────────────────────────────────────────────────

def test_chunk_document_basic():
    long_text = "This is a sentence. " * 200
    metadata  = {"source": "test.txt", "classification": "INTERNAL"}
    chunks    = chunk_document(long_text, metadata)
    assert len(chunks) > 1
    for i, chunk in enumerate(chunks):
        assert chunk.metadata["chunk_index"] == i
        assert chunk.metadata["total_chunks"] == len(chunks)
        assert "source" in chunk.metadata


def test_chunk_document_preserves_metadata():
    text     = "Short text."
    metadata = {"filename": "memo.txt", "classification": "CONFIDENTIAL"}
    chunks   = chunk_document(text, metadata)
    assert all(c.metadata["classification"] == "CONFIDENTIAL" for c in chunks)


# ─────────────────────────────────────────────────────────────────────────────
# extract_metadata
# ─────────────────────────────────────────────────────────────────────────────

@patch("src.ingestion.handler.s3_client")
def test_extract_metadata(mock_s3):
    mock_s3.head_object.return_value = {
        "Metadata": {
            "classification": "CONFIDENTIAL",
            "uploaded_by":    "alice",
            "document_date":  "2024-09-12",
        },
        "ContentType": "text/plain",
    }
    result = extract_metadata("uploads/memo.txt", "test-bucket")
    assert result["classification"] == "CONFIDENTIAL"
    assert result["uploaded_by"]    == "alice"
    assert result["filename"]       == "memo.txt"


# ─────────────────────────────────────────────────────────────────────────────
# handler — integration-style with mocked AWS
# ─────────────────────────────────────────────────────────────────────────────

@patch("src.ingestion.handler.get_vector_store")
@patch("src.ingestion.handler.s3_client")
def test_handler_success(mock_s3, mock_vectorstore):
    sample_text = "Meeting memo content. " * 50

    mock_s3.get_object.return_value = {
        "Body": MagicMock(read=lambda: sample_text.encode("utf-8"))
    }
    mock_s3.head_object.return_value = {
        "Metadata": {"classification": "INTERNAL"},
        "ContentType": "text/plain",
    }

    mock_vs_instance = MagicMock()
    mock_vectorstore.return_value = mock_vs_instance

    from src.ingestion.handler import handler

    event = {
        "Records": [{
            "s3": {
                "bucket": {"name": "test-bucket"},
                "object": {"key": "uploads/memo.txt"},
            }
        }]
    }

    result = handler(event, None)
    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert "uploads/memo.txt" in body["processed"]
    assert len(body["errors"]) == 0
    mock_vs_instance.add_documents.assert_called_once()
