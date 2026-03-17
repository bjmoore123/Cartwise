"""tests/conftest.py — Shared fixtures for all tests.

Sets environment variables before any Lambda module is imported,
then provides a moto-mocked DynamoDB fixture.
"""
import os
import sys

# Set test env vars before any Lambda module is imported
os.environ.update({
    "USERS_TABLE":       "test-users",
    "SESSIONS_TABLE":    "test-sessions",
    "DEALS_TABLE":       "test-deals",
    "SCRAPE_LOGS_TABLE": "test-scrape-logs",
    "AUTH_LOGS_TABLE":   "test-auth-logs",
    "APP_LOGS_TABLE":    "test-app-logs",
    "HISTORY_TABLE":     "test-deal-history",
    "CORPUS_TABLE":      "test-deal-corpus",
    "ADMIN_SECRET":      "test-admin-secret-12345",
    "FRONTEND_URL":      "http://localhost:3000",
    "API_URL":           "http://localhost:3000",
    "PDC_REGION":        "us-east-1",
    "UNSUB_SECRET":      "test-unsub-secret",
    "RESEND_WEBHOOK_SECRET": "not-yet-configured",
    "INBOUND_EMAIL_ADDR":    "",
    "RESEND_API_KEY":        "",
    "RESEND_FROM_NAME":      "Test",
    "RESEND_FROM_ADDR":      "test@example.com",
})

# Add lambda/ and project root to path so imports resolve without packaging
_root = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(_root, "lambda"))
sys.path.insert(0, _root)

import pytest
import boto3
from moto import mock_aws


@pytest.fixture
def dynamodb_tables():
    """Spin up mocked DynamoDB tables for each test."""
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        table_defs = [
            ("test-users",        "email"),
            ("test-sessions",     "token"),
            ("test-deals",        "store_id"),
            ("test-scrape-logs",  "job_id"),
            ("test-auth-logs",    "log_id"),
            ("test-app-logs",     "log_id"),
            ("test-deal-history", "store_id"),
            ("test-deal-corpus",  "corpus_id"),
        ]
        for name, key in table_defs:
            ddb.create_table(
                TableName=name,
                KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
        yield ddb
