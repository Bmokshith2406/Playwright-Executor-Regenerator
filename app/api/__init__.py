"""
API Module - Versioned API Routes

This module provides versioned API routing for the repair engine.
"""

from fastapi import APIRouter

# API Version prefix
API_V1_PREFIX = "/api/v1"
API_V2_PREFIX = "/api/v2"

# Create versioned routers
v1_router = APIRouter(prefix=API_V1_PREFIX, tags=["v1"])
v2_router = APIRouter(prefix=API_V2_PREFIX, tags=["v2"])
