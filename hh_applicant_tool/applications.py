from __future__ import annotations

from typing import Any, Mapping

from .utils import random_text


def render_application_message(template: str, placeholders: Mapping[str, Any]) -> str:
    """Render an application message using the existing random_text + mapping format."""

    return random_text(template) % placeholders


def send_application(api_client, params: dict[str, Any]) -> dict:
    """Send an application using the same endpoint as apply-similar."""

    return api_client.post("/negotiations", params)
