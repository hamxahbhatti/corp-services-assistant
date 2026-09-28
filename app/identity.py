"""Demo identity: stands in for Microsoft Entra ID.

In production the web app signs users in with Entra ID (MSAL), the token carries group claims,
and the orchestrator performs an OAuth 2.0 on-behalf-of (OBO) exchange to call downstream APIs
with the *user's* identity. Here we mint HS256 JWTs locally to reproduce the same flow:

  user token (aud=corp-assistant)  --OBO exchange-->  downstream token (aud=enterprise-apis)

The MCP tool server only accepts the downstream token and authorises every call on its claims,
so the agent can never do more than the signed-in employee.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import jwt

from .config import settings

ISSUER = "https://login.demo.local/authority-tenant/v2.0"
APP_AUDIENCE = "corp-assistant"
API_AUDIENCE = "enterprise-apis"


@dataclass(frozen=True)
class Persona:
    oid: str
    name: str
    title: str
    employee_id: str
    email: str
    groups: tuple[str, ...]
    language: str  # preferred UI language


PERSONAS: dict[str, Persona] = {
    "sara": Persona("u-1043", "Sara Al Mansoori", "Finance Officer (requester)", "E1043", "sara.almansoori@authority.example",
                    ("Staff", "FIN-Requesters"), "ar"),
    "omar": Persona("u-2210", "Omar Haddad", "IT Engineer", "E2210", "omar.haddad@authority.example",
                    ("Staff", "IT-Team"), "en"),
    "layla": Persona("u-3307", "Layla Rahman", "Legal Counsel", "E3307", "layla.rahman@authority.example",
                     ("Staff", "LEGAL"), "en"),
    "huda": Persona("u-4120", "Huda Saeed", "HR Compensation Specialist", "E4120", "huda.saeed@authority.example",
                    ("Staff", "HR-Team", "HR-COMP"), "en"),
}


def issue_user_token(persona_key: str, ttl: int = 3600) -> str:
    p = PERSONAS[persona_key]
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": APP_AUDIENCE, "sub": p.oid, "oid": p.oid, "name": p.name, "email": p.email,
              "employee_id": p.employee_id, "groups": list(p.groups), "iat": now, "exp": now + ttl}
    return jwt.encode(claims, settings.identity_secret, algorithm="HS256")


def validate(token: str, audience: str) -> dict:
    return jwt.decode(token, settings.identity_secret, algorithms=["HS256"], audience=audience, issuer=ISSUER)


def obo_exchange(user_token: str, scope: str = API_AUDIENCE, ttl: int = 600) -> str:
    """Simulated OBO: validate the caller's token, then mint a short-lived token for the downstream API
    that carries the same user identity and groups (never a service account)."""
    c = validate(user_token, APP_AUDIENCE)
    now = int(time.time())
    down = {k: c[k] for k in ("iss", "sub", "oid", "name", "email", "employee_id", "groups")}
    down.update({"aud": scope, "azp": APP_AUDIENCE, "scp": "Invoices.Read Tickets.Read PR.Read Mail.Send Catalog.Request",
                 "iat": now, "exp": now + ttl})
    return jwt.encode(down, settings.identity_secret, algorithm="HS256")
