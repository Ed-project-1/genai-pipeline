"""
ingestion/handler.py
────────────────────
AWS Lambda function triggered by S3 PUT events.
Parses uploaded documents (TXT, PDF, DOCX), chunks them with LangChain,
generates embeddings via Amazon Titan Embeddings V2 on Bedrock,
and indexes vectors into Amazon OpenSearch Serverless.
"""

import json
import os
import logging
import boto3
import urllib.parse

from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_aws import BedrockEmbeddings
from langchain_community.vectorstores import OpenSearchVectorSearch
from langchain.schema import Document
from opensearchpy import RequestsHttpConnection
from requests_aws4auth import AWS4Auth

# ── Document parsers ──────────────────────────────────────────────────────────
from pypdf import PdfReader
from docx import Document as DocxDocument
import io

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ── Environment variables (set by CloudFormation / Parameter Store) ───────────
OPENSEARCH_ENDPOINT = os.environ["OPENSEARCH_ENDPOINT"]          # e.g. https://xxx.us-east-1.aoss.amazonaws.com
OPENSEARCH_INDEX    = os.environ.get("OPENSEARCH_INDEX", "meridian-docs")
BEDROCK_REGION      = os.environ.get("BEDROCK_REGION", "us-east-1")
AWS_REGION          = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

# ── Chunking configuration ────────────────────────────────────────────────────
CHUNK_SIZE    = int(os.environ.get("CHUNK_SIZE", "1000"))
CHUNK_OVERLAP = int(os.environ.get("CHUNK_OVERLAP", "200"))

s3_client = boto3.client("s3")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_aws_auth() -> AWS4Auth:
    """Build SigV4 auth for OpenSearch Serverless."""
    credentials = boto3.Session().get_credentials()
    return AWS4Auth(
        refreshable_credentials=credentials,
        region=AWS_REGION,
        service="aoss",   # Amazon OpenSearch Serverless
    )


def parse_document(content: bytes, filename: str) -> str:
    """Extract raw text from TXT, PDF, or DOCX bytes."""
    ext = filename.rsplit(".", 1)[-1].lower()

    if ext == "txt":
        return content.decode("utf-8", errors="replace")

    elif ext == "pdf":
        reader = PdfReader(io.BytesIO(content))
        pages = [page.extract_text() or "" for page in reader.pages]
        return "\n\n".join(pages)

    elif ext in ("docx", "doc"):
        doc = DocxDocument(io.BytesIO(content))
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        return "\n\n".join(paragraphs)

    else:
        logger.warning("Unsupported file type: %s — treating as plain text", ext)
        return content.decode("utf-8", errors="replace")


def extract_metadata(s3_key: str, s3_bucket: str) -> dict:
    """Pull object metadata from S3 (user-defined tags + system metadata)."""
    response = s3_client.head_object(Bucket=s3_bucket, Key=s3_key)
    user_meta = response.get("Metadata", {})
    return {
        "source":          s3_key,
        "bucket":          s3_bucket,
        "filename":        s3_key.split("/")[-1],
        "classification":  user_meta.get("classification", "INTERNAL"),
        "uploaded_by":     user_meta.get("uploaded_by", "unknown"),
        "document_date":   user_meta.get("document_date", ""),
        "content_type":    response.get("ContentType", ""),
    }


def chunk_document(text: str, metadata: dict) -> list[Document]:
    """Split raw text into overlapping chunks, preserving metadata."""
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        separators=["\n\n", "\n", " ", ""],
    )
    chunks = splitter.create_documents(
        texts=[text],
        metadatas=[metadata],
    )
    # Tag each chunk with its position for provenance
    for i, chunk in enumerate(chunks):
        chunk.metadata["chunk_index"] = i
        chunk.metadata["total_chunks"] = len(chunks)
    logger.info("Document split into %d chunks", len(chunks))
    return chunks


def get_vector_store() -> OpenSearchVectorSearch:
    """Return a LangChain OpenSearchVectorSearch client."""
    embeddings = BedrockEmbeddings(
        model_id="amazon.titan-embed-text-v2:0",
        region_name=BEDROCK_REGION,
    )
    return OpenSearchVectorSearch(
        opensearch_url=OPENSEARCH_ENDPOINT,
        index_name=OPENSEARCH_INDEX,
        embedding_function=embeddings,
        http_auth=get_aws_auth(),
        use_ssl=True,
        verify_certs=True,
        connection_class=RequestsHttpConnection,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Lambda handler
# ─────────────────────────────────────────────────────────────────────────────

def handler(event: dict, context) -> dict:
    """
    Entry point. Processes each S3 record in the event batch.
    SQS → Lambda (for large files): event["Records"][0]["body"] contains the
    original S3 event JSON string.
    Direct S3 trigger: event["Records"][0]["s3"] is available directly.
    """
    processed = []
    errors    = []

    for record in event.get("Records", []):
        # Support both direct S3 trigger and SQS-wrapped S3 events
        if "body" in record:
            s3_event = json.loads(record["body"])
            s3_records = s3_event.get("Records", [])
        else:
            s3_records = [record]

        for s3_record in s3_records:
            bucket = s3_record["s3"]["bucket"]["name"]
            key    = urllib.parse.unquote_plus(
                s3_record["s3"]["object"]["key"]
            )

            try:
                logger.info("Processing s3://%s/%s", bucket, key)

                # 1. Download from S3
                obj      = s3_client.get_object(Bucket=bucket, Key=key)
                content  = obj["Body"].read()

                # 2. Parse text
                raw_text = parse_document(content, key)
                if not raw_text.strip():
                    logger.warning("Empty document: %s — skipping", key)
                    continue

                # 3. Extract metadata
                metadata = extract_metadata(key, bucket)

                # 4. Chunk
                chunks = chunk_document(raw_text, metadata)

                # 5. Embed + index into OpenSearch
                vector_store = get_vector_store()
                vector_store.add_documents(chunks)

                logger.info(
                    "Indexed %d chunks from %s into OpenSearch index '%s'",
                    len(chunks), key, OPENSEARCH_INDEX
                )
                processed.append(key)

            except Exception as exc:
                logger.exception("Failed to process %s: %s", key, exc)
                errors.append({"key": key, "error": str(exc)})

    return {
        "statusCode": 200 if not errors else 207,
        "body": json.dumps({
            "processed": processed,
            "errors":    errors,
        }),
    }
