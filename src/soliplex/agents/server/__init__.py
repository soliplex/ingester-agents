"""
FastAPI server for Soliplex Agents.

Provides REST API endpoints for filesystem and SCM ingestion agents.
"""

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from soliplex.agents.config import settings

from .routes.fs import fs_router
from .routes.scm import scm_router

logger = logging.getLogger(__name__)


async def lifespan(app: FastAPI):
    """Manage app lifecycle."""
    logging.basicConfig(level=settings.log_level)
    logger.info("Starting soliplex-agents server")
    yield
    logger.info("soliplex-agents server stopped")


app = FastAPI(
    title="Soliplex Agents API",
    description="REST API for Soliplex document ingestion agents",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(fs_router)
app.include_router(scm_router)


# Health check endpoint (no auth required)
@app.get("/health", tags=["health"])
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy"}
