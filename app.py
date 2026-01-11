import asyncio
import re
from dataclasses import dataclass
from typing import List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode

import aiohttp
from bs4 import BeautifulSoup
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI(title="Internal Link Opportunities Finder (Sitemap-Only)")

# -----------------------------
# URL normalization helpers
# -----------------------------
def normalize_url(u: str) -> str:
    """
    Normalize URLs so comparisons are consistent:
    - lower scheme/host
    - remove fragments
    - remove common tracking params
    - normalize trailing slash (keep path, strip single trailing slash except root)
    """
    try:
        u = u.strip()
        p = urlparse(u)
        scheme = (p.scheme or "https").lower()
        netloc = p.netloc.lower()
        path = p.path or "/"
        fragment = ""
        qs = parse_qsl(p.query, keep_blank_values=True)
        drop = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "gclid", "fbclid"}
        qs = [(k, v) for k, v in qs if k.lower() not in drop]
        query = urlencode(qs, doseq=True)

        if path != "/" and path.endswith("/"):
            path = path[:-1]

        return urlunparse((scheme, netloc, path, "", query, fragment))
    except Exception:
        return u.strip()

# -----------------------------
# Text extraction + snippet
# -----------------------------
def extract_visible_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "svg", "canvas", "iframe"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    return text

def find_keyword_snippet(text: str, keyword: str, window: int = 80) -> Optional[str]:
    if not keyword:
        return None
    pattern = re.compile(re.escape(keyword), re.IGNORECASE)
    m = pattern.search(text)
    if not m:
        return None
    start = max(0, m.start() - window)
    end = min(len(text), m.end() + window)
    snippet = text[start:end].strip()
    if start > 0:
        snippet = "… " + snippet
    if end < len(text):
        snippet = snippet + " …"
    return snippet

def extract_links(html: str, base_url: str) -> Set[str]:
    soup = BeautifulSoup(html, "lxml")
    links = set()
    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        if not href:
            continue
        abs_url = urljoin(base_url, href)
        links.add(normalize_url(abs_url))
    return links

# -----------------------------
# Sitemap parsing (index + urlset)
# -----------------------------
def parse_sitemap_xml(xml: str) -> Tuple[List[str], List[str]]:
    """
    Returns (child_sitemaps, urls)
    Supports:
      - <sitemapindex><sitemap><loc>...</loc></sitemap>...
      - <urlset><url><loc>...</loc></url>...
    """
    soup = BeautifulSoup(xml, "xml")
    child_sitemaps = [loc.get_text(strip=True) for sm in soup.find_all("sitemap") for loc in sm.find_all("loc")]
    urls = [loc.get_text(strip=True) for u in soup.find_all("url") for loc in u.find_all("loc")]

    def uniq(seq):
        seen = set()
        out = []
        for x in seq:
            if x and x not in seen:
                seen.add(x)
                out.append(x)
        return out

    return uniq(child_sitemaps), uniq(urls)

async def fetch_text(session: aiohttp.ClientSession, url: str, timeout_s: int = 20) -> Optional[str]:
    try:
        async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=timeout_s),
            headers={"User-Agent": "SitemapLinkOpportunitiesBot/1.0"},
        ) as resp:
            if resp.status != 200:
                return None
            return await resp.text(errors="ignore")
    except Exception:
        return None

async def collect_urls_from_sitemap(
    session: aiohttp.ClientSession,
    sitemap_url: str,
    max_sitemaps: int = 25,
    max_urls: int = 5000,
) -> List[str]:
    queue = [sitemap_url]
    seen_sitemaps = set()
    urls: List[str] = []

    while queue and len(seen_sitemaps) < max_sitemaps and len(urls) < max_urls:
        sm = queue.pop(0)
        if sm in seen_sitemaps:
            continue
        seen_sitemaps.add(sm)

        xml = await fetch_text(session, sm)
        if not xml:
            continue

        child_sitemaps, found_urls = parse_sitemap_xml(xml)

        for c in child_sitemaps:
            if c not in seen_sitemaps and (len(seen_sitemaps) + len(queue)) < max_sitemaps:
                queue.append(c)

        for u in found_urls:
            if len(urls) >= max_urls:
                break
            urls.append(u)

    seen = set()
    out = []
    for u in urls:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out

# -----------------------------
# Core scanning
# -----------------------------
@dataclass
class Opportunity:
    source_url: str
    matched_keyword: str
    snippet: str

async def scan_one_page(
    session: aiohttp.ClientSession,
    page_url: str,
    target_norm: str,
    keywords: List[str],
) -> Optional[Opportunity]:
    html = await fetch_text(session, page_url)
    if not html:
        return None

    links = extract_links(html, page_url)
    if target_norm in links:
        return None

    text = extract_visible_text(html)
    for kw in keywords:
        snip = find_keyword_snippet(text, kw)
        if snip:
            return Opportunity(source_url=page_url, matched_keyword=kw, snippet=snip)
    return None

