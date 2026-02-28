"""
Database Module
---------------

Database connection utilities, models, and memory layer.
"""

from db.session import create_knowledge, get_postgres_db
from db.url import db_url
from db.models import Client, EmailHistory, get_session
from db.init_data import init_default_data
from db.memory import (
    add_client,
    decrement_discount,
    delete_client,
    get_client,
    get_email_history,
    list_clients,
    save_email,
    update_client,
)

__all__ = [
    "Client",
    "EmailHistory",
    "add_client",
    "create_knowledge",
    "db_url",
    "decrement_discount",
    "delete_client",
    "get_client",
    "get_email_history",
    "get_postgres_db",
    "get_session",
    "init_default_data",
    "list_clients",
    "save_email",
    "update_client",
]
