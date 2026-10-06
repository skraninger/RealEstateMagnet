"""Research tools for the Workstream B agent: web search + human-paced page reads.

The agent browses like a research assistant (see plan §2) — it decides which
pages to open; nothing here bulk-crawls. robots.txt does not apply to this
workstream, but access stays single-request and human-paced.
"""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from .models import GoogleSearchResult

logger = logging.getLogger(__name__)

# Directory for storing CAPTCHA artifacts (screenshots and PDFs) for manual review
CAPTCHA_ARTIFACTS_DIR = Path("data/captcha_artifacts")


def _get_artifacts_dir() -> Path:
    """Get or create the CAPTCHA artifacts directory for storing screenshots and PDFs."""
    CAPTCHA_ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    return CAPTCHA_ARTIFACTS_DIR


def _generate_artifact_name(url: str, artifact_type: str) -> str:
    """Generate a descriptive filename for CAPTCHA artifacts.
    
    Args:
        url: The URL being accessed
        artifact_type: Type of artifact (e.g., 'screenshot', 'pdf')
    
    Returns:
        Filename with timestamp, domain, and artifact type
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    domain = urlparse(url).netloc.replace(".", "_").replace(":", "_")
    return f"{artifact_type}_{timestamp}_{domain}.png" if artifact_type == "screenshot" \
        else f"{artifact_type}_{timestamp}_{domain}.pdf"


@dataclass(frozen=True)
class SearchResult:
    url: str
    title: str
    snippet: str


def web_search(query: str, max_results: int = 8) -> list[SearchResult]:
    """DuckDuckGo web search (no API key). Returns [] on provider errors."""
    from ddgs import DDGS

    try:
        rows = DDGS().text(query, max_results=max_results)
    except Exception as exc:  # rate limits, network hiccups — agent can retry
        logger.warning("web_search failed for %r: %s", query, exc)
        return []
    results: list[SearchResult] = []
    for row in rows or []:
        url = row.get("href") or row.get("url")
        if not url:
            continue
        results.append(
            SearchResult(url=url, title=row.get("title", ""), snippet=row.get("body", ""))
        )
    return results[:max_results]


def _extract_text(html: str, url: str) -> str:
    import trafilatura

    text = trafilatura.extract(html, url=url, include_comments=False, include_tables=True)
    if text and text.strip():
        return text.strip()
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "nav", "footer", "header", "form"]):
        tag.decompose()
    return soup.get_text(" ", strip=True)


def _detect_captcha(text: str, html: str = "") -> dict:
    """Detect if page contains a CAPTCHA challenge.
    
    Returns:
        dict with keys:
            - detected: bool
            - type: str (recaptcha, hcaptcha, cloudflare, generic, none)
            - confidence: float (0.0-1.0)
            - message: str
    """
    indicators = {
        "recaptcha": [
            "recaptcha", "g-recaptcha", "google.com/recaptcha",
            "I'm not a robot", "Please click", "Verify you are human"
        ],
        "hcaptcha": [
            "hcaptcha", "h-captcha", "hcaptcha.com",
            "Please confirm you're human", "Human verification"
        ],
        "cloudflare": [
            "cf-challenge", "cloudflare", "cf_clearance",
            "Checking your browser", "Just a moment", "Enable JavaScript and cookies",
            "Attention Required", "captcha.cf"
        ],
        "generic": [
            "captcha", "CAPTCHA", "security check", "verify you are",
            "prove you're human", "robot check", "bot check",
            "I'm not a robot", "not a robot"
        ]
    }
    
    combined_text = text + " " + html
    combined_lower = combined_text.lower()
    
    # Check each CAPTCHA type
    for captcha_type, phrases in indicators.items():
        matches = []
        for phrase in phrases:
            if phrase.lower() in combined_lower:
                matches.append(phrase)
        
        if matches:
            # Calculate confidence based on number of matches
            confidence = min(1.0, len(matches) * 0.3 + 0.4)
            return {
                "detected": True,
                "type": captcha_type,
                "confidence": confidence,
                "message": f"Detected {captcha_type} CAPTCHA indicators: {', '.join(matches[:3])}"
            }
    
    # Check for very short content (likely a challenge page)
    if len(text.strip()) < 50 and len(text.strip()) > 0:
        return {
            "detected": True,
            "type": "generic",
            "confidence": 0.6,
            "message": f"Suspiciously short content ({len(text)} chars), likely a challenge page"
        }
    
    return {
        "detected": False,
        "type": "none",
        "confidence": 0.0,
        "message": "No CAPTCHA detected"
    }


async def _read_page_js(url: str, max_retries: int = 2) -> str:
    """Read page using Playwright with realistic browser settings to avoid bot detection.
    
    Includes CAPTCHA detection and will take a screenshot for manual review if detected.
    Screenshots are saved to data/captcha_artifacts/ for review.
    """
    from playwright.async_api import async_playwright

    # Use realistic user agent to avoid bot detection
    user_agent = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    
    async with async_playwright() as pw:
        # Launch with anti-detection flags
        browser = await pw.chromium.launch(
            headless=True,
            args=['--disable-blink-features=AutomationControlled']
        )
        try:
            context = await browser.new_context(
                user_agent=user_agent,
                viewport={'width': 1920, 'height': 1080},
                locale='en-US'
            )
            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            await page.wait_for_timeout(2000)  # Wait for JS to render
            html = await page.content()
            text = _extract_text(html, url)
            
            # Check for CAPTCHA
            captcha_check = _detect_captcha(text, html)
            if captcha_check["detected"]:
                logger.warning("CAPTCHA detected on %s: %s", url, captcha_check["message"])
                
                # Save screenshot to artifacts directory for manual review
                artifacts_dir = _get_artifacts_dir()
                screenshot_filename = _generate_artifact_name(url, "screenshot")
                screenshot_path = artifacts_dir / screenshot_filename
                await page.screenshot(path=str(screenshot_path), full_page=True)
                logger.info("CAPTCHA screenshot saved to: %s", screenshot_path)
                
                # Return empty to trigger fallback methods
                return ""
        finally:
            await browser.close()
    
    return text


async def _read_page_pdf(url: str, max_retries: int = 2) -> str:
    """Capture page as PDF using real Chrome browser and extract text with pdfplumber.
    
    This is the most robust method - uses a real (non-headless) browser with anti-detection
    to bypass bot protection, captures the full page as PDF, then extracts text with pdfplumber.
    Slower than other methods but works on sites that block automated access.
    
    Includes CAPTCHA detection - if detected, takes a screenshot for manual review and
    waits for user to solve it before retrying. The browser window stays open so you
    can manually solve the CAPTCHA if needed.
    
    PDFs and screenshots are saved to data/captcha_artifacts/ for review.
    """
    import asyncio
    from pathlib import Path as PathLib
    import pdfplumber
    from playwright.async_api import async_playwright
    
    artifacts_dir = _get_artifacts_dir()
    pdf_filename = _generate_artifact_name(url, "page")
    pdf_path = artifacts_dir / pdf_filename
    
    for attempt in range(max_retries + 1):
        try:
            async with async_playwright() as pw:
                # Launch real browser (not headless) with anti-detection flags
                browser = await pw.chromium.launch(
                    headless=False,
                    args=['--disable-blink-features=AutomationControlled']
                )
                
                # Use realistic browser profile
                context = await browser.new_context(
                    user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    viewport={'width': 1920, 'height': 1080},
                    locale='en-US'
                )
                
                page = await context.new_page()
                
                logger.info("Capturing page as PDF (attempt %d/%d): %s", attempt + 1, max_retries + 1, url)
                await page.goto(url, wait_until='networkidle', timeout=60000)
                await page.wait_for_timeout(3000)  # Wait for content to fully load
                
                # Get page content for CAPTCHA detection
                html = await page.content()
                text = _extract_text(html, url)
                
                # Check for CAPTCHA
                captcha_check = _detect_captcha(text, html)
                if captcha_check["detected"]:
                    logger.warning("CAPTCHA detected on %s: %s", url, captcha_check["message"])
                    
                    # Save screenshot to artifacts directory for manual review
                    screenshot_filename = _generate_artifact_name(url, f"captcha_attempt{attempt}")
                    screenshot_path = artifacts_dir / screenshot_filename
                    await page.screenshot(path=str(screenshot_path), full_page=True)
                    logger.info("CAPTCHA screenshot saved to: %s", screenshot_path)
                    
                    if attempt < max_retries:
                        logger.info(
                            "Browser window is open - you can manually solve the CAPTCHA if needed. "
                            "Waiting 10 seconds before retry (attempt %d/%d)...",
                            attempt + 1, max_retries
                        )
                        await asyncio.sleep(10)
                        await browser.close()
                        continue
                    else:
                        logger.error("CAPTCHA still present after %d attempts, giving up", max_retries)
                        await browser.close()
                        return ""
                
                # No CAPTCHA detected, proceed with PDF generation
                await page.pdf(
                    path=str(pdf_path),
                    format='A4',
                    print_background=True,
                    margin={'top': '0.5in', 'right': '0.5in', 'bottom': '0.5in', 'left': '0.5in'}
                )
                logger.info("PDF saved to: %s", pdf_path)
                
                await browser.close()
            
            # Extract text from PDF using pdfplumber
            logger.info("Extracting text from PDF: %s", pdf_path)
            full_text = []
            with pdfplumber.open(pdf_path) as pdf:
                for i, page in enumerate(pdf.pages, 1):
                    page_text = page.extract_text()
                    if page_text:
                        full_text.append(page_text)
            
            combined_text = '\n\n'.join(full_text)
            logger.info("PDF extraction complete: %d characters from %d pages", len(combined_text), len(full_text))
            
            # Verify the extracted content doesn't contain CAPTCHA indicators
            final_captcha_check = _detect_captcha(combined_text)
            if final_captcha_check["detected"] and final_captcha_check["confidence"] > 0.7:
                logger.warning("Extracted PDF content still contains CAPTCHA indicators: %s", final_captcha_check["message"])
                if attempt < max_retries:
                    logger.info("Retrying with fresh browser session...")
                    continue
                else:
                    logger.error("Could not bypass CAPTCHA after %d attempts", max_retries)
                    return ""
            
            return combined_text
            
        except Exception as exc:
            logger.warning("_read_page_pdf failed for %s: %s", url, exc)
            if attempt < max_retries:
                logger.info("Retrying after error...")
                continue
            else:
                return ""
    
    return ""


@retry(
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=1, min=1, max=5),
    retry=retry_if_exception_type((httpx.TimeoutException, httpx.ConnectError)),
    reraise=True,
)
async def _fetch_page_html(url: str) -> str:
    headers = {
        "User-Agent": os.environ.get(
            "CRAWLER_USER_AGENT", "RealEstateMagnet/0.1 (research assistant)"
        )
    }
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.text


async def read_page(url: str, max_chars: int = 12_000, use_js: bool = False) -> str:
    """Fetch one page and return its main text, truncated to ``max_chars``.

    Cascades through three methods:
    1. Static HTTP fetch (fastest, easily blocked)
    2. Playwright JS render with realistic user agent (medium success rate)
    3. Real Chrome browser → PDF → pdfplumber text extraction (slowest but bypasses bot detection)

    Automatically tries each method in order. If a method fails or returns empty content,
    moves to the next. This ensures maximum compatibility with sites that have bot detection.

    Returns "" on complete failure (the agent decides what to do with an unreadable page).
    """
    # Method 1: Static HTTP fetch
    try:
        html = await _fetch_page_html(url)
        text = _extract_text(html, url)
        if text and len(text) > 100:
            logger.debug("read_page succeeded with static fetch for %s", url)
            return text[:max_chars]
        logger.info("Static fetch returned insufficient content for %s, trying JS render", url)
    except Exception as exc:
        logger.info("Static fetch failed for %s: %s, trying JS render", url, exc)

    # Method 2: Playwright JS render with realistic user agent
    try:
        text = await _read_page_js(url)
        if text and len(text) > 100:
            logger.debug("read_page succeeded with JS render for %s", url)
            return text[:max_chars]
        logger.info("JS render returned insufficient content for %s, trying PDF capture", url)
    except Exception as exc:
        logger.info("JS render failed for %s: %s, trying PDF capture", url, exc)

    # Method 3: Real Chrome browser → PDF → pdfplumber
    try:
        text = await _read_page_pdf(url)
        if text and len(text) > 100:
            logger.debug("read_page succeeded with PDF capture for %s", url)
            return text[:max_chars]
        logger.warning("PDF capture returned insufficient content for %s", url)
    except Exception as exc:
        logger.warning("PDF capture failed for %s: %s", url, exc)

    return ""


async def model_server_health(base_url: str, timeout: float = 3.0) -> bool:
    """Check an OpenAI-compatible local server (llama.cpp exposes /health)."""
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[: -len("/v1")]
    for path in ("/health", "/v1/models"):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(root + path)
                if resp.status_code < 500:
                    return True
        except Exception:
            continue
    return False


async def google_search(
    query: str,
    max_results: int = 10,
    profile_path: str | Path | None = None,
    headless: bool = False,
    delay_seconds: float = 2.0,
) -> list[GoogleSearchResult]:
    """Search Google via browser automation using Playwright.

    Uses Chrome browser to search Google and extract results using JavaScript
    for more robust extraction that adapts to HTML structure changes.

    Args:
        query: Search query string
        max_results: Maximum number of results to return (default: 10)
        profile_path: Path to Chrome user profile directory (not used, kept for compatibility)
        headless: Run browser in headless mode (default: False for debugging)
        delay_seconds: Delay before starting to avoid rate limits (default: 2.0)

    Returns:
        List of GoogleSearchResult objects with title, url, snippet, position

    Note:
        Requires playwright and chromium installed: pip install playwright && playwright install chromium
    """
    import asyncio
    import sys
    from urllib.parse import quote_plus

    from playwright.async_api import async_playwright

    results: list[GoogleSearchResult] = []

    try:
        async with async_playwright() as p:
            # Use a fresh Chrome instance
            browser = await p.chromium.launch(
                headless=headless,
                channel='chrome',  # Use installed Chrome if available
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--disable-dev-shm-usage',
                ],
            )
            context = await browser.new_context(
                viewport={'width': 1280, 'height': 720},
                user_agent=(
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                    '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
                ),
            )
            page = await context.new_page()

            try:
                logger.info('Searching Google: %s', query)
                await asyncio.sleep(delay_seconds)

                search_url = f'https://www.google.com/search?q={quote_plus(query)}'
                await page.goto(search_url, wait_until='networkidle', timeout=30000)
                
                # Wait for results to load
                await asyncio.sleep(2)
                
                # Use JavaScript to extract search results - more robust than CSS selectors
                js_extraction = """
                () => {
                    const results = [];
                    // Try multiple selector strategies
                    const selectors = ['div.yuRUbf', 'div.g', '[data-hveid]', 'div.b8lM7'];
                    let containers = [];
                    
                    for (const sel of selectors) {
                        containers = document.querySelectorAll(sel);
                        if (containers.length > 0) break;
                    }
                    
                    // Fallback: find all h3 elements with links
                    if (containers.length === 0) {
                        const h3s = document.querySelectorAll('h3');
                        for (const h3 of h3s) {
                            const link = h3.closest('a') || h3.querySelector('a') || h3.parentElement.querySelector('a');
                            if (link && link.href) {
                                const snippet = h3.parentElement.textContent.replace(h3.textContent, '').trim().substring(0, 200);
                                results.push({
                                    title: h3.textContent.trim(),
                                    url: link.href,
                                    snippet: snippet
                                });
                            }
                        }
                    } else {
                        // Extract from containers
                        for (let i = 0; i < Math.min(containers.length, 30); i++) {
                            const container = containers[i];
                            const h3 = container.querySelector('h3');
                            if (!h3) continue;
                            
                            const title = h3.textContent.trim();
                            if (!title) continue;
                            
                            // Find link - may be parent, child, or sibling
                            let link = container.querySelector('a[href]');
                            if (!link) link = h3.closest('a');
                            if (!link) link = container.querySelector('a');
                            
                            const url = link ? link.getAttribute('href') : '';
                            
                            // Get snippet
                            let snippet = '';
                            try {
                                const clone = container.cloneNode(true);
                                const h3InClone = clone.querySelector('h3');
                                if (h3InClone) h3InClone.remove();
                                snippet = clone.textContent.trim().substring(0, 300);
                            } catch(e) {
                                snippet = '';
                            }
                            
                            if (title && url) {
                                results.push({title, url, snippet});
                            }
                        }
                    }
                    
                    return results.slice(0, 20);
                }
                """
                
                search_results = await page.evaluate(js_extraction)
                logger.info(f'JavaScript extracted {len(search_results)} results')
                
                for idx, result_data in enumerate(search_results[:max_results], start=1):
                    url = result_data.get('url', '')
                    # Convert relative URLs to absolute
                    if url and url.startswith('/'):
                        url = f'https://www.google.com{url}'
                    
                    if url and url.startswith('http'):
                        results.append(
                            GoogleSearchResult(
                                title=result_data.get('title', '').strip(),
                                url=url,
                                snippet=result_data.get('snippet', '').strip()[:200],
                                position=idx,
                            )
                        )

                logger.info('Found %d Google search results for: %s', len(results), query)

            except Exception as exc:
                logger.error("Google search failed for query '%s': %s", query, exc)
            finally:
                await context.close()
                await browser.close()

    except Exception as exc:
        logger.error('Browser launch failed: %s', exc)

    return results[:max_results]


async def take_screenshot(
    url: str,
    output_path: str | Path | None = None,
    full_page: bool = True,
    wait_ms: int = 2000,
) -> str | None:
    """Take a screenshot of a web page using Playwright.

    Useful for pages where data is rendered in images or complex layouts
    that are hard to extract via text parsing.

    Args:
        url: The URL to screenshot
        output_path: Where to save the PNG. If None, uses a temp file.
        full_page: If True, captures the entire scrollable page
        wait_ms: Milliseconds to wait after page load for JS to render

    Returns:
        Path to the saved screenshot, or None on failure
    """
    from playwright.async_api import async_playwright

    user_agent = os.environ.get(
        "CRAWLER_USER_AGENT", "RealEstateMagnet/0.1 (research assistant)"
    )

    if output_path is None:
        fd, output_path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
    else:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                page = await browser.new_page(
                    user_agent=user_agent,
                    viewport={"width": 1280, "height": 900},
                )
                await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                await page.wait_for_timeout(wait_ms)
                await page.screenshot(path=str(output_path), full_page=full_page)
                logger.info("Screenshot saved: %s", output_path)
                return str(output_path)
            finally:
                await browser.close()
    except Exception as exc:
        logger.warning("take_screenshot failed for %s: %s", url, exc)
        if output_path and Path(output_path).exists():
            Path(output_path).unlink(missing_ok=True)
        return None


async def read_page_with_screenshot(
    url: str,
    max_chars: int = 12_000,
    screenshot_path: str | Path | None = None,
) -> tuple[str, str | None]:
    """Read a page and optionally take a screenshot.

    Returns both the text content and the screenshot path (if requested).
    Useful when you need text extraction AND visual reference.
    """
    text = await read_page(url, max_chars=max_chars, use_js=True)
    screenshot = None
    if screenshot_path is not None or (not text or len(text) < 100):
        screenshot = await take_screenshot(url, output_path=screenshot_path)
    return text, screenshot


async def analyze_screenshot(
    image_path: str | Path,
    prompt: str,
    model_name: str | None = None,
    base_url: str | None = None,
) -> str:
    """Analyze a screenshot using the multimodal LLM.

    Qwen3.8-27B supports vision and can extract information from images
    that may not be available in the HTML (e.g., data rendered in canvas,
    complex layouts, charts, etc.).

    Args:
        image_path: Path to the screenshot image
        prompt: What to extract or analyze from the image
        model_name: Model name (defaults to MODEL_NAME env var)
        base_url: Model server URL (defaults to MODEL_BASE_URL env var)

    Returns:
        LLM's analysis of the image as text
    """
    import base64
    from pathlib import Path as PathLib

    image_path = PathLib(image_path)
    if not image_path.exists():
        logger.warning("Image not found: %s", image_path)
        return ""

    model_name = model_name or os.environ.get("MODEL_NAME", "qwen3.8-27b")
    base_url = base_url or os.environ.get("MODEL_BASE_URL", "http://localhost:8080/v1")

    try:
        with open(image_path, "rb") as f:
            image_data = base64.b64encode(f.read()).decode("utf-8")

        image_url = f"data:image/png;base64,{image_data}"

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {os.environ.get('MODEL_API_KEY', 'local')}",
        }

        payload = {
            "model": model_name,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                }
            ],
            "max_tokens": 2048,
        }

        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            result = resp.json()
            return result["choices"][0]["message"]["content"]

    except Exception as exc:
        logger.warning("analyze_screenshot failed: %s", exc)
        return ""
