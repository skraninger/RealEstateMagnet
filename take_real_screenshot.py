import asyncio
from playwright.async_api import async_playwright

async def take_real_screenshot():
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
        
        url = 'https://jakegunther.premiersothebysrealty.com/blog/pelican-bay-fees--here-s-exactly-what-you-re-paying-and-what-you-re-actually-getting'
        
        print(f"Navigating to: {url}")
        await page.goto(url, wait_until='networkidle', timeout=60000)
        
        # Wait for content to fully load
        await page.wait_for_timeout(3000)
        
        # Take full-page screenshot
        await page.screenshot(path='pelican_bay_fees.png', full_page=True)
        print('Screenshot saved: pelican_bay_fees.png')
        
        await browser.close()

asyncio.run(take_real_screenshot())
