"""SAML 2.0 single sign-on for the console (SP-initiated, HTTP-POST binding).

The XML work is done by python3-saml (OneLogin's toolkit), which verifies XML
signatures with xmlsec1 (libxmlsec1 through the `xmlsec` binding). Nothing here parses
or canonicalises XML or checks a signature by hand.

What is checked, on every response (python3-saml in strict mode, plus this module):

- the assertion is signed (`wantAssertionsSigned`) by the IdP certificate configured
  here, with RSA-SHA256 or stronger; SHA-1 signatures and digests are refused
  (`rejectDeprecatedAlgorithm`); the toolkit checks that the signed element is the
  element it reads (signature wrapping), and refuses responses with more than one
  assertion;
- `Issuer` is the configured IdP entity id, `Audience` is this SP's entity id,
  `Destination` and `Recipient` are this ACS URL, `NotBefore` / `NotOnOrAfter` and the
  subject confirmation window hold (with the toolkit's clock skew allowance);
- `InResponseTo` is the id of a request this SP issued: the id travels in
  `RelayState`, HMAC-signed and expiring after `request_ttl_seconds`, so an
  unsolicited (IdP-initiated) response or one for another request is refused;
- an assertion id is accepted once (`ReplayCache`, in memory until the assertion's
  own `NotOnOrAfter`). ASSUMPTION: one process; with several workers a replay could
  land on another worker inside the assertion's lifetime (minutes at most). A shared
  cache is not built.

Mapping: the subject is the NameID (or `subject_attribute`), the tenant comes from
`tenant_attribute` (else `default_tenant`, else the login is refused) and roles from
`roles_attribute` (multi-valued), mapped to permissions exactly like API key roles
(`scruple.auth.rbac`): unknown roles grant nothing, a login that grants nothing or
grants both propose and approve is refused. `role_map` can rename IdP group names
to scruple roles (`"risk-team": "risk_admin"`).

AuthnRequests are not signed (no SP private key is configured); the IdP must accept
unsigned requests from this SP. Single logout is not built: signing out clears the
console session only.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from scruple.auth.models import TENANT_PATTERN
from scruple.auth.rbac import SeparationOfDutiesError, check_separation, permissions_for
from scruple.registry.models import ACTOR_PATTERN

POST_BINDING = "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"
REDIRECT_BINDING = "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"
NAMEID_UNSPECIFIED = "urn:oasis:names:tc:SAML:1.1:nameid-format:unspecified"
RSA_SHA256 = "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256"
SHA256 = "http://www.w3.org/2001/04/xmlenc#sha256"
MAX_RESPONSE_BYTES = 256 * 1024
_STATE_LABEL = b"scruple-saml-relay-v1"
_NEXT = re.compile(r"^/console(/[A-Za-z0-9._~%!$&'()*+,;=:@/?-]*)?$")


class SAMLError(Exception):
    """A login that must be refused. The message is safe to show the user."""


class SAMLConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    sp_entity_id: str = Field(min_length=1, max_length=512)
    acs_url: str = Field(pattern=r"^https?://", max_length=512)
    idp_entity_id: str = Field(min_length=1, max_length=512)
    idp_sso_url: str = Field(pattern=r"^https?://", max_length=1024)
    idp_x509_cert: str = Field(min_length=64, description="PEM or base64 DER")
    subject_attribute: str | None = None
    tenant_attribute: str = "scruple_tenant"
    default_tenant: str | None = Field(default=None, pattern=TENANT_PATTERN)
    roles_attribute: str = "scruple_roles"
    role_map: dict[str, str] = Field(default_factory=dict)
    request_ttl_seconds: int = Field(default=600, ge=30, le=3600)


@dataclass(frozen=True)
class SAMLIdentity:
    subject: str
    tenant: str
    roles: tuple[str, ...]
    idp: str
    assertion_id: str


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def safe_next(target: str | None) -> str:
    """Where to land after login: a console path, never another site."""
    if target and _NEXT.match(target) and "//" not in target and "\\" not in target:
        return target
    return "/console/"


class RelayState:
    """`RelayState` = HMAC-signed {request id, next path, expiry}."""

    def __init__(self, secret: bytes, ttl_seconds: int, now: Callable[[], float]) -> None:
        self._key = hmac.new(secret, _STATE_LABEL, hashlib.sha256).digest()
        self._ttl = ttl_seconds
        self._now = now

    def _mac(self, body: str) -> str:
        return _b64(hmac.new(self._key, body.encode("ascii"), hashlib.sha256).digest())

    def make(self, request_id: str, next_path: str) -> str:
        body = _b64(
            json.dumps(
                {
                    "id": request_id,
                    "next": safe_next(next_path),
                    "exp": int(self._now()) + self._ttl,
                },
                separators=(",", ":"),
            ).encode()
        )
        return f"{body}.{self._mac(body)}"

    def read(self, state: str | None) -> tuple[str, str]:
        """(request id, next path), or `SAMLError`."""
        if not state or len(state) > 1024 or state.count(".") != 1:
            raise SAMLError("sign-in was not started here; start again")
        body, mac = state.split(".")
        if not hmac.compare_digest(self._mac(body), mac):
            raise SAMLError("sign-in was not started here; start again")
        try:
            data = json.loads(_unb64(body))
            rid, nxt, exp = str(data["id"]), str(data["next"]), int(data["exp"])
        except (ValueError, KeyError, TypeError):
            raise SAMLError("sign-in was not started here; start again") from None
        if self._now() >= exp:
            raise SAMLError("sign-in took too long; start again")
        return rid, safe_next(nxt)


class ReplayCache:
    """Assertion ids seen, kept until their own NotOnOrAfter."""

    def __init__(self, now: Callable[[], float] = time.time, max_size: int = 100_000) -> None:
        self._seen: dict[str, float] = {}
        self._now = now
        self._max = max_size
        self._lock = threading.Lock()

    def first_use(self, assertion_id: str, until: float) -> bool:
        with self._lock:
            now = self._now()
            if len(self._seen) >= self._max:
                self._seen = {k: v for k, v in self._seen.items() if v > now}
                if len(self._seen) >= self._max:
                    return False  # fail closed rather than forget
            seen = self._seen.get(assertion_id)
            if seen is not None and seen > now:
                return False
            self._seen[assertion_id] = max(until, now + 300)
            return True


def _first(values: object) -> str | None:
    if isinstance(values, list | tuple) and values and isinstance(values[0], str):
        return values[0]
    return None


class SAMLServiceProvider:
    """The SP side: build the login redirect, check the response, map the identity."""

    def __init__(
        self,
        config: SAMLConfig,
        secret: bytes,
        *,
        now: Callable[[], float] = time.time,
        replay: ReplayCache | None = None,
    ) -> None:
        if len(secret) < 32:
            raise ValueError("the SAML relay secret must be at least 32 bytes")
        self.config = config
        self.relay = RelayState(secret, config.request_ttl_seconds, now)
        self.replay = replay or ReplayCache(now)
        self._now = now

    def settings(self) -> dict[str, Any]:
        c = self.config
        return {
            "strict": True,
            "debug": False,
            "sp": {
                "entityId": c.sp_entity_id,
                "assertionConsumerService": {"url": c.acs_url, "binding": POST_BINDING},
                "NameIDFormat": NAMEID_UNSPECIFIED,
                "x509cert": "",
                "privateKey": "",
            },
            "idp": {
                "entityId": c.idp_entity_id,
                "singleSignOnService": {"url": c.idp_sso_url, "binding": REDIRECT_BINDING},
                "x509cert": c.idp_x509_cert,
            },
            "security": {
                "authnRequestsSigned": False,
                "wantMessagesSigned": False,
                "wantAssertionsSigned": True,
                "wantAssertionsEncrypted": False,
                "wantNameId": True,
                "wantAttributeStatement": False,
                "requestedAuthnContext": False,
                "rejectDeprecatedAlgorithm": True,
                "rejectUnsolicitedResponsesWithInResponseTo": True,
                "allowRepeatAttributeName": False,
                "signatureAlgorithm": RSA_SHA256,
                "digestAlgorithm": SHA256,
            },
        }

    def _request_data(self, post: Mapping[str, str] | None = None) -> dict[str, Any]:
        """The toolkit's view of 'this request', built from the configured ACS URL
        (not from Host headers a proxy or client controls), so Destination and
        Recipient must equal what was configured."""
        u = urlsplit(self.config.acs_url)
        return {
            "https": "on" if u.scheme == "https" else "off",
            "http_host": u.netloc,  # host[:port], as configured
            "script_name": u.path or "/",
            "get_data": {},
            "post_data": dict(post or {}),
        }

    def _auth(self, post: Mapping[str, str] | None = None) -> Any:
        from onelogin.saml2.auth import OneLogin_Saml2_Auth

        return OneLogin_Saml2_Auth(self._request_data(post), old_settings=self.settings())

    def login_redirect(self, next_path: str | None) -> str:
        """The IdP URL to send the browser to (AuthnRequest + signed RelayState)."""
        from onelogin.saml2.authn_request import OneLogin_Saml2_Authn_Request
        from onelogin.saml2.utils import OneLogin_Saml2_Utils

        auth = self._auth()
        req = OneLogin_Saml2_Authn_Request(auth.get_settings())
        state = self.relay.make(req.get_id(), next_path or "/console/")
        params = {"SAMLRequest": req.get_request(), "RelayState": state}
        url: str = OneLogin_Saml2_Utils.redirect(self.config.idp_sso_url, params)
        return url

    def metadata(self) -> str:
        from onelogin.saml2.settings import OneLogin_Saml2_Settings

        s = OneLogin_Saml2_Settings(self.settings(), sp_validation_only=True)
        xml = s.get_sp_metadata()
        return xml.decode() if isinstance(xml, bytes) else str(xml)

    def consume(
        self, saml_response: str | None, relay_state: str | None
    ) -> tuple[SAMLIdentity, str]:
        """Check a POSTed response. (identity, next path), or `SAMLError`."""
        request_id, next_path = self.relay.read(relay_state)
        if not saml_response or len(saml_response) > MAX_RESPONSE_BYTES:
            raise SAMLError("no SAML response, or one too large")
        auth = self._auth({"SAMLResponse": saml_response, "RelayState": relay_state or ""})
        try:
            auth.process_response(request_id=request_id)
        except Exception as exc:  # the toolkit raises its own and lxml/xmlsec errors
            raise SAMLError("the SAML response could not be verified") from exc
        if auth.get_errors() or not auth.is_authenticated():
            raise SAMLError("the SAML response could not be verified")
        assertion_id = auth.get_last_assertion_id()
        until = auth.get_last_assertion_not_on_or_after() or (self._now() + 300)
        if not isinstance(assertion_id, str) or not self.replay.first_use(assertion_id, until):
            raise SAMLError("this SAML response was already used")
        identity = self.identity(auth.get_nameid(), auth.get_attributes(), assertion_id)
        return identity, next_path

    def identity(
        self, nameid: object, attributes: Mapping[str, object], assertion_id: str
    ) -> SAMLIdentity:
        c = self.config
        subject = _first(attributes.get(c.subject_attribute)) if c.subject_attribute else nameid
        if not isinstance(subject, str) or not re.match(ACTOR_PATTERN, subject):
            raise SAMLError("the IdP's subject is not an acceptable user name")
        tenant = _first(attributes.get(c.tenant_attribute)) or c.default_tenant
        if tenant is None or not re.match(TENANT_PATTERN, tenant):
            raise SAMLError("the IdP sent no acceptable tenant for this user")
        raw = attributes.get(c.roles_attribute) or []
        names = [r for r in raw if isinstance(r, str)] if isinstance(raw, list) else []
        roles = tuple(sorted({c.role_map.get(r, r) for r in names}))
        granted = permissions_for(roles, ())
        try:
            check_separation(granted)
        except SeparationOfDutiesError as exc:
            raise SAMLError(str(exc)) from None
        if not granted:
            raise SAMLError("this user has no scruple role")
        return SAMLIdentity(subject, tenant, roles, c.idp_entity_id, assertion_id)


__all__ = [
    "RelayState",
    "ReplayCache",
    "SAMLConfig",
    "SAMLError",
    "SAMLIdentity",
    "SAMLServiceProvider",
    "safe_next",
]
