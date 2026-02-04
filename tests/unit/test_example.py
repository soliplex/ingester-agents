"""Tests for soliplex.agents.example module."""

import logging

import pytest

from soliplex.agents.example import run_schedule_minute


@pytest.mark.asyncio
async def test_run_schedule_minute_logs_message(caplog):
    """Test run_schedule_minute logs an info message."""
    with caplog.at_level(logging.INFO, logger="soliplex.agents.example"):
        await run_schedule_minute()

    assert "Running example schedule" in caplog.text


@pytest.mark.asyncio
async def test_run_schedule_minute_returns_none():
    """Test run_schedule_minute returns None (no explicit return value)."""
    result = await run_schedule_minute()
    assert result is None
