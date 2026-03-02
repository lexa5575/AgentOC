"""
Migration: Add gmail_thread_id to email_history
------------------------------------------------

Run this script to add the gmail_thread_id column to existing databases.

Usage:
    docker exec agentos-api python scripts/migrate_thread_id.py
"""

import logging
import sys

from sqlalchemy import text

from db.models import engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def migrate():
    """Add gmail_thread_id column to email_history table."""
    with engine.connect() as conn:
        # Check if column already exists
        result = conn.execute(text("""
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_name = 'email_history' 
            AND column_name = 'gmail_thread_id'
        """))
        
        if result.fetchone():
            logger.info("Column gmail_thread_id already exists, skipping migration")
            return
        
        # Add the column
        logger.info("Adding gmail_thread_id column to email_history...")
        conn.execute(text("""
            ALTER TABLE email_history 
            ADD COLUMN gmail_thread_id VARCHAR
        """))
        
        # Create index for fast lookups by thread
        logger.info("Creating index on gmail_thread_id...")
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_email_history_gmail_thread_id 
            ON email_history (gmail_thread_id)
        """))
        
        conn.commit()
        logger.info("Migration complete!")


if __name__ == "__main__":
    try:
        migrate()
    except Exception as e:
        logger.error("Migration failed: %s", e, exc_info=True)
        sys.exit(1)