"""
Sheet Config Operations
-----------------------

CRUD for LLM-generated sheet structure configurations.
One config per warehouse, stored as JSON in PostgreSQL.
"""

import json
import logging
from datetime import datetime, timedelta

from db.models import SheetConfig, get_session
from tools.structure_analyzer import SheetStructureConfig

logger = logging.getLogger(__name__)

# Config staleness threshold (re-analyze after this period)
CONFIG_MAX_AGE = timedelta(hours=24)


def load_sheet_config(warehouse: str) -> SheetStructureConfig | None:
    """Load sheet config from DB for a warehouse.

    Returns None if no config exists.
    """
    session = get_session()
    try:
        record = session.query(SheetConfig).filter_by(warehouse=warehouse).first()
        if not record:
            return None
        data = json.loads(record.config_json)
        return SheetStructureConfig(**data)
    except Exception as e:
        logger.error("Failed to load sheet config for %s: %s", warehouse, e)
        return None
    finally:
        session.close()


def save_sheet_config(warehouse: str, config: SheetStructureConfig) -> bool:
    """Save or update sheet config for a warehouse.

    Upserts: updates if exists, inserts if new.
    Returns True on success.
    """
    session = get_session()
    try:
        config_json = config.model_dump_json()
        record = session.query(SheetConfig).filter_by(warehouse=warehouse).first()

        if record:
            record.config_json = config_json
            record.analyzed_at = config.analyzed_at
        else:
            session.add(SheetConfig(
                warehouse=warehouse,
                config_json=config_json,
                analyzed_at=config.analyzed_at,
            ))

        session.commit()
        logger.info("Saved sheet config for %s (%d sections)", warehouse, len(config.sections))
        return True
    except Exception as e:
        logger.error("Failed to save sheet config for %s: %s", warehouse, e)
        session.rollback()
        return False
    finally:
        session.close()


def is_config_stale(config: SheetStructureConfig) -> bool:
    """Check if a config is older than CONFIG_MAX_AGE."""
    age = datetime.utcnow() - config.analyzed_at
    return age > CONFIG_MAX_AGE


def delete_sheet_config(warehouse: str) -> bool:
    """Delete sheet config for a warehouse (forces LLM re-analysis on next sync)."""
    session = get_session()
    try:
        deleted = session.query(SheetConfig).filter_by(warehouse=warehouse).delete()
        session.commit()
        return deleted > 0
    except Exception as e:
        logger.error("Failed to delete sheet config for %s: %s", warehouse, e)
        session.rollback()
        return False
    finally:
        session.close()
