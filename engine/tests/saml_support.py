"""A local test IdP: a freshly generated RSA key and self-signed certificate, and
SAML responses built from a template and signed with xmlsec (python3-saml's
`OneLogin_Saml2_Utils.add_sign`). Nothing leaves the process; no real IdP is involved.
"""

from __future__ import annotations

import base64
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from xml.sax.saxutils import escape

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

SP_ENTITY = "https://scruple.test/saml/sp"
ACS_URL = "https://scruple.test/auth/saml/acs"
IDP_ENTITY = "https://idp.test/metadata"
IDP_SSO = "https://idp.test/sso"
RSA_SHA1 = "http://www.w3.org/2000/09/xmldsig#rsa-sha1"
SHA1 = "http://www.w3.org/2000/09/xmldsig#sha1"


@dataclass(frozen=True)
class KeyPair:
    key_pem: str
    cert_pem: str


def make_keypair(cn: str = "scruple test idp") -> KeyPair:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return KeyPair(key_pem, cert.public_bytes(serialization.Encoding.PEM).decode())


def _t(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def attribute(name: str, *values: str) -> str:
    vals = "".join(
        f'<saml:AttributeValue xsi:type="xs:string">{escape(v)}</saml:AttributeValue>'
        for v in values
    )
    return f'<saml:Attribute Name="{escape(name)}">{vals}</saml:Attribute>'


def assertion_xml(
    *,
    in_response_to: str,
    nameid: str = "alice@bank.example",
    tenant: str | None = "acme",
    roles: tuple[str, ...] = ("risk_admin",),
    audience: str = SP_ENTITY,
    issuer: str = IDP_ENTITY,
    recipient: str = ACS_URL,
    not_on_or_after: datetime | None = None,
    assertion_id: str | None = None,
    raw_nameid: str | None = None,
) -> str:
    now = datetime.now(UTC)
    until = not_on_or_after or now + timedelta(minutes=5)
    attrs = []
    if tenant is not None:
        attrs.append(attribute("scruple_tenant", tenant))
    if roles:
        attrs.append(attribute("scruple_roles", *roles))
    statement = f"<saml:AttributeStatement>{''.join(attrs)}</saml:AttributeStatement>"
    aid = assertion_id or f"_a{uuid.uuid4().hex}"
    nid = raw_nameid if raw_nameid is not None else escape(nameid)
    return (
        f'<saml:Assertion xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" '
        f'xmlns:xs="http://www.w3.org/2001/XMLSchema" '
        f'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        f'ID="{aid}" Version="2.0" IssueInstant="{_t(now)}">'
        f"<saml:Issuer>{escape(issuer)}</saml:Issuer>"
        f"<saml:Subject><saml:NameID "
        f'Format="urn:oasis:names:tc:SAML:1.1:nameid-format:unspecified">{nid}</saml:NameID>'
        f'<saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer">'
        f'<saml:SubjectConfirmationData NotOnOrAfter="{_t(until)}" Recipient="{escape(recipient)}" '
        f'InResponseTo="{escape(in_response_to)}"/></saml:SubjectConfirmation></saml:Subject>'
        f'<saml:Conditions NotBefore="{_t(now - timedelta(minutes=1))}" NotOnOrAfter="{_t(until)}">'
        f"<saml:AudienceRestriction><saml:Audience>{escape(audience)}</saml:Audience>"
        f"</saml:AudienceRestriction></saml:Conditions>"
        f'<saml:AuthnStatement AuthnInstant="{_t(now)}" SessionIndex="_s{uuid.uuid4().hex}">'
        f"<saml:AuthnContext><saml:AuthnContextClassRef>"
        f"urn:oasis:names:tc:SAML:2.0:ac:classes:PasswordProtectedTransport"
        f"</saml:AuthnContextClassRef></saml:AuthnContext></saml:AuthnStatement>"
        f"{statement}</saml:Assertion>"
    )


def response_xml(assertions: str, *, in_response_to: str, destination: str = ACS_URL) -> str:
    now = datetime.now(UTC)
    return (
        f'<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" '
        f'xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" ID="_r{uuid.uuid4().hex}" '
        f'Version="2.0" IssueInstant="{_t(now)}" Destination="{escape(destination)}" '
        f'InResponseTo="{escape(in_response_to)}">'
        f"<saml:Issuer>{IDP_ENTITY}</saml:Issuer>"
        f'<samlp:Status><samlp:StatusCode Value="urn:oasis:names:tc:SAML:2.0:status:Success"/>'
        f"</samlp:Status>{assertions}</samlp:Response>"
    )


def sign(xml: str, kp: KeyPair, *, sha1: bool = False) -> str:
    """Sign the (first) Assertion element in `xml` with xmlsec."""
    from onelogin.saml2.constants import OneLogin_Saml2_Constants as C
    from onelogin.saml2.utils import OneLogin_Saml2_Utils

    m = re.search(r"<saml:Assertion .*?</saml:Assertion>", xml, re.S)
    assert m is not None
    signed = OneLogin_Saml2_Utils.add_sign(
        m.group(0),
        kp.key_pem,
        kp.cert_pem,
        sign_algorithm=C.RSA_SHA1 if sha1 else C.RSA_SHA256,
        digest_algorithm=C.SHA1 if sha1 else C.SHA256,
    )
    text = signed.decode() if isinstance(signed, bytes) else str(signed)
    text = re.sub(r"^<\?xml[^>]*\?>\s*", "", text)
    return xml[: m.start()] + text + xml[m.end() :]


def b64(xml: str) -> str:
    return base64.b64encode(xml.encode()).decode()


def request_id_of(location: str) -> str:
    """The AuthnRequest id inside a login redirect (deflated, base64)."""
    import zlib
    from urllib.parse import parse_qs, urlsplit

    q = parse_qs(urlsplit(location).query)
    raw = zlib.decompress(base64.b64decode(q["SAMLRequest"][0]), -15).decode()
    m = re.search(r'ID="([^"]+)"', raw)
    assert m is not None
    return m.group(1)


def relay_of(location: str) -> str:
    from urllib.parse import parse_qs, urlsplit

    return parse_qs(urlsplit(location).query)["RelayState"][0]
