"""
retrieval/chain.py
──────────────────
LangChain LCEL RAG pipeline:
  1. Retrieve top-k relevant chunks from OpenSearch using semantic search
  2. Optionally filter by document classification level
  3. Feed context + question to Claude 3.5 Sonnet via Bedrock
  4. Return answer with source citations
"""

import os
import logging
from typing import Optional

import boto3
from langchain_aws import ChatBedrock, BedrockEmbeddings
from langchain_community.vectorstores import OpenSearchVectorSearch
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnablePassthrough, RunnableLambda
from langchain_core.output_parsers import StrOutputParser
from langchain.schema import Document
from opensearchpy import RequestsHttpConnection
from requests_aws4auth import AWS4Auth

logger = logging.getLogger(__name__)

# ── Environment ───────────────────────────────────────────────────────────────
OPENSEARCH_ENDPOINT = os.environ["OPENSEARCH_ENDPOINT"]
OPENSEARCH_INDEX    = os.environ.get("OPENSEARCH_INDEX", "meridian-docs")
BEDROCK_REGION      = os.environ.get("BEDROCK_REGION", "us-east-1")
AWS_REGION          = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
PRIMARY_MODEL_ID    = os.environ.get("PRIMARY_MODEL_ID",  "anthropic.claude-3-5-sonnet-20241022-v2:0")
FALLBACK_MODEL_ID   = os.environ.get("FALLBACK_MODEL_ID", "meta.llama3-1-70b-instruct-v1:0")
TOP_K               = int(os.environ.get("RETRIEVAL_TOP_K", "5"))

# ─────────────────────────────────────────────────────────────────────────────
# System prompt — instructs the LLM on citation format
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are Meridian, an intelligent assistant for internal company documents.

Your role is to answer questions accurately and concisely using ONLY the provided context.

RULES:
1. Answer based solely on the context provided. If the context does not contain enough 
   information, say "I don't have enough information in the provided documents to answer that."
2. Always cite your sources using the format [Source: <filename>, Chunk <chunk_index>].
3. Be precise. Avoid hallucination.
4. For lists of action items, decisions, or dates — present them clearly with bullet points.
5. If the question is about people, decisions, or dates — be specific and quote directly 
   from the source when relevant.

Context:
{context}
"""

USER_PROMPT = "Question: {question}"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_aws_auth() -> AWS4Auth:
    credentials = boto3.Session().get_credentials()
    return AWS4Auth(
        refreshable_credentials=credentials,
        region=AWS_REGION,
        service="aoss",
    )


def _format_docs(docs: list[Document]) -> str:
    """Serialize retrieved chunks into the context string."""
    parts = []
    for doc in docs:
        meta     = doc.metadata
        filename = meta.get("filename", meta.get("source", "unknown"))
        chunk    = meta.get("chunk_index", "?")
        date     = meta.get("document_date", "")
        header   = f"[Source: {filename}, Chunk {chunk}" + (f", Date: {date}" if date else "") + "]"
        parts.append(f"{header}\n{doc.page_content}")
    return "\n\n---\n\n".join(parts)


def _build_opensearch_filter(classification: Optional[str]) -> Optional[dict]:
    """
    Build an OpenSearch bool filter for classification-based access control.
    Users can only see documents at or below their clearance level.
    """
    allowed = {
        "SECRET":       ["INTERNAL", "CONFIDENTIAL", "SECRET"],
        "CONFIDENTIAL": ["INTERNAL", "CONFIDENTIAL"],
        "INTERNAL":     ["INTERNAL"],
    }.get(classification or "INTERNAL", ["INTERNAL"])

    return {
        "bool": {
            "filter": [
                {"terms": {"metadata.classification.keyword": allowed}}
            ]
        }
    }


# ─────────────────────────────────────────────────────────────────────────────
# Chain factory
# ─────────────────────────────────────────────────────────────────────────────

def build_rag_chain(user_classification: str = "INTERNAL"):
    """
    Build and return the LCEL RAG chain.
    Call once per Lambda cold start and cache the result.
    """
    # 1. Embeddings model
    embeddings = BedrockEmbeddings(
        model_id="amazon.titan-embed-text-v2:0",
        region_name=BEDROCK_REGION,
    )

    # 2. Vector store retriever
    vector_store = OpenSearchVectorSearch(
        opensearch_url=OPENSEARCH_ENDPOINT,
        index_name=OPENSEARCH_INDEX,
        embedding_function=embeddings,
        http_auth=_get_aws_auth(),
        use_ssl=True,
        verify_certs=True,
        connection_class=RequestsHttpConnection,
    )

    os_filter = _build_opensearch_filter(user_classification)
    retriever = vector_store.as_retriever(
        search_type="similarity",
        search_kwargs={
            "k":              TOP_K,
            "boolean_filter": os_filter,
        },
    )

    # 3. LLM — Claude 3.5 Sonnet via Bedrock
    llm = ChatBedrock(
        model_id=PRIMARY_MODEL_ID,
        region_name=BEDROCK_REGION,
        model_kwargs={
            "max_tokens":   2048,
            "temperature":  0.1,      # Low temp for factual RAG
            "top_p":        0.9,
        },
    )

    # 4. Prompt
    prompt = ChatPromptTemplate.from_messages([
        ("system", SYSTEM_PROMPT),
        ("human",  USER_PROMPT),
    ])

    # 5. LCEL chain: retrieve → format → prompt → LLM → parse
    chain = (
        {
            "context":  retriever | RunnableLambda(_format_docs),
            "question": RunnablePassthrough(),
        }
        | prompt
        | llm
        | StrOutputParser()
    )

    return chain, retriever


def query_with_sources(
    question: str,
    user_classification: str = "INTERNAL",
) -> dict:
    """
    Run a RAG query and return both the answer and the source documents.
    This is the main function called by the API handler.
    """
    chain, retriever = build_rag_chain(user_classification)

    # Retrieve source documents separately for provenance response
    source_docs = retriever.invoke(question)
    answer      = chain.invoke(question)

    sources = [
        {
            "filename":       doc.metadata.get("filename", "unknown"),
            "chunk_index":    doc.metadata.get("chunk_index"),
            "total_chunks":   doc.metadata.get("total_chunks"),
            "classification": doc.metadata.get("classification"),
            "document_date":  doc.metadata.get("document_date", ""),
            "excerpt":        doc.page_content[:300] + "..." if len(doc.page_content) > 300 else doc.page_content,
        }
        for doc in source_docs
    ]

    return {
        "answer":  answer,
        "sources": sources,
        "model":   PRIMARY_MODEL_ID,
    }
