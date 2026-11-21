"""Credential-holding reverse proxy for model API calls.

The gateway runs in its own container. The agent's container reaches the model
APIs only through it, and holds no API key. See gateway/README.md.
"""
