"""
agents/summarizer.py
────────────────────
LangChain Agent with custom tools for document summarization.
Uses a map-reduce approach for large documents:
  - Map:    summarize each chunk individually
  - Reduce: synthesize chunk summaries into a final structured output

Also defines reusable LangChain Tools that can be plugged into
AgentExecutor for more complex agentic workflows.
"""

import os
import logging
import json
import boto3
import io

from langchain_aws import ChatBedrock, BedrockEmbeddings
from langchain.chains.summarize import load_summarize_chain
from langchain.schema import Document
from langchain_core.prompts import PromptTemplate
from langchain_core.tools import Tool
from langchain.agents import AgentExecutor, create_react_agent
from langchain_core.prompts import ChatPromptTemplate
from pypdf import PdfReader
from docx import Document as DocxDocument

logger = logging.getLogger(__name__)

BEDROCK_REGION  = os.environ.get("BEDROCK_REGION", "us-east-1")
PRIMARY_MODEL   = os.environ.get("PRIMARY_MODEL_ID", "anthropic.claude-3-5-sonnet-20241022-v2:0")

s3_client = boto3.client("s3")


# ─────────────────────────────────────────────────────────────────────────────
# Map prompt — used per chunk
# ─────────────────────────────────────────────────────────────────────────────

MAP_PROMPT_TEMPLATE = """You are analyzing a section of a business document.
Summarize the following section, preserving all important facts, names, dates,
decisions, and action items. Be concise but complete.

Document section:
{text}

CONCISE SUMMARY:"""

# ─────────────────────────────────────────────────────────────────────────────
# Reduce prompt — used to combine chunk summaries
# ─────────────────────────────────────────────────────────────────────────────

REDUCE_PROMPT_TEMPLATE = """You are synthesizing summaries of sections from a business document
into a final, structured report. Return ONLY valid JSON — no markdown, no preamble.

Chunk summaries:
{text}

Return a JSON object with EXACTLY these keys:
{{
  "summary": "2-3 paragraph executive summary of the entire document",
  "key_decisions": ["decision 1", "decision 2", ...],
  "action_items": ["owner: action (due date)", ...]
}}

JSON:"""


def _get_llm() -> ChatBedrock:
    return ChatBedrock(
        model_id=PRIMARY_MODEL,
        region_name=BEDROCK_REGION,
        model_kwargs={"max_tokens": 4096, "temperature": 0.1},
    )


def _download_and_parse(s3_key: str, bucket: str) -> str:
    """Download from S3 and extract text."""
    obj     = s3_client.get_object(Bucket=bucket, Key=s3_key)
    content = obj["Body"].read()
    ext     = s3_key.rsplit(".", 1)[-1].lower()

    if ext == "pdf":
        reader = PdfReader(io.BytesIO(content))
        return "\n\n".join(p.extract_text() or "" for p in reader.pages)
    elif ext in ("docx", "doc"):
        doc = DocxDocument(io.BytesIO(content))
        return "\n\n".join(p.text for p in doc.paragraphs if p.text.strip())
    else:
        return content.decode("utf-8", errors="replace")


async def summarize_document(
    s3_key: str,
    bucket: str,
    classification: str = "INTERNAL",
) -> dict:
    """
    Summarize a full document using LangChain's map-reduce summarize chain.
    Returns structured JSON with summary, key decisions, and action items.
    """
    from langchain.text_splitter import RecursiveCharacterTextSplitter

    raw_text = _download_and_parse(s3_key, bucket)

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=3000,
        chunk_overlap=300,
    )
    chunks = splitter.create_documents([raw_text])
    logger.info("Summarizing %d chunks from %s", len(chunks), s3_key)

    llm = _get_llm()

    map_prompt    = PromptTemplate(template=MAP_PROMPT_TEMPLATE,    input_variables=["text"])
    reduce_prompt = PromptTemplate(template=REDUCE_PROMPT_TEMPLATE, input_variables=["text"])

    chain = load_summarize_chain(
        llm=llm,
        chain_type="map_reduce",
        map_prompt=map_prompt,
        combine_prompt=reduce_prompt,
        verbose=False,
    )

    raw_output = chain.invoke({"input_documents": chunks})
    output_text = raw_output.get("output_text", "{}")

    # Strip markdown fences if present
    output_text = output_text.strip()
    if output_text.startswith("```"):
        output_text = output_text.split("```")[1]
        if output_text.startswith("json"):
            output_text = output_text[4:]

    try:
        result = json.loads(output_text)
    except json.JSONDecodeError:
        logger.warning("Failed to parse JSON from LLM output; returning raw text")
        result = {
            "summary":       output_text,
            "key_decisions": [],
            "action_items":  [],
        }

    result["model"] = PRIMARY_MODEL
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Custom LangChain Tools (reusable in AgentExecutor)
# ─────────────────────────────────────────────────────────────────────────────

