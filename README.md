# Internal Link Opportunities Finder (Sitemap-Only)

A simple SEO tool: give it a sitemap URL, a target URL, and keywords. It will:
1) Pull URLs from the sitemap (supports sitemap index files),
2) Fetch pages,
3) Find pages where your keyword appears,
4) Exclude pages that already link to the target URL,
5) Return a table of internal link opportunities with snippets.

## Quick start (local)

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app:app --reload
```

Open: http://127.0.0.1:8000

## API

`GET /api/scan`

Query params:
- `sitemap_url` (required)
- `target_url` (required)
- `keywords` (required, comma-separated)
- `max_pages` (default 200, max 2000)
- `concurrency` (default 10, max 30)
- `same_host_only` (default true)

Example:

```bash
curl "http://127.0.0.1:8000/api/scan?sitemap_url=https://example.com/sitemap.xml&target_url=https://example.com/money-page/&keywords=running%20shoes,trail%20shoes&max_pages=200"
```

## Notes / limitations

- This MVP does literal substring keyword matching.
- For huge sitemaps, increase `max_pages` carefully.
- If you want to respect `robots.txt`, add a pre-check before fetching pages.
- Some pages may block automated user agents; change the user-agent string if needed.
