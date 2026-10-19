"""
Web search skill — free, keyless search results via the `ddgs` library
(DuckDuckGo results, no API key/account needed).

Used to ground open-ended chat replies in current information. Returns
the full result text (title + body + URL) so the LLM can quote and
synthesize rather than just see truncated titles.
"""
from ddgs import DDGS

MAX_RESULTS = 6


def search(query: str, max_results: int = MAX_RESULTS) -> list[dict]:
    """
    Returns up to `max_results` results as
    [{"title": ..., "body": ..., "href": ...}, ...]. Returns an empty
    list (never raises) if the search fails — callers should treat
    that as "no live context available" and fall back to normal chat.
    """
    query = (query or "").strip()
    if not query:
        return []
    try:
        with DDGS() as ddgs:
            return list(ddgs.text(query, max_results=max_results))
    except Exception:
        return []


def build_context_block(query: str, max_results: int = MAX_RESULTS) -> str:
    """
    Runs a search and formats the results as a compact text block
    suitable for dropping into the system prompt as extra context.
    Returns "" if the search returned nothing.
    """
    results = search(query, max_results)
    if not results:
        return ""

    lines = ["Live web search results for reference (may be relevant to the user's message):"]
    for i, r in enumerate(results, 1):
        title = r.get("title", "").strip()
        body = r.get("body", "").strip()
        href = r.get("href", "").strip()
        lines.append(f"[{i}] {title}")
        if body:
            lines.append(f"    {body}")
        if href:
            lines.append(f"    URL: {href}")
    return "\n".join(lines)
