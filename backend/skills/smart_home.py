"""
Smart home skill.

This is a stub wired for Home Assistant (the most common self-hosted
smart-home hub), since that's the most likely fit for a self-hosted
setup. If you use something else (SmartThings, Hubitat, HomeKit),
swap the implementation of `call_service` and keep the function
signature the same so main.py doesn't need to change.

To activate:
1. Run Home Assistant (or point at an existing instance).
2. Create a Long-Lived Access Token in HA: profile -> security.
3. Set HA_URL and HA_TOKEN in your .env file.
"""
import os
import httpx

HA_URL = os.getenv("HA_URL", "")
HA_TOKEN = os.getenv("HA_TOKEN", "")


def is_configured() -> bool:
    return bool(HA_URL and HA_TOKEN)


async def call_service(domain: str, service: str, entity_id: str) -> dict:
    """
    Example: call_service("light", "turn_on", "light.living_room")
    """
    if not is_configured():
        return {"error": "Smart home not configured yet. Set HA_URL and HA_TOKEN in .env"}

    url = f"{HA_URL}/api/services/{domain}/{service}"
    headers = {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, headers=headers, json={"entity_id": entity_id}, timeout=10)
        resp.raise_for_status()
        return {"ok": True, "result": resp.json()}


async def get_state(entity_id: str) -> dict:
    if not is_configured():
        return {"error": "Smart home not configured yet. Set HA_URL and HA_TOKEN in .env"}

    url = f"{HA_URL}/api/states/{entity_id}"
    headers = {"Authorization": f"Bearer {HA_TOKEN}"}
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        return resp.json()
