import json
import os
import re
from html import escape
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlparse

from fastapi import FastAPI, Query, Request, Form
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware

from azure.ai.projects import AIProjectClient
from azure.ai.agents import AgentsClient
from azure.ai.agents.models import BingGroundingTool
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

load_dotenv()

CITATION_PATTERN = re.compile(r"【\d+:\d+†source】")
FILTER_CONFIG_PATH = Path(__file__).resolve().parent / "config" / "domain-filters.json"
FilterMode = Literal["whitelist", "blacklist"]
PUBLIC_RESULT_FIELDS = {
    "query",
    "agent_id",
    "thread_id",
    "run_id",
    "run_status",
    "status",
    "error",
    "assistant_response",
    "citations",
}


def normalize_domain(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Domain entries must be non-empty strings.")

    candidate = value.strip().lower()
    if "://" not in candidate and any(character in candidate for character in "/?#"):
        raise ValueError(f"Invalid domain entry: {value}")

    parsed = urlparse(candidate if "://" in candidate else f"//{candidate}")
    hostname = (parsed.hostname or "").rstrip(".").lower()
    if not hostname or "*" in hostname:
        raise ValueError(f"Invalid domain entry: {value}")
    return hostname


def load_domain_filters(path: Path = FILTER_CONFIG_PATH) -> dict[str, list[str]]:
    with path.open(encoding="utf-8") as config_file:
        config = json.load(config_file)

    if (
        not isinstance(config, dict)
        or not isinstance(config.get("whitelist"), list)
        or not isinstance(config.get("blacklist"), list)
    ):
        raise ValueError(
            'Domain filter configuration must contain "whitelist" and "blacklist" arrays.'
        )

    return {
        "whitelist": list(dict.fromkeys(
            normalize_domain(domain) for domain in config["whitelist"]
        )),
        "blacklist": list(dict.fromkeys(
            normalize_domain(domain) for domain in config["blacklist"]
        )),
    }


def domain_matches(hostname: str, configured_domain: str) -> bool:
    normalized_hostname = hostname.rstrip(".").lower()
    return (
        normalized_hostname == configured_domain
        or normalized_hostname.endswith(f".{configured_domain}")
    )


def citation_is_allowed(url: str, domains: list[str], mode: FilterMode) -> bool:
    if not domains:
        return True

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"Invalid citation URL: {url}")

    matches = any(
        domain_matches(parsed.hostname, domain)
        for domain in domains
    )
    return matches if mode == "whitelist" else not matches


def segment_sentences(summary: str) -> list[str]:
    segments = re.findall(
        r""".*?(?:[.!?]+(?:["')\]]+)?(?=\s|$)|\n+|$)""",
        summary,
        flags=re.DOTALL,
    )
    return [segment for segment in segments if segment]


def filter_search_result(
    search_result: dict,
    mode: FilterMode,
    config: dict[str, list[str]] | None = None,
) -> dict:
    if mode not in {"whitelist", "blacklist"}:
        raise ValueError(f"Unsupported filter mode: {mode}")

    filtered_result = {
        key: value
        for key, value in search_result.items()
        if key in PUBLIC_RESULT_FIELDS
    }
    filtered_result["filter_mode"] = mode

    summary = filtered_result.get("assistant_response")
    citations = filtered_result.get("citations")
    if not isinstance(summary, str) or not isinstance(citations, list):
        return filtered_result

    domain_config = config if config is not None else load_domain_filters()
    domains = domain_config[mode]
    filtered_result["configured_domain_count"] = len(domains)
    marker_to_citation = {}
    citation_index = 0
    for marker in CITATION_PATTERN.findall(summary):
        if marker not in marker_to_citation:
            if citation_index >= len(citations):
                raise ValueError(f"No citation metadata was found for marker {marker}.")
            marker_to_citation[marker] = citations[citation_index]
            citation_index += 1

    if not marker_to_citation and citations and domains:
        allowed_citations = [
            citation
            for citation in citations
            if citation_is_allowed(citation.get("url", ""), domains, mode)
        ]
        if len(allowed_citations) != len(citations):
            filtered_result["assistant_response"] = ""
            filtered_result["citations"] = []
            filtered_result["removed_sentence_count"] = 1
            return filtered_result

    retained_citations = {}
    retained_sentences = []
    removed_sentence_count = 0
    for sentence in segment_sentences(summary):
        markers = CITATION_PATTERN.findall(sentence)
        rejected = any(
            not citation_is_allowed(
                marker_to_citation[marker].get("url", ""),
                domains,
                mode,
            )
            for marker in markers
        )
        if rejected:
            removed_sentence_count += 1
            continue

        retained_sentences.append(sentence)
        for marker in markers:
            retained_citations.setdefault(marker, marker_to_citation[marker])

    filtered_result["assistant_response"] = "".join(retained_sentences).strip()
    filtered_result["citations"] = list(retained_citations.values())
    filtered_result["removed_sentence_count"] = removed_sentence_count
    return filtered_result


