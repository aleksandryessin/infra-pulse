"""CSV ingestion (package B1): parse uploaded files, load PostgreSQL, track imports.

Stdlib + psycopg only: the HTTP process imports the repository part, the worker
imports the rest. Research, DuckDB and ML libraries are never imported here.
"""
