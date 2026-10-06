"""Vision-driven browser agent for accessing web pages with CAPTCHA handling.

Uses Playwright with a visible (non-headless) browser controlled by the local
multimodal LLM. The agent:

1. Launches Chrome with headless=False (visible window)
2. Navigates to the target URL
3. Takes a screenshot and sends it to the LLM
4. LLM returns a BrowserAction (click CAPTCHA, wait, extract content, etc.)
5. Agent executes the action via Playwright
6. Loops until LLM signals "done" or "give_up" (max 10 steps)
7. Saves page as PDF, extracts text via pdfplumber
8. Returns BrowserExtractResult with content, PDF path, and status

This allows the LLM to autonomously handle Cloudflare CAPTCHAs and other
bot-detection challenges by visually identifying and clicking checkboxes.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from .models import BrowserAction, BrowserExtractResult

logger = logging.getLogger(__name__)


DEFAULT_PAGES_DIR = Path("data/research_pages")
DEFAULT_MAX_STEPS = 10
DEFAULT_VIEWPORT = {"width": 1280, "height": 900}
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _sanitize_domain(url: str) -> str:
    """Extract domain from URL and sanitize for filesystem."""
    domain = urlparse(url).netloc
    return domain.replace(".", "_").replace(":", "_").replace("/", "_")


def _generate_pdf_path(url: str, community_slug: Optional[str], pages_dir: Path) -> Path:
    """Generate a unique PDF path for the given URL."""
    domain = _sanitize_domain(url)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug_part = community_slug or "unknown"
    filename = f"{timestamp}_{slug_part}.pdf"
    path = pages_dir / domain / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _generate_screenshot_path(url: str, community_slug: Optional[str], pages_dir: Path) -> Path:
    """Generate a unique screenshot path for the given URL."""
    domain = _sanitize_domain(url)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug_part = community_slug or "unknown"
    filename = f"{timestamp}_{slug_part}.png"
    path = pages_dir / domain / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _classify_failure(http_status: Optional[int], page_content: Optional[str], error_msg: Optional[str]) -> tuple[Optional[str], bool]:
    """Classify a failure into a category and determine if it's retryable.
    
    Returns:
        (failure_category, is_retryable)
    """
    content = (page_content or "").lower()
    error = (error_msg or "").lower()
    combined = content + " " + error
    
    # Cloudflare
    if any(phrase in combined for phrase in ["cloudflare", "cf-challenge", "attention required", "checking your browser"]):
        return "cloudflare_block", False
    
    # Bot detection
    if any(phrase in combined for phrase in ["access denied", "blocked", "bot detection", "automated access"]):
        return "bot_detection", False
    
    # Auth required
    if http_status == 401 or any(phrase in combined for phrase in ["login", "sign in", "authentication required", "please log in"]):
        return "auth_required", False
    
    # Not found
    if http_status == 404 or any(phrase in combined for phrase in ["not found", "page doesn't exist", "404"]):
        return "not_found", False
    
    # Rate limited
    if http_status == 429 or "too many requests" in combined or "rate limit" in combined:
        return "rate_limited", True
    
    # Geo-blocked (check before forbidden as it may return 403)
    if any(phrase in combined for phrase in ["not available in your region", "geo blocked", "access restricted"]):
        return "geo_blocked", False
    
    # Forbidden
    if http_status == 403:
        return "forbidden", False
    
    # Bad request
    if http_status == 400:
        return "bad_request", False
    
    # Timeout
    if "timeout" in combined:
        return "timeout", True
    
    # Network error
    if any(phrase in combined for phrase in ["dns", "connection", "network", "unreachable"]):
        return "network_error", False
    
    # Server error
    if http_status and http_status >= 500:
        return "unknown", True
    
    return "unknown", False


async def _analyze_screenshot_with_llm(
    screenshot_base64: str,
    url: str,
    community_slug: Optional[str],
    step_number: int,
    viewport: dict[str, int],
    model_name: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> BrowserAction:
    """Send screenshot to LLM and get back a BrowserAction.
    
    Args:
        screenshot_base64: Base64-encoded PNG screenshot
        url: The URL being accessed
        community_slug: Which community this relates to (for context)
        step_number: Current step in the loop (1-indexed)
        viewport: Viewport dimensions for coordinate reference
        model_name: LLM model name
        base_url: LLM API base URL
        api_key: LLM API key
    
    Returns:
        BrowserAction from the LLM
    """
    from pydantic_ai import Agent
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider
    
    model_name = model_name or os.environ.get("MODEL_NAME", "qwen3.8-27b")
    base_url = base_url or os.environ.get("MODEL_BASE_URL", "http://localhost:8080/v1")
    api_key = api_key or os.environ.get("MODEL_API_KEY", "local")
    
    model = OpenAIChatModel(
        model_name,
        provider=OpenAIProvider(base_url=base_url, api_key=api_key),
    )
    
    system_prompt = f"""\
