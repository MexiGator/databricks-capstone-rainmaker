"""
Rainmaker -- model + messaging adapters.

Thin on purpose. Everything that talks to something outside this codebase
lives here, so the tools and tests can be exercised without a network.
"""

from __future__ import annotations

import os

DEFAULT_MODEL = os.environ.get("RAINMAKER_MODEL", "databricks-meta-llama-3-3-70b-instruct")


def complete(prompt: str, max_tokens: int = 512, temperature: float = 0.3) -> str:
    """
    Call a Databricks Foundation Model serving endpoint.

    Uses the SDK's NATIVE serving-endpoint query rather than
    `serving_endpoints.get_open_ai_client()`. The OpenAI-client shim only exists
    on newer databricks-sdk builds, so calling it blows up with
    'ServingEndpointsAPI object has no attribute get_open_ai_client' on the
    version deployed to the app. `serving_endpoints.query(...)` is present across
    SDK versions and keeps the app's OAuth auth, so the draft path stops depending
    on which SDK the runtime happens to ship.

    temperature is low by design: outreach copy that varies wildly between
    runs is impossible to evaluate and unnerving to demo.
    """
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.service.serving import ChatMessage, ChatMessageRole

    resp = WorkspaceClient().serving_endpoints.query(
        name=DEFAULT_MODEL,
        messages=[ChatMessage(role=ChatMessageRole.USER, content=prompt)],
        max_tokens=max_tokens,
        temperature=temperature,
    )
    return resp.choices[0].message.content


def try_send_sms(phone: str, body: str) -> str:
    """
    Send a real SMS if Twilio is configured; otherwise report 'queued'.

    Never raises. A third-party outage must not be able to break the demo,
    and the Lakebase write is what actually satisfies the requirement.
    """
    sid = os.environ.get("TWILIO_ACCOUNT_SID")
    token = os.environ.get("TWILIO_AUTH_TOKEN")
    from_number = os.environ.get("TWILIO_FROM_NUMBER")

    if not all([sid, token, from_number]):
        return "queued (Twilio not configured)"

    try:
        from twilio.rest import Client

        Client(sid, token).messages.create(to=phone, from_=from_number, body=body)
        return "sent via Twilio"
    except Exception as exc:  # noqa: BLE001 - deliberate: never break the loop
        return f"queued (Twilio error: {exc})"
