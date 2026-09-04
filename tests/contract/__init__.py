"""Contract (API schema) tests for EI-ADS FastAPI services.

This package holds schemathesis-driven property-based contract tests that
auto-generate requests from each service's OpenAPI schema and assert the
service never returns a 5xx (server error) for a generated request.
"""
