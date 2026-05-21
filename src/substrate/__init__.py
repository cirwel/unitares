"""S19 substrate-claim attestation utilities.

 (M3-v2) for the design.
This package houses pure-Python helpers for the enrollment CLI and (in later
PRs) the runtime peer-attestation backends. Code that touches PostgreSQL or
the MCP server lives elsewhere; this package is dependency-light so it can
be exercised by unit tests without a DB.
"""
