import asyncio
from playwright.async_api import async_playwright
from pathlib import Path

async def capture_page_as_pdf(url, output_path='page_content.pdf'):
    """Capture a full web page as PDF for complete content extraction."""
    async with async_playwright() as pw:
        # Launch browser (some sites require non-headless)
        browser = await pw.chromium.launch(headless=False)
        
        # Use realistic user agent to avoid bot detection
        context = await browser.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            viewport={'width': 1920, 'height': 1080}
        )
        
        page = await context.new_page()
        
        print(f"Navigating to: {url}")
        await page.goto(url, wait_until='networkidle', timeout=60000)
        
        # Wait for content to fully load
        await page.wait_for_timeout(3000)
        
        # Generate PDF with full page content
        await page.pdf(
            path=output_path,
            format='A4',
            print_background=True,
            margin={'top': '0.5in', 'right': '0.5in', 'bottom': '0.5in', 'left': '0.5in'}
        )
        
        print(f"PDF saved to: {output_path}")
        await browser.close()

async def extract_text_from_pdf(pdf_path):
    """Extract text content from PDF using pdfplumber."""
    import pdfplumber
    
    full_text = []
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages, 1):
            text = page.extract_text()
            if text:
                full_text.append(f"--- Page {i} ---\n{text}")
    
    return '\n\n'.join(full_text)

async def main():
    url = 'https://jakegunther.premiersothebysrealty.com/blog/pelican-bay-fees--here-s-exactly-what-you-re-paying-and-what-you-re-actually-getting'
    pdf_path = 'pelican_bay_fees.pdf'
    
    # Step 1: Capture page as PDF
    await capture_page_as_pdf(url, pdf_path)
    
    # Step 2: Extract text from PDF
    print("\nExtracting text from PDF...")
    extracted_text = await extract_text_from_pdf(pdf_path)
    
    # Step 3: Save extracted text
    text_path = 'pelican_bay_fees.txt'
    with open(text_path, 'w', encoding='utf-8') as f:
        f.write(extracted_text)
    
    print(f"\nExtracted text saved to: {text_path}")
    print(f"Total characters: {len(extracted_text)}")
    print("\n" + "="*80)
    print("EXTRACTED CONTENT:")
    print("="*80)
    print(extracted_text)

if __name__ == '__main__':
    asyncio.run(main())