You are browsing a web page in a real Chrome browser (visible window).
You see a screenshot of the current page state.

Target URL: {url}
Community: {community_slug or 'not specified'}
Step: {step_number}

Screenshot dimensions: {viewport['width']}x{viewport['height']} pixels.
Coordinates are relative to the top-left corner of the screenshot.

Your goal: Extract useful content about the community from this page.

IMPORTANT RULES:
1. If you see a CAPTCHA checkbox ("I'm not a robot", Cloudflare challenge, or similar), 
   click it using captcha_click with the coordinates of the checkbox.
2. If you see a "Checking your browser..." or waiting message, use wait with 5 seconds.
3. If the page is loaded and readable, extract the text content and signal "done" with 
   the full page text in page_content.
4. If you see a modal/popup blocking content, click to dismiss it using click.
5. If the page hasn't loaded yet, use wait with 2-3 seconds.
6. If you cannot proceed after trying CAPTCHA at least 3 times, signal "give_up" with 
   an error_description explaining why.
7. Never fabricate content - only extract what you actually see on the page.

When signaling "done", include:
- page_content: The full text content you extracted from the page
- summary: A brief 1-2 sentence summary of what the page is about
- reasoning: Why you're signaling done

When signaling "give_up", include:
- error_description: What went wrong or what's blocking you
- reasoning: Why you're giving up
"""
    
    agent = Agent(
        model,
        output_type=BrowserAction,
        system_prompt=system_prompt,
    )
    
    image_url = f"data:image/png;base64,{screenshot_base64}"
    
    prompt = f"Here is the current page state (step {step_number}). What action should I take?"
    
    try:
        result = await agent.run(
            [
                {"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ]},
            ],
            model_settings={"max_tokens": 4096},
        )
        return result.output
    except Exception as exc:
        logger.error("LLM analysis failed: %s", exc)
        return BrowserAction(
            action="give_up",
            error_description=f"LLM analysis failed: {exc}",
            reasoning="Exception during LLM call",
        )


async def _execute_action(
    page: Any,
    action: BrowserAction,
    viewport: dict[str, int],
) -> None:
    """Execute a BrowserAction via Playwright.
    
    Args:
        page: Playwright page object
        action: The action to execute
        viewport: Viewport dimensions for coordinate mapping
    """
    if action.action == "click" or action.action == "captcha_click":
        if action.x is not None and action.y is not None:
            logger.info("Clicking at (%d, %d): %s", action.x, action.y, action.reasoning)
            await page.mouse.click(action.x, action.y)
        else:
            logger.warning("Click action missing coordinates")
    
    elif action.action == "type":
        if action.selector and action.text:
            logger.info("Typing into %s: %s", action.selector, action.reasoning)
            await page.fill(action.selector, action.text)
        elif action.x is not None and action.y is not None and action.text:
            logger.info("Clicking at (%d, %d) and typing: %s", action.x, action.y, action.reasoning)
            await page.mouse.click(action.x, action.y)
            await page.keyboard.type(action.text)
        else:
            logger.warning("Type action missing selector/text or coordinates")
    
    elif action.action == "scroll":
        direction = action.direction or "down"
        scroll_amount = 500 if direction == "down" else -500
        logger.info("Scrolling %s: %s", direction, action.reasoning)
        await page.mouse.wheel(0, scroll_amount)
    
    elif action.action == "wait":
        seconds = action.seconds or 3.0
        logger.info("Waiting %.1f seconds: %s", seconds, action.reasoning)
        await asyncio.sleep(seconds)
    
    elif action.action == "done":
        logger.info("LLM signaled done: %s", action.reasoning)
    
    elif action.action == "give_up":
        logger.info("LLM signaled give_up: %s", action.reasoning)
    
    else:
        logger.warning("Unknown action: %s", action.action)


async def access_and_extract(
    url: str,
    community_slug: Optional[str] = None,
    max_steps: int = DEFAULT_MAX_STEPS,
    pages_dir: Path = DEFAULT_PAGES_DIR,
    model_name: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> BrowserExtractResult:
    """Access a URL via vision-driven browser agent and extract content.
    
    1. Launch visible Chrome (headless=False)
    2. Navigate to URL
    3. Loop: screenshot → LLM vision → execute action → repeat
    4. When LLM signals "done" or max_steps reached:
       - Save PDF
       - Extract text via pdfplumber
       - Return result
    5. On failure: classify and return error details
    
    Args:
        url: The URL to access
        community_slug: Which community this relates to (optional)
        max_steps: Maximum vision loop iterations (default: 10)
        pages_dir: Directory to save PDFs/screenshots
        model_name: LLM model name
        base_url: LLM API base URL
        api_key: LLM API key
    
    Returns:
        BrowserExtractResult with content, PDF path, status, and metadata
    """
    from playwright.async_api import async_playwright
    
    started = time.monotonic()
    viewport = DEFAULT_VIEWPORT.copy()
    pdf_path: Optional[Path] = None
    screenshot_path: Optional[Path] = None
    page_content: Optional[str] = None
    page_title: Optional[str] = None
    http_status: Optional[int] = None
    error_msg: Optional[str] = None
    captcha_detected = False
    captcha_solved = False
    steps_taken = 0
    
    try:
        async with async_playwright() as pw:
            # Launch visible browser
            browser = await pw.chromium.launch(
                headless=False,
                args=['--disable-blink-features=AutomationControlled'],
            )
            
            try:
                context = await browser.new_context(
                    user_agent=DEFAULT_USER_AGENT,
                    viewport=viewport,
                    locale='en-US',
                )
                page = await context.new_page()
                
                # Navigate to URL
                logger.info("Navigating to %s", url)
                response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                http_status = response.status if response else None
                
                # Wait for initial load
                await asyncio.sleep(2)
                
                # Vision loop
                for step in range(1, max_steps + 1):
                    steps_taken = step
                    
                    # Take screenshot
                    screenshot_bytes = await page.screenshot(full_page=True)
                    screenshot_b64 = base64.b64encode(screenshot_bytes).decode("utf-8")
                    
                    # Save screenshot (update path each iteration)
                    screenshot_path = _generate_screenshot_path(url, community_slug, pages_dir)
                    screenshot_path.write_bytes(screenshot_bytes)
                    
                    # Send to LLM
                    action = await _analyze_screenshot_with_llm(
                        screenshot_b64,
                        url,
                        community_slug,
                        step,
                        viewport,
                        model_name,
                        base_url,
                        api_key,
                    )
                    
                    # Check for CAPTCHA
                    if "captcha" in action.reasoning.lower():
                        captcha_detected = True
                    
                    # Execute action
                    await _execute_action(page, action, viewport)
                    
                    # Wait for action to take effect
                    await asyncio.sleep(1.5)
                    
                    # Check if done
                    if action.action == "done":
                        page_content = action.page_content
                        page_title = await page.title()
                        captcha_solved = captcha_detected  # If we got here with captcha_detected, we solved it
                        
                        # Save PDF
                        pdf_path = _generate_pdf_path(url, community_slug, pages_dir)
                        await page.pdf(
                            path=str(pdf_path),
                            format='A4',
                            print_background=True,
                            margin={'top': '0.5in', 'right': '0.5in', 'bottom': '0.5in', 'left': '0.5in'},
                        )
                        
                        # Extract text from PDF
                        try:
                            import pdfplumber
                            with pdfplumber.open(pdf_path) as pdf:
                                text_pages = []
                                for pdf_page in pdf.pages:
                                    text = pdf_page.extract_text()
                                    if text:
                                        text_pages.append(text)
                                page_content = "\n\n".join(text_pages)
                        except Exception as exc:
                            logger.warning("PDF text extraction failed: %s", exc)
                            page_content = action.page_content  # Fall back to LLM extraction
                        
                        break
                    
                    elif action.action == "give_up":
                        error_msg = action.error_description or "LLM gave up"
                        break
                
                else:
                    # Max steps reached
                    error_msg = f"Max steps ({max_steps}) reached without completion"
                
            finally:
                await browser.close()
    
    except httpx.TimeoutException:
        error_msg = "Navigation timeout"
        http_status = None
    except httpx.ConnectError as exc:
        error_msg = f"Network error: {exc}"
        http_status = None
    except Exception as exc:
        error_msg = f"Unexpected error: {exc}"
        logger.exception("Vision browser agent failed for %s", url)
    
    # Determine final status
    if page_content and pdf_path:
        status = "printed"
        failure_category = None
        is_retryable = False
    elif error_msg:
        failure_category, is_retryable = _classify_failure(http_status, None, error_msg)
        if http_status and 400 <= http_status < 500:
            status = "failed_4xx"
        elif http_status and http_status >= 500:
            status = "failed_5xx"
        elif "timeout" in error_msg.lower():
            status = "failed_timeout"
        elif "network" in error_msg.lower():
            status = "failed_network"
        else:
            status = "blocked"
    else:
        status = "blocked"
        failure_category = "unknown"
        is_retryable = False
        error_msg = "Unknown failure"
    
    elapsed = time.monotonic() - started
    
    return BrowserExtractResult(
        url=url,
        success=(status == "printed"),
        status=status,
        page_title=page_title,
        page_content=page_content,
        content_length=len(page_content or ""),
        pdf_path=str(pdf_path) if pdf_path else None,
        screenshot_path=str(screenshot_path) if screenshot_path else None,
        http_status=http_status,
        failure_category=failure_category,
        failure_detail=error_msg,
        is_retryable=is_retryable,
        captcha_detected=captcha_detected,
        captcha_solved=captcha_solved,
        steps_taken=steps_taken,
        elapsed_seconds=elapsed,
    )
