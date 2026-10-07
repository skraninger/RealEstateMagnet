"""Test script to verify URL tracking is working."""
import sqlite3

conn = sqlite3.connect('data/communities.db')
cursor = conn.cursor()

# Count total URLs
cursor.execute('SELECT COUNT(*) FROM source_urls')
count = cursor.fetchone()[0]
print(f'Total URLs tracked: {count}')

# Show sample tracked URLs
cursor.execute('''
    SELECT url, status, discovered_by, has_community_data, data_quality_score 
    FROM source_urls 
    LIMIT 10
''')
rows = cursor.fetchall()

print('\nSample tracked URLs:')
for row in rows:
    url = row[0][:60] + '...' if len(row[0]) > 60 else row[0]
    print(f'  URL: {url}')
    print(f'    Status: {row[1]}, Discovered by: {row[2]}')
    print(f'    Has community data: {bool(row[3])}, Quality score: {row[4]}')
    print()

# Count by status
cursor.execute('SELECT status, COUNT(*) FROM source_urls GROUP BY status')
status_counts = cursor.fetchall()
print('\nURLs by status:')
for status, count in status_counts:
    print(f'  {status}: {count}')

conn.close()
