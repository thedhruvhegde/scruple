"""RFC 3161 time-stamp requests and responses (TSQ/TSR), built and checked locally.

A time-stamping authority (TSA) signs "this digest existed at time T". Scruple sends
the digest of an anchor statement (`scruple.audit.anchor`) and keeps the signed token,
so anyone holding the TSA's certificate can later show the audit head existed then,
without trusting the operator of the database.

What `verify_token` checks (RFC 3161 section 2.4.2 and RFC 5652 section 5.4):

1. the content is a TSTInfo inside CMS SignedData;
2. the TSTInfo message imprint equals the digest we asked about (and the nonce, if any);
3. the signed attributes carry content type TSTInfo and a message digest equal to the
   hash of the encapsulated TSTInfo;
4. the signature over the DER signed attributes verifies with the signer certificate
   (RSA PKCS#1 v1.5, ECDSA or Ed25519);
5. the signing-certificate attribute (ESSCertID or ESSCertIDv2) names that certificate;
6. the certificate has a critical extended key usage containing id-kp-timeStamping and
   was valid at the token's genTime;
7. the certificate is one of the trusted certificates passed in, or is directly issued
   by one of them (one level, signature checked).

Not checked: revocation (no CRL or OCSP fetch; there is no network in verification),
chains longer than one intermediate step, RSA-PSS signatures, and the TSA's accuracy
field. These are listed as gaps in docs/trust/README.md.

No network here: `request_timestamp` takes an optional `httpx` transport, and the
tests run a local TSA built from a throwaway key and certificate.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import httpx
from cryptography import x509
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509.oid import ExtendedKeyUsageOID

from scruple.audit import der

HashName = Literal["sha256", "sha384", "sha512"]

HASH_OIDS: dict[str, str] = {
    "sha256": "2.16.840.1.101.3.4.2.1",
    "sha384": "2.16.840.1.101.3.4.2.2",
    "sha512": "2.16.840.1.101.3.4.2.3",
}
SHA1_OID = "1.3.14.3.2.26"
_HASHES: dict[str, type[hashes.HashAlgorithm]] = {
    HASH_OIDS["sha256"]: hashes.SHA256,
    HASH_OIDS["sha384"]: hashes.SHA384,
    HASH_OIDS["sha512"]: hashes.SHA512,
}
_HASHLIB: dict[str, str] = {
    HASH_OIDS["sha256"]: "sha256",
    HASH_OIDS["sha384"]: "sha384",
    HASH_OIDS["sha512"]: "sha512",
    SHA1_OID: "sha1",
}

OID_SIGNED_DATA = "1.2.840.113549.1.7.2"
OID_TST_INFO = "1.2.840.113549.1.9.16.1.4"
OID_CONTENT_TYPE = "1.2.840.113549.1.9.3"
OID_MESSAGE_DIGEST = "1.2.840.113549.1.9.4"
OID_SIGNING_CERT = "1.2.840.113549.1.9.16.2.12"
OID_SIGNING_CERT_V2 = "1.2.840.113549.1.9.16.2.47"
OID_RSA = "1.2.840.113549.1.1.1"
OID_EC_PUBLIC_KEY = "1.2.840.10045.2.1"
OID_ED25519 = "1.3.101.112"
_SIG_HASH: dict[str, str | None] = {
    # signatureAlgorithm -> the hash it implies (None: use the SignerInfo digestAlgorithm)
    OID_RSA: None,
    OID_EC_PUBLIC_KEY: None,
    "1.2.840.113549.1.1.11": HASH_OIDS["sha256"],
    "1.2.840.113549.1.1.12": HASH_OIDS["sha384"],
    "1.2.840.113549.1.1.13": HASH_OIDS["sha512"],
    "1.2.840.10045.4.3.2": HASH_OIDS["sha256"],
    "1.2.840.10045.4.3.3": HASH_OIDS["sha384"],
    "1.2.840.10045.4.3.4": HASH_OIDS["sha512"],
    OID_ED25519: None,
}
TSQ_CONTENT_TYPE = "application/timestamp-query"
TSR_CONTENT_TYPE = "application/timestamp-reply"

PKI_STATUS = {
    0: "granted",
    1: "grantedWithMods",
    2: "rejection",
    3: "waiting",
    4: "revocationWarning",
    5: "revocationNotification",
}


class TimestampError(ValueError):
    """A time-stamp request, response or token did not check out."""


@dataclass(frozen=True)
class TimestampRequest:
    der: bytes
    digest: bytes
    hash_oid: str
    nonce: int | None


@dataclass(frozen=True)
class TokenInfo:
    """What a verified token says. `token` is the TimeStampToken (CMS ContentInfo) DER."""

    token: bytes
    gen_time: datetime
    serial: int
    policy: str
    hash_oid: str
    digest: bytes
    nonce: int | None
    signer_subject: str
    signer_fingerprint_sha256: str


def digest_of(data: bytes, hash_name: HashName = "sha256") -> bytes:
    return hashlib.new(hash_name, data).digest()


def _seq(tlv: der.TLV, what: str = "sequence") -> list[der.TLV]:
    return der.expect(tlv, der.SEQUENCE, what).children()


def _alg_oid(tlv: der.TLV) -> str:
    """The OID of an AlgorithmIdentifier; parameters must be absent or NULL."""
    items = _seq(tlv, "AlgorithmIdentifier")
    if not items or len(items) > 2:
        raise TimestampError("bad AlgorithmIdentifier")
    if len(items) == 2 and items[1].tag == der.NULL and items[1].value:
        raise TimestampError("bad AlgorithmIdentifier parameters")
    return der.dec_oid(items[0])


def _alg_id(hash_oid: str) -> bytes:
    return der.seq(der.oid(hash_oid), der.null())


def build_request(
    digest: bytes,
    hash_name: HashName = "sha256",
    *,
    nonce: int | None = None,
    use_nonce: bool = True,
    cert_req: bool = True,
) -> TimestampRequest:
    """A TimeStampReq (version 1) for `digest`. A random 64-bit nonce by default."""
    hash_oid = HASH_OIDS[hash_name]
    if len(digest) != hashlib.new(hash_name).digest_size:
        raise TimestampError(f"digest is {len(digest)} bytes, {hash_name} needs a full digest")
    if use_nonce and nonce is None:
        nonce = secrets.randbits(63) | 1
    if not use_nonce:
        nonce = None
    parts = [der.integer(1), der.seq(_alg_id(hash_oid), der.octets(digest))]
    if nonce is not None:
        parts.append(der.integer(nonce))
    if cert_req:
        parts.append(der.boolean(True))
    return TimestampRequest(der=der.seq(*parts), digest=digest, hash_oid=hash_oid, nonce=nonce)


def parse_request(data: bytes) -> TimestampRequest:
    """Read a TimeStampReq (used by the local test TSA and for checking requests)."""
    items = der.expect(der.parse(data), der.SEQUENCE, "TimeStampReq").children()
    if len(items) < 2 or der.dec_int(items[0]) != 1:
        raise TimestampError("TimeStampReq must be version 1")
    hash_oid, digest = _imprint(items[1])
    nonce: int | None = None
    for it in items[2:]:
        if it.tag == der.INTEGER:
            nonce = der.dec_int(it)
    return TimestampRequest(der=data, digest=digest, hash_oid=hash_oid, nonce=nonce)


def _imprint(tlv: der.TLV) -> tuple[str, bytes]:
    alg, hashed = der.expect(tlv, der.SEQUENCE, "MessageImprint").children()
    return _alg_oid(alg), der.expect(hashed, der.OCTET_STRING, "digest").value


def request_timestamp(
    url: str,
    request: TimestampRequest,
    *,
    timeout: float = 30.0,
    transport: httpx.BaseTransport | None = None,
) -> bytes:
    """POST the request to a TSA and return the raw TimeStampResp bytes."""
    with httpx.Client(transport=transport, timeout=timeout) as client:
        r = client.post(url, content=request.der, headers={"Content-Type": TSQ_CONTENT_TYPE})
    if r.status_code != 200:
        raise TimestampError(f"TSA answered HTTP {r.status_code}")
    ctype = r.headers.get("content-type", "").split(";")[0].strip()
    if ctype != TSR_CONTENT_TYPE:
        raise TimestampError(f"TSA answered content type {ctype!r}, not {TSR_CONTENT_TYPE}")
    return r.content


def token_from_response(data: bytes) -> bytes:
    """The TimeStampToken from a TimeStampResp, or an error naming the PKI status."""
    items = der.expect(der.parse(data), der.SEQUENCE, "TimeStampResp").children()
    status_items = der.expect(items[0], der.SEQUENCE, "PKIStatusInfo").children()
    status = der.dec_int(status_items[0])
    if status not in (0, 1):
        detail = ""
        for it in status_items[1:]:
            if it.tag == der.SEQUENCE:
                texts = [c.value.decode("utf-8", "replace") for c in it.children()]
                detail = ": " + "; ".join(texts)
        raise TimestampError(f"TSA refused: {PKI_STATUS.get(status, status)}{detail}")
    if len(items) < 2:
        raise TimestampError("TSA granted the request but sent no token")
    return items[1].raw


def verify_response(
    data: bytes,
    request: TimestampRequest,
    trusted: list[x509.Certificate] | None,
) -> TokenInfo:
    """Check a TimeStampResp against the request it answers."""
    info = verify_token(token_from_response(data), request.digest, trusted, hash_oid=None)
    if info.hash_oid != request.hash_oid:
        raise TimestampError("token uses a different hash algorithm than the request")
    if request.nonce is not None and info.nonce != request.nonce:
        raise TimestampError("nonce in the token does not match the request (replayed?)")
    return info


def _signer_infos_and_certs(
    token: bytes,
) -> tuple[bytes, list[der.TLV], list[x509.Certificate]]:
    content_info = der.expect(der.parse(token), der.SEQUENCE, "ContentInfo").children()
    if len(content_info) != 2 or der.dec_oid(content_info[0]) != OID_SIGNED_DATA:
        raise TimestampError("token is not CMS SignedData")
    if not content_info[1].is_context(0):
        raise TimestampError("SignedData content must be [0] EXPLICIT")
    (signed_data,) = content_info[1].children()
    sd = der.expect(signed_data, der.SEQUENCE, "SignedData").children()
    if der.dec_int(sd[0]) != 3:
        raise TimestampError("SignedData must be version 3 for TSTInfo content")
    digest_algs = {_alg_oid(a) for a in der.expect(sd[1], der.SET, "digestAlgorithms").children()}
    encap = der.expect(sd[2], der.SEQUENCE, "EncapsulatedContentInfo").children()
    if der.dec_oid(encap[0]) != OID_TST_INFO:
        raise TimestampError("encapsulated content is not a TSTInfo")
    if len(encap) != 2 or not encap[1].is_context(0):
        raise TimestampError("TSTInfo content is missing")
    (econtent,) = encap[1].children()
    tst = der.expect(econtent, der.OCTET_STRING, "eContent").value
    certs: list[x509.Certificate] = []
    signer_infos: der.TLV | None = None
    for it in sd[3:]:
        if it.is_context(0) and it.constructed:
            certs = [
                x509.load_der_x509_certificate(c.raw)
                for c in it.children()
                if c.tag == der.SEQUENCE
            ]
        elif it.tag == der.SET:
            signer_infos = it
    if signer_infos is None:
        raise TimestampError("SignedData has no signerInfos")
    infos = signer_infos.children()
    if len(infos) != 1:
        raise TimestampError(f"expected exactly one signer, found {len(infos)}")
    si = der.expect(infos[0], der.SEQUENCE, "SignerInfo").children()
    if _alg_oid(si[2]) not in digest_algs:
        raise TimestampError("signer digest algorithm is not listed in SignedData")
    return tst, infos, certs


def _parse_tst(tst: bytes) -> tuple[str, str, bytes, int, datetime, int | None]:
    items = der.expect(der.parse(tst), der.SEQUENCE, "TSTInfo").children()
    if der.dec_int(items[0]) != 1:
        raise TimestampError("TSTInfo must be version 1")
    policy = der.dec_oid(items[1])
    hash_oid, digest = _imprint(items[2])
    serial = der.dec_int(items[3])
    gen_time = der.dec_gentime(items[4])
    nonce: int | None = None
    for it in items[5:]:
        if it.tag == der.INTEGER:
            nonce = der.dec_int(it)
    return policy, hash_oid, digest, serial, gen_time, nonce


def _find_signer(sid: der.TLV, certs: list[x509.Certificate]) -> x509.Certificate:
    if sid.tag == der.SEQUENCE:  # IssuerAndSerialNumber
        issuer, serial = sid.children()
        want = (issuer.raw, der.dec_int(serial))
        for c in certs:
            if (c.issuer.public_bytes(), c.serial_number) == want:
                return c
    elif sid.is_context(0):  # SubjectKeyIdentifier
        for c in certs:
            try:
                ski = c.extensions.get_extension_for_class(x509.SubjectKeyIdentifier)
            except x509.ExtensionNotFound:
                continue
            if ski.value.digest == sid.value:
                return c
    raise TimestampError("signer certificate not found in the token or the trusted set")


def _check_signature(
    cert: x509.Certificate, sig_oid: str, digest_oid: str, signature: bytes, data: bytes
) -> None:
    if sig_oid not in _SIG_HASH:
        raise TimestampError(f"unsupported signature algorithm {sig_oid}")
    hash_oid = _SIG_HASH[sig_oid] or digest_oid
    key = cert.public_key()
    try:
        if isinstance(key, ed25519.Ed25519PublicKey):
            key.verify(signature, data)
            return
        if hash_oid not in _HASHES:
            raise TimestampError(f"unsupported signature hash {hash_oid}")
        h = _HASHES[hash_oid]()
        if isinstance(key, rsa.RSAPublicKey):
            key.verify(signature, data, padding.PKCS1v15(), h)
        elif isinstance(key, ec.EllipticCurvePublicKey):
            key.verify(signature, data, ec.ECDSA(h))
        else:
            raise TimestampError("unsupported signer key type")
    except InvalidSignature as exc:
        raise TimestampError("token signature does not verify") from exc


def _check_signing_cert_attr(attrs: dict[str, der.TLV], cert: x509.Certificate) -> None:
    cert_der = cert.public_bytes(Encoding.DER)
    if OID_SIGNING_CERT_V2 in attrs:
        (value,) = attrs[OID_SIGNING_CERT_V2].children()
        first = der.expect(value, der.SEQUENCE, "SigningCertificateV2").children()[0]
        ess = _seq(_seq(first)[0])
        hash_oid = HASH_OIDS["sha256"]
        if ess[0].tag == der.SEQUENCE:
            hash_oid = _alg_oid(ess[0])
            ess = ess[1:]
        cert_hash = der.expect(ess[0], der.OCTET_STRING, "certHash").value
    elif OID_SIGNING_CERT in attrs:
        (value,) = attrs[OID_SIGNING_CERT].children()
        first = der.expect(value, der.SEQUENCE, "SigningCertificate").children()[0]
        ess = _seq(_seq(first)[0])
        hash_oid = SHA1_OID
        cert_hash = der.expect(ess[0], der.OCTET_STRING, "certHash").value
    else:
        raise TimestampError("signed attributes lack the signing-certificate attribute")
    if hash_oid not in _HASHLIB:
        raise TimestampError(f"unsupported certificate hash {hash_oid}")
    if hashlib.new(_HASHLIB[hash_oid], cert_der).digest() != cert_hash:
        raise TimestampError("signing-certificate attribute names a different certificate")


def _check_tsa_cert(cert: x509.Certificate, gen_time: datetime) -> None:
    try:
        eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
    except x509.ExtensionNotFound as exc:
        raise TimestampError("TSA certificate has no extended key usage") from exc
    if ExtendedKeyUsageOID.TIME_STAMPING not in eku.value or not eku.critical:
        raise TimestampError("TSA certificate lacks a critical timeStamping key usage")
    if not cert.not_valid_before_utc <= gen_time <= cert.not_valid_after_utc:
        raise TimestampError("TSA certificate was not valid at the token's genTime")


def _check_trust(
    cert: x509.Certificate, trusted: list[x509.Certificate], gen_time: datetime
) -> None:
    for anchor in trusted:
        if anchor == cert:
            return
    for anchor in trusted:
        if anchor.subject != cert.issuer:
            continue
        try:
            cert.verify_directly_issued_by(anchor)
        except (ValueError, TypeError, InvalidSignature):
            continue
        if anchor.not_valid_before_utc <= gen_time <= anchor.not_valid_after_utc:
            return
    raise TimestampError("TSA certificate is not trusted (not in, or issued by, --tsa-cert)")


def verify_token(
    token: bytes,
    digest: bytes,
    trusted: list[x509.Certificate] | None,
    *,
    hash_oid: str | None = HASH_OIDS["sha256"],
) -> TokenInfo:
    """Verify a TimeStampToken over `digest`. `trusted=None` checks everything except
    trust (the signer certificate comes from the token); callers say so to the user."""
    try:
        return _verify_token(token, digest, trusted, hash_oid)
    except der.DERError as exc:
        raise TimestampError(f"malformed token: {exc}") from exc
    except UnsupportedAlgorithm as exc:
        raise TimestampError(f"unsupported algorithm in token: {exc}") from exc
    except (IndexError, ValueError) as exc:
        if isinstance(exc, TimestampError):
            raise
        raise TimestampError(f"malformed token: {exc}") from exc


def _verify_token(
    token: bytes,
    digest: bytes,
    trusted: list[x509.Certificate] | None,
    hash_oid: str | None,
) -> TokenInfo:
    tst, infos, certs = _signer_infos_and_certs(token)
    policy, tst_hash_oid, tst_digest, serial, gen_time, nonce = _parse_tst(tst)
    if hash_oid is not None and tst_hash_oid != hash_oid:
        raise TimestampError("token imprint uses a different hash algorithm")
    if tst_digest != digest:
        raise TimestampError("token imprint does not match the anchored statement")

    si = der.expect(infos[0], der.SEQUENCE, "SignerInfo").children()
    version, sid, digest_alg = der.dec_int(si[0]), si[1], si[2]
    if (version, sid.tag == der.SEQUENCE) not in ((1, True), (3, False)):
        raise TimestampError("SignerInfo version does not match its signer identifier")
    digest_oid = _alg_oid(digest_alg)
    if not si[3].is_context(0):
        raise TimestampError("SignerInfo has no signed attributes")
    signed_attrs = si[3]
    sig_alg_oid = _alg_oid(si[4])
    signature = der.expect(si[5], der.OCTET_STRING, "signature").value

    attrs: dict[str, der.TLV] = {}
    for a in signed_attrs.children():
        a_oid, a_values = der.expect(a, der.SEQUENCE, "Attribute").children()
        name = der.dec_oid(a_oid)
        if name in attrs:
            raise TimestampError(f"signed attribute {name} appears twice")
        attrs[name] = der.expect(a_values, der.SET, "attribute values")
    if OID_CONTENT_TYPE not in attrs or OID_MESSAGE_DIGEST not in attrs:
        raise TimestampError("signed attributes lack content type or message digest")
    (ctype,) = attrs[OID_CONTENT_TYPE].children()
    if der.dec_oid(ctype) != OID_TST_INFO:
        raise TimestampError("signed content type is not TSTInfo")
    (md,) = attrs[OID_MESSAGE_DIGEST].children()
    if digest_oid not in _HASHLIB or digest_oid == SHA1_OID:
        raise TimestampError(f"unsupported digest algorithm {digest_oid}")
    if (
        hashlib.new(_HASHLIB[digest_oid], tst).digest()
        != der.expect(md, der.OCTET_STRING, "messageDigest").value
    ):
        raise TimestampError("message digest does not match the TSTInfo (edited)")

    pool = certs + list(trusted or [])
    signer = _find_signer(sid, pool)
    # The signature covers the DER SET OF the attributes, not the [0] IMPLICIT tag.
    _check_signature(signer, sig_alg_oid, digest_oid, signature, b"\x31" + signed_attrs.raw[1:])
    _check_signing_cert_attr(attrs, signer)
    _check_tsa_cert(signer, gen_time)
    if trusted is not None:
        _check_trust(signer, trusted, gen_time)
    return TokenInfo(
        token=token,
        gen_time=gen_time,
        serial=serial,
        policy=policy,
        hash_oid=tst_hash_oid,
        digest=tst_digest,
        nonce=nonce,
        signer_subject=signer.subject.rfc4514_string(),
        signer_fingerprint_sha256=signer.fingerprint(hashes.SHA256()).hex(),
    )
