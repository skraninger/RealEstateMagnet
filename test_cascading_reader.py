import asyncio
from modules.community.tools import read_page

async def test():
    url = 'https://jakegunther.premiersothebysrealty.com/blog/pelican-bay-fees--here-s-exactly-what-you-re-paying-and-what-you-re-actually-getting'
    print(f'Testing cascading page reader for: {url}')
    print('This will try: 1) Static HTTP 2) JS Render 3) PDF capture')
    print()
    
    text = await read_page(url, max_chars=2000)
    
    if text:
        print(f'SUCCESS! Extracted {len(text)} characters')
        print()
        print('First 500 characters:')
        print('=' * 60)
        print(text[:500])
    else:
        print('FAILED - could not extract any content')

asyncio.run(test())
