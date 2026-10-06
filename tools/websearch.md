## websearch

Search the web for real-time, up-to-date information and return raw search results (titles, URLs, and relevant excerpts) for your analysis.

**Syntax Example:**

<websearch query="latest AI announcements this week">
</websearch>
<websearch query="latest AI announcements this week -anthropic -deepseek">
</websearch>

**Notes:**
- Supply exactly one search query in the `query` attribute per directive
- **Escape any literal quotation marks inside the `query` value as `&quot;`** so they don't prematurely close the attribute and break the directive parser. For example, to search for the exact phrase "Bongo Bob" on the Mac, write:
  `<websearch query="&quot;Bongo Bob&quot; macintosh software download">`
  Do NOT write `<websearch query=""Bongo Bob" macintosh software download">` — the inner `"` characters will truncate the query to empty.
- Use this when you need current information that may not be in your training data (recent news, current prices, live status, up-to-date docs, etc.)
- The search runs on OpenRouter's web search server tool using a fixed model independent of your usual one
- If OpenRouter is unavailable (no API key or call failure), this tool falls back to a keyless DuckDuckGo Lite scrape and returns snippet-only results (titles, URLs, and short excerpts — no full-page content)
- Results (titles, URLs, and excerpt snippets from each source) are automatically fed back to you for analysis — synthesize a grounded, cited answer from them
- Prefer this over `<readweb>` when you do NOT already have a specific URL (websearch finds sources for you); use `<readweb>` only when you already have a URL to fetch