def build_document_search_tool(retriever) -> Tool:
    """Wraps the vector retriever as a LangChain Tool."""
    def search(query: str) -> str:
        docs = retriever.invoke(query)
        if not docs:
            return "No relevant documents found."
        parts = []
        for doc in docs:
            fn  = doc.metadata.get("filename", "unknown")
            idx = doc.metadata.get("chunk_index", "?")
            parts.append(f"[{fn} / chunk {idx}]: {doc.page_content[:500]}")
        return "\n\n".join(parts)

    return Tool(
        name="DocumentSearch",
        func=search,
        description=(
            "Searches the internal document corpus for information relevant to a query. "
            "Input: a natural language query string. "
            "Output: relevant document excerpts with source citations."
        ),
    )


def build_action_item_extractor_tool() -> Tool:
    """Uses the LLM to extract action items from provided text."""
    llm = _get_llm()

    def extract_actions(text: str) -> str:
        prompt = (
            "Extract all action items from the following text. "
            "Format each as: 'Owner: action (due date or TBD)'. "
            "If there are none, return 'No action items found.'\n\n"
            f"Text:\n{text}"
        )
        response = llm.invoke(prompt)
        return response.content

    return Tool(
        name="ActionItemExtractor",
        func=extract_actions,
        description=(
            "Extracts action items, tasks, and owners from a block of meeting text. "
            "Input: raw meeting notes or document text. "
            "Output: a formatted list of action items."
        ),
    )


def build_decision_extractor_tool() -> Tool:
    """Uses the LLM to extract key decisions from provided text."""
    llm = _get_llm()

    def extract_decisions(text: str) -> str:
        prompt = (
            "Extract all key decisions made in the following text. "
            "Format each as a brief, clear statement. "
            "If there are none, return 'No decisions found.'\n\n"
            f"Text:\n{text}"
        )
        response = llm.invoke(prompt)
        return response.content

    return Tool(
        name="DecisionExtractor",
        func=extract_decisions,
        description=(
            "Extracts key decisions from meeting notes or documents. "
            "Input: raw text. Output: a list of decision statements."
        ),
    )


def build_agent(retriever) -> AgentExecutor:
    """
    Build a ReAct agent with all three custom tools.
    Useful for multi-step queries like:
      'Search for the budget discussion, then list all decisions and action items.'
    """
    tools = [
        build_document_search_tool(retriever),
        build_action_item_extractor_tool(),
        build_decision_extractor_tool(),
    ]

    llm = _get_llm()

    # ReAct prompt with tool descriptions
    react_prompt = ChatPromptTemplate.from_messages([
        ("system", """You are Meridian, a document intelligence agent.
You have access to tools to search documents, extract action items, and extract decisions.
Use the tools strategically to answer the user's question thoroughly.
Always cite your sources.

Available tools: {tools}
Tool names: {tool_names}

Use this format:
Thought: <your reasoning>
Action: <tool name>
Action Input: <input to the tool>
Observation: <tool result>
... (repeat as needed)
Thought: I now have enough information.
Final Answer: <your comprehensive answer>
"""),
        ("human", "{input}\n\n{agent_scratchpad}"),
    ])

    agent = create_react_agent(llm=llm, tools=tools, prompt=react_prompt)
    return AgentExecutor(
        agent=agent,
        tools=tools,
        verbose=True,
        max_iterations=5,
        handle_parsing_errors=True,
    )
