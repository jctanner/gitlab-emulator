"""Tests for baseline HTTP security headers."""

import pytest


EXPECTED_SECURITY_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "SAMEORIGIN",
    "referrer-policy": "same-origin",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
}


def _assert_security_headers(resp) -> None:
    for name, value in EXPECTED_SECURITY_HEADERS.items():
        assert resp.headers[name] == value


@pytest.mark.asyncio
async def test_api_responses_include_security_headers(client):
    resp = await client.get("/api/v4/emojis")
    assert resp.status_code == 200
    _assert_security_headers(resp)


@pytest.mark.asyncio
async def test_admin_html_includes_security_headers(client):
    redirect = await client.get("/admin", follow_redirects=False)
    assert redirect.status_code == 307
    _assert_security_headers(redirect)

    admin_root = await client.get("/admin/", follow_redirects=False)
    assert admin_root.status_code == 302
    _assert_security_headers(admin_root)

    resp = await client.get("/admin/login")
    assert resp.status_code == 200
    _assert_security_headers(resp)


@pytest.mark.asyncio
async def test_error_responses_include_security_headers(client):
    resp = await client.get("/api/v4/does-not-exist")
    assert resp.status_code == 404
    _assert_security_headers(resp)
