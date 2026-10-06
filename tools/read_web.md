## readweb

Fetch and extract readable content from a web page (articles, blog posts, documentation, etc.)

**Syntax Example:**

<readweb>
https://example.com/article
</readweb>

**Notes:**
- Supply exactly one URL per directive
- Only the readable text content is extracted (navigation, ads, footers, etc. are stripped)
- Results are automatically fed back to you for analysis
- Use this to read news articles, documentation, blog posts, or any web page the user asks about
- If you need media files or your request gets bot blocked, try curl via the runcmd tool as an alternative (if it's enabled)
- This fetches a KNOWN URL. If you instead need to SEARCH the web (you don't have a specific URL), use the `<websearch query="...">` tool instead — never use `<readweb>` for open-ended web searches