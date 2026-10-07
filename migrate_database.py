"""Database migration to add missing columns to source_urls table."""
import sqlite3
from pathlib import Path

def migrate_database():
    """Add missing columns to source_urls table."""
    db_path = Path("data/communities.db")
    
    if not db_path.exists():
        print("Database not found, skipping migration")
        return
    
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    
    # Check if source_urls table exists
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='source_urls'")
    if not cursor.fetchone():
        print("source_urls table does not exist, skipping migration")
        conn.close()
        return
    
    # Get existing columns
    cursor.execute("PRAGMA table_info(source_urls)")
    existing_columns = {row[1] for row in cursor.fetchall()}
    
    # Define new columns to add
    new_columns = [
        ("robot_friendly", "BOOLEAN DEFAULT 0"),
        ("robots_txt_checked", "BOOLEAN DEFAULT 0"),
        ("has_community_data", "BOOLEAN DEFAULT 0"),
        ("data_quality_score", "REAL DEFAULT 0.0"),
        ("data_types_found", "TEXT"),
        ("data_summary", "TEXT"),
    ]
    
    added_count = 0
    for col_name, col_type in new_columns:
        if col_name not in existing_columns:
            print(f"Adding column: {col_name} ({col_type})")
            cursor.execute(f"ALTER TABLE source_urls ADD COLUMN {col_name} {col_type}")
            added_count += 1
        else:
            print(f"Column already exists: {col_name}")
    
    conn.commit()
    conn.close()
    
    if added_count > 0:
        print(f"\nMigration complete: Added {added_count} column(s)")
    else:
        print("\nMigration complete: No new columns needed")

if __name__ == "__main__":
    migrate_database()