async def run_scan(
    sitemap_url: str,
    target_url: str,
    keywords_csv: str,
    max_pages: int,
    concurrency: int,
    same_host_only: bool,
):
    keywords = [k.strip() for k in keywords_csv.split(",") if k.strip()]
    if not keywords:
        return {"error": "Provide at least one keyword."}

    target_norm = normalize_url(target_url)
    root_host = urlparse(sitemap_url).netloc.lower()

    async with aiohttp.ClientSession() as session:
        urls = await collect_urls_from_sitemap(session, sitemap_url)

        if same_host_only:
            urls = [u for u in urls if urlparse(u).netloc.lower() == root_host]

        urls = urls[:max_pages]

        sem = asyncio.Semaphore(concurrency)

        async def bounded(u: str):
            async with sem:
                return await scan_one_page(
                    session=session,
                    page_url=u,
                    target_norm=target_norm,
                    keywords=keywords,
                )

        tasks = [bounded(u) for u in urls]
        results = []
        for coro in asyncio.as_completed(tasks):
            op = await coro
            if op:
                results.append(op)

        return {
            "sitemap_url": sitemap_url,
            "target_url": target_url,
            "keywords": keywords,
            "scanned_pages": len(urls),
            "opportunities": [
                {"source_url": r.source_url, "matched_keyword": r.matched_keyword, "snippet": r.snippet}
                for r in results
            ],
        }

# -----------------------------
# Routes
# -----------------------------
@app.get("/", response_class=HTMLResponse)
def home():
    return """
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Internal Link Opportunities (Sitemap-Only)</title>
  <style>
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial; margin: 24px; max-width: 960px; }
    label { display:block; margin-top: 12px; font-weight: 600; }
    input { width: 100%; padding: 10px; font-size: 14px; }
    button { margin-top: 16px; padding: 10px 14px; font-size: 14px; cursor: pointer; }
    table { width:100%; border-collapse: collapse; margin-top: 18px; }
    th, td { border-bottom: 1px solid #ddd; padding: 10px; vertical-align: top; }
    .muted { color:#555; font-size: 13px; }
    .pill { display:inline-block; padding:2px 8px; border:1px solid #ddd; border-radius: 999px; font-size: 12px; }
    pre { white-space: pre-wrap; word-break: break-word; }
  </style>
</head>
<body>
  <h1>Internal Link Opportunities Finder</h1>
  <p class="muted">Provide a sitemap URL. The tool fetches pages, finds keyword mentions, and returns pages that do not already link to your target URL.</p>

  <label>Sitemap URL</label>
  <input id="sitemap" placeholder="https://example.com/sitemap.xml" />

  <label>Target URL (the page you want links to)</label>
  <input id="target" placeholder="https://example.com/your-target-page/" />

  <label>Keywords (comma-separated)</label>
  <input id="keywords" placeholder="e.g., best running shoes, running shoe size, trail shoes" />

  <label>Max pages to scan (default 200)</label>
  <input id="maxPages" placeholder="200" />

  <button id="run">Run scan</button>

  <div id="status" class="muted" style="margin-top:12px;"></div>
  <div id="out"></div>

<script>
const $ = (id) => document.getElementById(id);
function esc(s){ return (s||"").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;"); }

$("run").addEventListener("click", async () => {
  $("status").textContent = "Scanning… (time depends on sitemap size)";
  $("out").innerHTML = "";

  const sitemap = $("sitemap").value.trim();
  const target = $("target").value.trim();
  const keywords = $("keywords").value.trim();
  const maxPages = parseInt($("maxPages").value.trim() || "200", 10);

  const params = new URLSearchParams({
    sitemap_url: sitemap,
    target_url: target,
    keywords: keywords,
    max_pages: String(maxPages),
    same_host_only: "true"
  });

  try {
    const res = await fetch("/api/scan?" + params.toString());
    const data = await res.json();

    if (data.error) {
      $("status").textContent = "Error: " + data.error;
      return;
    }

    $("status").textContent =
      `Scanned ${data.scanned_pages} pages. Found ${data.opportunities.length} opportunities.`;

    if (!data.opportunities.length) {
      $("out").innerHTML = "<p>No opportunities found with the current keywords/settings.</p>";
      return;
    }

    let html = "<table><thead><tr><th>Source page</th><th>Keyword</th><th>Snippet</th></tr></thead><tbody>";
    for (const r of data.opportunities) {
      html += `<tr>
        <td><a href="${esc(r.source_url)}" target="_blank" rel="noreferrer">${esc(r.source_url)}</a></td>
        <td><span class="pill">${esc(r.matched_keyword)}</span></td>
        <td><pre>${esc(r.snippet)}</pre></td>
      </tr>`;
    }
    html += "</tbody></table>";
    $("out").innerHTML = html;

  } catch (e) {
    $("status").textContent = "Request failed. Check the server logs.";
  }
});
</script>
</body>
</html>
"""

@app.get("/api/scan")
async def api_scan(
    sitemap_url: str = Query(...),
    target_url: str = Query(...),
    keywords: str = Query(..., description="Comma-separated"),
    max_pages: int = Query(200, ge=1, le=2000),
    concurrency: int = Query(10, ge=1, le=30),
    same_host_only: bool = Query(True),
):
    if not sitemap_url.lower().startswith(("http://", "https://")):
        return JSONResponse({"error": "sitemap_url must start with http:// or https://"})
    if not target_url.lower().startswith(("http://", "https://")):
        return JSONResponse({"error": "target_url must start with http:// or https://"})

    result = await run_scan(
        sitemap_url=sitemap_url,
        target_url=target_url,
        keywords_csv=keywords,
        max_pages=max_pages,
        concurrency=concurrency,
        same_host_only=same_host_only,
    )
    return JSONResponse(result)
