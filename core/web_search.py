# -*- coding: utf-8 -*-
import os

try:
    from ddgs import DDGS
except Exception:
    DDGS = None


class WebSearch:
    def __init__(self):
        self.enabled = os.getenv("WEB_SEARCH_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
        self.max_results = int(os.getenv("WEB_SEARCH_MAX_RESULTS", "5"))
        self.region = os.getenv("WEB_SEARCH_REGION", "br-pt")

    def search(self, query):
        query = str(query or "").strip()
        if not self.enabled or not query or DDGS is None:
            return []

        try:
            results = DDGS().text(
                query,
                region=self.region,
                safesearch="moderate",
                max_results=self.max_results,
                backend="auto",
            )
        except Exception as exc:
            print(f"[WEB ERRO] {type(exc).__name__}: {exc}")
            return []

        clean = []
        for item in results or []:
            title = str(item.get("title") or "").strip()
            href = str(item.get("href") or "").strip()
            body = str(item.get("body") or "").strip()
            if not title and not body:
                continue
            clean.append({"title": title, "url": href, "snippet": body[:600]})
        return clean

    @staticmethod
    def format_results(results, max_chars=3500):
        if not results:
            return "Nenhum resultado encontrado."

        parts = []
        for index, item in enumerate(results, 1):
            parts.append(
                f"{index}. {item['title']}\n"
                f"URL: {item['url']}\n"
                f"Resumo: {item['snippet']}"
            )

        text = "\n\n".join(parts)
        return text[:max_chars]