def format_result_for_display(search_result: dict) -> dict:
    summary = search_result.get("assistant_response") or ""
    citations = search_result.get("citations", [])
    formatted_response = escape(summary)
    seen_citations = {}

    for marker in CITATION_PATTERN.findall(formatted_response):
        if marker not in seen_citations:
            seen_citations[marker] = len(seen_citations) + 1

    for marker, number in seen_citations.items():
        if number <= len(citations):
            citation_url = escape(citations[number - 1]["url"], quote=True)
            replacement = (
                f'<sup><a href="{citation_url}" target="_blank" '
                f'rel="noopener noreferrer" class="citation-link">[{number}]</a></sup>'
            )
            formatted_response = formatted_response.replace(marker, replacement)

    return {
        "summary": formatted_response,
        "citations": citations,
        "removed_sentence_count": search_result.get("removed_sentence_count", 0),
        "configured_domain_count": search_result.get("configured_domain_count", 0),
    }


# Create the FastAPI application with optional metadata
app = FastAPI(
    title="My Search API",
    description="An example FastAPI application with a /search endpoint, complete with automatic Swagger docs at /docs.",
    version="1.0.0"
)

# Set up Jinja2 templates
templates = Jinja2Templates(directory="templates")

# Optionally allow CORS for local dev
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Home page: GET shows form, POST processes search
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse(request, "index.html", {
        "request": request,
        "unfiltered_results": None,
        "filtered_results": None,
        "raw_json": None,
        "query": "",
        "filter_mode": "whitelist",
    })

@app.post("/", response_class=HTMLResponse)
async def home_post(
    request: Request,
    query: str = Form(...),
    use_blacklist: bool = Form(False),
):
    filter_mode: FilterMode = "blacklist" if use_blacklist else "whitelist"
    unfiltered_result = await run_agent_search(query)
    search_result = filter_search_result(unfiltered_result, filter_mode)
    search_result = {
        key: value
        for key, value in search_result.items()
        if key in PUBLIC_RESULT_FIELDS
        or key in {
            "filter_mode",
            "removed_sentence_count",
            "configured_domain_count",
        }
    }
    # Parse results for display
    results = []
    raw_json = None
    formatted_response = ""
    citations_list = []
    if isinstance(search_result, dict):
        summary = search_result.get("assistant_response") or ""
        citations_list = search_result.get("citations", [])
        
        # Format the response with citation exponents (only for display)
        formatted_response = escape(summary)
        if citations_list:
            # Replace Azure citation markers like 【3:0†source】 with clickable superscripts
            # Pattern matches 【number:number†source】
            citation_pattern = r'【(\d+):(\d+)†source】'
            
            # Build a map of citation markers to citation index
            matches = re.findall(citation_pattern, formatted_response)
            seen_citations = {}
            citation_counter = 1
            
            for doc_id, ann_id in matches:
                marker = f'【{doc_id}:{ann_id}†source】'
                if marker not in seen_citations:
                    seen_citations[marker] = citation_counter
                    citation_counter += 1
            
            # Replace each citation marker with a superscript link
            for marker, num in seen_citations.items():
                if num <= len(citations_list):
                    citation = citations_list[num - 1]
                    citation_url = escape(citation["url"], quote=True)
                    replacement = (
                        f'<sup><a href="{citation_url}" target="_blank" '
                        f'rel="noopener noreferrer" class="citation-link">[{num}]</a></sup>'
                    )
                    formatted_response = formatted_response.replace(marker, replacement)
        
        results = {
            "summary": formatted_response,
            "citations": citations_list,
            "removed_sentence_count": search_result.get("removed_sentence_count", 0),
            "configured_domain_count": search_result.get(
                "configured_domain_count",
                0,
            ),
        }
        raw_payload = unfiltered_result.get("raw_message", unfiltered_result)
        raw_json = json.dumps(raw_payload, indent=2, default=str)
    
    return templates.TemplateResponse(request, "index.html", {
        "request": request,
        "unfiltered_results": format_result_for_display(unfiltered_result),
        "filtered_results": results,
        "raw_json": raw_json,
        "query": query,
        "filter_mode": filter_mode,
    })


