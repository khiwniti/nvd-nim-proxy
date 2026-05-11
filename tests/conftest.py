"""Provide a stub NVIDIA_API_KEY before proxy module import so tests
that exercise the FastAPI lifespan don't fail on the startup guard.
No upstream requests are made by these tests.
"""
import os

os.environ.setdefault("NVIDIA_API_KEY", "test-key-for-pytest")
