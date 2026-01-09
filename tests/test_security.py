"""Unit tests for security module."""

import time
import pytest
from fastapi import Request, HTTPException, status
from unittest.mock import Mock

from rmq_middleware.security import (
    RateLimiter,
    check_rate_limit,
    validate_request_size,
    validate_amqp_name,
    InputValidationError,
    get_security_settings,
)

class TestRateLimiter:
    def test_rate_limiter_allow(self, monkeypatch):
        settings = get_security_settings()
        monkeypatch.setattr(settings, "rate_limit_enabled", True)
        monkeypatch.setattr(settings, "rate_limit_requests", 2)
        monkeypatch.setattr(settings, "rate_limit_window_seconds", 1)
        
        limiter = RateLimiter()
        key = "test_client"
        
        # First request allowed
        allowed, remaining = limiter.is_allowed(key)
        assert allowed is True
        assert remaining == 1
        
        # Second request allowed
        allowed, remaining = limiter.is_allowed(key)
        assert allowed is True
        assert remaining == 0
        
        # Third request blocked
        allowed, remaining = limiter.is_allowed(key)
        assert allowed is False
        assert remaining == 0

    def test_rate_limiter_cleanup(self, monkeypatch):
        settings = get_security_settings()
        monkeypatch.setattr(settings, "rate_limit_enabled", True)
        monkeypatch.setattr(settings, "rate_limit_window_seconds", 0.1)
        
        limiter = RateLimiter()
        limiter.is_allowed("test")
        assert "test" in limiter._requests
        
        time.sleep(0.2)
        limiter.cleanup()
        assert "test" not in limiter._requests

    @pytest.mark.asyncio
    async def test_check_rate_limit_middleware(self, monkeypatch):
        settings = get_security_settings()
        monkeypatch.setattr(settings, "rate_limit_enabled", True)
        monkeypatch.setattr(settings, "rate_limit_requests", 1)
        
        # Reset global limiter for test
        from rmq_middleware.security import rate_limiter
        rate_limiter._requests.clear()
        
        request = Mock(spec=Request)
        request.client.host = "127.0.0.1"
        
        # First call OK
        await check_rate_limit(request)
        
        # Second call raises 429
        with pytest.raises(HTTPException) as exc:
            await check_rate_limit(request)
        assert exc.value.status_code == status.HTTP_429_TOO_MANY_REQUESTS

class TestInputValidation:
    def test_validate_amqp_name_success(self):
        assert validate_amqp_name("valid.name-123") == "valid.name-123"
        assert validate_amqp_name("routing.key.#") == "routing.key.#"

    def test_validate_amqp_name_failure(self):
        with pytest.raises(InputValidationError):
            validate_amqp_name("invalid/name") # Slash not allowed usually unless escaped, but pattern forbids
        with pytest.raises(InputValidationError):
            validate_amqp_name("amq.prefix_reserved")
        with pytest.raises(InputValidationError):
            validate_amqp_name("a" * 300) # Too long

class TestRequestSize:
    @pytest.mark.asyncio
    async def test_validate_request_size_ok(self):
        request = Mock(spec=Request)
        request.headers = {"content-length": "100"}
        await validate_request_size(request)

    @pytest.mark.asyncio
    async def test_validate_request_size_exceeded(self, monkeypatch):
        settings = get_security_settings()
        monkeypatch.setattr(settings, "max_request_body_bytes", 50)
        
        request = Mock(spec=Request)
        request.headers = {"content-length": "100"}
        
        with pytest.raises(HTTPException) as exc:
            await validate_request_size(request)
        assert exc.value.status_code == status.HTTP_413_REQUEST_ENTITY_TOO_LARGE

    @pytest.mark.asyncio
    async def test_validate_request_size_no_header(self):
        request = Mock(spec=Request)
        request.headers = {}
        await validate_request_size(request)