@app.get("/search", summary="Search Endpoint", description="Accepts a query string and returns search results.")
async def search(
    query: Annotated[str, Query(description="Search query")],
    filter_mode: Annotated[
        FilterMode,
        Query(description="Domain filter mode"),
    ] = "whitelist",
):
    raw_result = await run_agent_search(query)
    return filter_search_result(raw_result, filter_mode)


async def run_agent_search(query: str) -> dict:

    """
    Run the Bing-grounded agent and return its unfiltered server-side result.
    
    Args:
        query (str): The search query provided by the user.
        
    Returns:
        dict: A dictionary containing the search results.
    """ 
    # This function is reused for both API and web form
    print("Starting the Bing Grounding AI agent setup process.")  
  
    # Step 0: Validate environment variables  
    print("Step 0: Validating environment variables...")  
    project_conn_str = os.environ.get("PROJECT_CONNECTION_STRING")
    bing_connection_name = os.environ.get("BING_RESOURCE_NAME")
    agent_name = os.environ.get("AGENT_NAME")
    agent_instructions = os.environ.get("AGENT_INSTRUCTIONS")
    agent_llm = os.environ.get("MODEL_DEPLOYMENT_NAME", 'gpt-4.1')
    api_key = os.environ.get("AZURE_OPENAI_API_KEY")

    missing_vars = []
    if not project_conn_str:
        missing_vars.append("PROJECT_CONNECTION_STRING")
    if not bing_connection_name:
        missing_vars.append("BING_RESOURCE_NAME")
    if missing_vars:
        raise EnvironmentError(
            f"Missing environment variable(s): {', '.join(missing_vars)}"
        )
    print("Environment variables validated successfully.")

    try:
        # Step 1: Initialize the AI Project Client with credentials
        print("Step 1: Initializing Azure AI Project Client...")
        
        print("Using DefaultAzureCredential (Azure CLI, Managed Identity, etc.)...")
        credential = DefaultAzureCredential()
        
        project_client = AIProjectClient(  
            credential=credential,  
            endpoint=project_conn_str  
        )  
        print("Azure AI Project Client initialized.")  
  
        with project_client, AgentsClient(
            endpoint=project_conn_str,
            credential=credential
        ) as agents_client:
            print("Step 2: Enabling Bing Grounding Tool...")
            bing_connection = project_client.connections.get(bing_connection_name)
            bing_tool = BingGroundingTool(connection_id=bing_connection.id)

            # # Stronger instructions
            # enforced_instructions = (
            #     "You are a factual assistant. "
            #     "For any question:\n"
            #     "1. Use the Bing grounding tool to search the web.\n"
            #     "2. Cite at least 1–3 source URLs at the end under 'Sources:'.\n"
            #     "If you cannot find data, clearly say so.\n"
            #     "Answer directly; do not start with greetings."
            # )
            # # Optionally override environment instructions:
            # final_instructions = enforced_instructions

            # Look for existing agent only if its instructions match our pattern; else recreate
            agents_list = list(agents_client.list_agents())
            agent = next((a for a in agents_list if a.name == agent_name), None)
            # if agent:
            #     # If existing agent has old, generic instructions, recreate
            #     if getattr(agent, "instructions", "")[:25] not in enforced_instructions[:25]:
            #         print("Existing agent instructions differ; creating a fresh agent.")
            #         agent = None

            if agent is None:
                agent = agents_client.create_agent(
                    model=agent_llm,
                    name=agent_name,
                    instructions=agent_instructions,
                    tools=bing_tool.definitions,
                    headers={"x-ms-enable-preview": "true"},
                    temperature=0,  # reduce small talk
                )
            print(f"Using agent ID: {agent.id}")

            # Step 4: Create thread
            thread = agents_client.threads.create()
            print(f"Thread ID: {thread.id}")

            # Step 5: Add user message - prepend directive to emphasize action
            print("Step 5: Adding user message to the thread...")
            user_message = agents_client.messages.create(
                thread_id=thread.id,
                role="user",
                content=query
            )
            print(f"User message ID: {user_message.id}")

            # Step 6: Run agent (simple wait)
            run = agents_client.runs.create_and_process(thread_id=thread.id, agent_id=agent.id, tool_choice={"type": "bing_grounding"})
            print(f"Run finished with status: {run.status}")

            #wait_seconds = 7  # Slightly longer to allow tool call
            #print(f"Waiting {wait_seconds}s for agent + Bing tool invocation...")
            #time.sleep(wait_seconds)

            # Optional refresh
            # try:
            #     run = agents_client.runs.get(thread_id=thread.id, run_id=run.id)
            #     print(f"Run status after wait: {run.status}")
            # except Exception as e:
            #     print(f"Run refresh failed: {e}")

            if run.status == "failed":
                return {
                    "query": query,
                    "status": run.status,
                    "error": str(getattr(run, "last_error", "Unknown error"))
                }

            # Step 7: Collect messages
            messages_list = list(agents_client.messages.list(thread_id=thread.id))

            # # Debug: Extract any tool call blocks
            # tool_calls_debug = []
            # for m in messages_list:
            #     if getattr(m, "role", None) == "assistant" and getattr(m, "content", None):
            #         for item in m.content:
            #             # Different SDK versions may label tool calls differently
            #             if hasattr(item, "tool_call") or getattr(item, "type", "") == "toolInvocation":
            #                 call_obj = getattr(item, "tool_call", None) or item
            #                 tool_calls_debug.append({
            #                     "tool_name": getattr(call_obj, "name", None),
            #                     "status": getattr(call_obj, "status", None),
            #                     "id": getattr(call_obj, "id", None),
            #                 })

            last_msg = next((m for m in reversed(messages_list) if m.role == "assistant"), None)

            assistant_text = ""
            citations = []
            if last_msg and last_msg.content:
                print(f"Processing assistant message: {last_msg.id}")
                for item in last_msg.content:
                    if 'text' in item and 'value' in item['text'] and item['text']['value']:
                        assistant_text += item['text']['value'] + "\n"

                    # Collect citation annotations
                    if 'text' in item and 'annotations' in item['text']:
                        for ann in item['text']['annotations']:
                            if 'url_citation' in ann:
                                citations.append({
                                    "title": ann['url_citation']['title'],
                                    "url": ann['url_citation']['url']
                                })
            
            # Debug: Print what we collected
            print(f"Found {len(citations)} citations in annotations")
            print(f"Citation markers in text: {assistant_text.count('†source')}")

            assistant_text = assistant_text.strip()
            if not assistant_text:
                print("Assistant produced no factual content; may need longer wait or instructions tweak.")

            raw_message = None
            if last_msg:
                if hasattr(last_msg, "as_dict"):
                    raw_message = last_msg.as_dict()
                elif hasattr(last_msg, "__dict__"):
                    raw_message = last_msg.__dict__
                else:
                    raw_message = {"message": str(last_msg)}

            return {
                "query": query,
                "agent_id": agent.id,
                "thread_id": thread.id,
                "run_id": getattr(run, "id", None),
                "run_status": getattr(run, "status", None),
                "assistant_response": assistant_text or None,
                "citations": citations,
                "raw_message": raw_message,
            }
    except Exception as e:  
        print(f"An error occurred: {e}")
        return {"error": str(e)}