from urllib.parse import urlparse

import requests

from app.config import TAVILY_API_KEY

TAVILY_SEARCH_URL = "https://api.tavily.com/search"


def search_web(query: str) -> list[dict[str, str]]:
    response = requests.post(
        TAVILY_SEARCH_URL,
        timeout=20,
        json={
            "api_key": TAVILY_API_KEY,
            "query": query,
            "search_depth": "basic",
            "max_results": 5,
            "include_answer": False,
        },
    )
    response.raise_for_status()
    try:
        results = response.json()["results"]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("The web search provider returned an invalid response") from exc
    if not isinstance(results, list):
        raise ValueError("The web search provider returned an invalid response")

    normalized = []
    for result in results:
        if not isinstance(result, dict):
            continue
        title = result.get("title")
        url = result.get("url")
        content = result.get("content")
        if not all(isinstance(value, str) for value in (title, url, content)):
            continue
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        normalized.append({
            "title": title[:300],
            "url": url[:2000],
            "content": content[:4000],
        })
        if len(normalized) == 5:
            break
    return normalized
