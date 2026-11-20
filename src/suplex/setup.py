from __future__ import annotations

import httpx

from suplex.config import Config

SERVICES = {
    "apify_token": {
        "name": "Apify",
        "required": True,
        "signup_url": "https://apify.com/sign-up",
        "key_url": "https://console.apify.com/account/integrations",
        "description": "Scrapes Google Maps and LinkedIn. Free tier: $5/month credits.",
    },
    "anthropic_api_key": {
        "name": "Anthropic (Claude)",
        "required": True,
        "signup_url": "https://console.anthropic.com/",
        "key_url": "https://console.anthropic.com/settings/keys",
        "description": "AI strategist that decides what to scrape. Powers the SDR brain.",
    },
    "reacher_api_key": {
        "name": "Reacher",
        "required": False,
        "signup_url": "https://reacher.email/",
        "key_url": "https://app.reacher.email/dashboard",
        "description": "Email verification via SMTP handshake. Checks if mailboxes actually exist. Optional — without it, verification is syntax + DNS only.",
    },
    "million_verifier_api_key": {
        "name": "MillionVerifier",
        "required": False,
        "signup_url": "https://www.millionverifier.com/",
        "key_url": "https://app.millionverifier.com/api",
        "description": "Catch-all domain detection. Supplements Reacher for domains that accept all emails. Optional.",
    },
}


async def validate_key(service_key: str, value: str) -> dict:
    validators = {
        "apify_token": _validate_apify,
        "anthropic_api_key": _validate_anthropic,
        "reacher_api_key": _validate_reacher,
        "million_verifier_api_key": _validate_million_verifier,
    }
    validator = validators.get(service_key)
    if not validator:
        return {"valid": False, "error": f"Unknown service: {service_key}"}
    return await validator(value)


async def configure_key(service_key: str, value: str) -> dict:
    result = await validate_key(service_key, value)
    if result["valid"]:
        config = Config.load()
        setattr(config, service_key, value)
        config.save()
        result["saved"] = True
    return result


def get_setup_status() -> dict:
    config = Config.load()
    status: dict = {"configured": [], "missing_required": [], "missing_optional": []}

    for key, meta in SERVICES.items():
        entry = {
            "key": key,
            "name": meta["name"],
            "description": meta["description"],
            "signup_url": meta["signup_url"],
            "key_url": meta["key_url"],
        }
        if config.is_configured(key):
            status["configured"].append(entry)
        elif meta["required"]:
            status["missing_required"].append(entry)
        else:
            status["missing_optional"].append(entry)

    status["ready"] = len(status["missing_required"]) == 0
    return status


async def _validate_apify(token: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.apify.com/v2/users/me",
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code == 200:
                data = resp.json().get("data", {})
                return {
                    "valid": True,
                    "username": data.get("username"),
                    "plan": data.get("plan", {}).get("id"),
                    "credits": data.get("proxy", {}).get("usageUsd"),
                }
            return {"valid": False, "error": f"HTTP {resp.status_code}"}
    except httpx.HTTPError as e:
        return {"valid": False, "error": str(e)}


async def _validate_anthropic(key: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.anthropic.com/v1/models",
                headers={
                    "x-api-key": key,
                    "anthropic-version": "2023-06-01",
                },
            )
            if resp.status_code == 200:
                return {"valid": True}
            return {"valid": False, "error": f"HTTP {resp.status_code}"}
    except httpx.HTTPError as e:
        return {"valid": False, "error": str(e)}


async def _validate_reacher(key: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                "https://api.reacher.email/v0/check_email",
                headers={"Authorization": key},
                json={"to_email": "test@gmail.com"},
            )
            if resp.status_code in (200, 429):
                return {"valid": True}
            return {"valid": False, "error": f"HTTP {resp.status_code}"}
    except httpx.HTTPError as e:
        return {"valid": False, "error": str(e)}


async def _validate_million_verifier(key: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.millionverifier.com/api/v3/",
                params={"api": key, "email": "test@gmail.com"},
            )
            if resp.status_code == 200:
                data = resp.json()
                if "error" not in data:
                    return {"valid": True, "result": data.get("result")}
            return {"valid": False, "error": f"HTTP {resp.status_code}"}
    except httpx.HTTPError as e:
        return {"valid": False, "error": str(e)}
