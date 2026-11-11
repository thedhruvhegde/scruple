"""A throwaway RFC 3161 time-stamping authority for tests (no network, keys made per run).

It builds TimeStampResp DER with `scruple.audit.der`, signed by a TSA certificate issued
by a local CA. Knobs let tests break one property at a time.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from scruple.audit import der
from scruple.audit import timestamp as ts

KeyType = Literal["rsa", "ec", "ed25519"]
PrivateKey = rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey | ed25519.Ed25519PrivateKey
NOW = datetime(2026, 11, 11, 12, 0, 0, tzinfo=UTC)
POLICY = "1.3.6.1.4.1.99999.1"
SIG_OIDS = {"rsa": "1.2.840.113549.1.1.11", "ec": "1.2.840.10045.4.3.2", "ed25519": ts.OID_ED25519}


def _key(kind: KeyType) -> PrivateKey:
    if kind == "rsa":
        return rsa.generate_private_key(public_exponent=65537, key_size=2048)
    if kind == "ec":
        return ec.generate_private_key(ec.SECP256R1())
    return ed25519.Ed25519PrivateKey.generate()


def _sign_cert(builder: x509.CertificateBuilder, key: PrivateKey) -> x509.Certificate:
    algo = None if isinstance(key, ed25519.Ed25519PrivateKey) else hashes.SHA256()
    return builder.sign(key, algo)


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def make_ca(cn: str = "scruple test root") -> tuple[x509.Certificate, PrivateKey]:
    key = _key("ec")
    b = (
        x509.CertificateBuilder()
        .subject_name(_name(cn))
        .issuer_name(_name(cn))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOW - timedelta(days=365))
        .not_valid_after(NOW + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
    )
    return _sign_cert(b, key), key


@dataclass
class LocalTSA:
    key_type: KeyType = "rsa"
    eku: Literal["critical", "noncritical", "none", "wrong"] = "critical"
    valid_days: tuple[int, int] = (-30, 365)
    ca: tuple[x509.Certificate, PrivateKey] = field(default_factory=make_ca)
    cert: x509.Certificate = field(init=False)
    key: PrivateKey = field(init=False)

    def __post_init__(self) -> None:
        self.key = _key(self.key_type)
        ca_cert, ca_key = self.ca
        b = (
            x509.CertificateBuilder()
            .subject_name(_name("scruple test tsa"))
            .issuer_name(ca_cert.subject)
            .public_key(self.key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(NOW + timedelta(days=self.valid_days[0]))
            .not_valid_after(NOW + timedelta(days=self.valid_days[1]))
        )
        if self.eku != "none":
            usage = (
                ExtendedKeyUsageOID.CLIENT_AUTH
                if self.eku == "wrong"
                else ExtendedKeyUsageOID.TIME_STAMPING
            )
            b = b.add_extension(x509.ExtendedKeyUsage([usage]), critical=self.eku != "noncritical")
        self.cert = _sign_cert(b, ca_key)

    @property
    def ca_cert(self) -> x509.Certificate:
        return self.ca[0]

    def _sign(self, data: bytes) -> bytes:
        k = self.key
        if isinstance(k, rsa.RSAPrivateKey):
            return k.sign(data, padding.PKCS1v15(), hashes.SHA256())
        if isinstance(k, ec.EllipticCurvePrivateKey):
            return k.sign(data, ec.ECDSA(hashes.SHA256()))
        return k.sign(data)

    def respond(
        self,
        tsq: bytes,
        *,
        gen_time: datetime = NOW,
        serial: int = 4242,
        status: int = 0,
        signing_cert: Literal["v1", "v2", "none", "other"] = "v2",
        include_certs: bool = True,
        nonce: int | Literal["echo"] | None = "echo",
        digest: bytes | None = None,
        tamper_tst: bool = False,
        tamper_signature: bool = False,
        content_type_oid: str = ts.OID_TST_INFO,
    ) -> bytes:
        if status not in (0, 1):
            return der.seq(der.seq(der.integer(status), der.seq(der.utf8("policy says no"))))
        req = ts.parse_request(tsq)
        imprint = der.seq(
            der.seq(der.oid(req.hash_oid), der.null()), der.octets(digest or req.digest)
        )
        parts = [
            der.integer(1),
            der.oid(POLICY),
            imprint,
            der.integer(serial),
            der.gentime(gen_time),
        ]
        n = req.nonce if nonce == "echo" else nonce
        if n is not None:
            parts.append(der.integer(n))
        tst = der.seq(*parts)
        signed_tst = tst
        if tamper_tst:  # change the policy arc after signing the digest
            tst = tst.replace(der.oid(POLICY), der.oid("1.3.6.1.4.1.99999.2"))
        cert_der = self.cert.public_bytes(Encoding.DER)
        attrs = [
            der.seq(der.oid(ts.OID_CONTENT_TYPE), der.set_of(der.oid(content_type_oid))),
            der.seq(
                der.oid(ts.OID_MESSAGE_DIGEST),
                der.set_of(der.octets(hashlib.sha256(signed_tst).digest())),
            ),
        ]
        if signing_cert in ("v2", "other"):
            h = hashlib.sha256(cert_der if signing_cert == "v2" else b"another cert").digest()
            attrs.append(
                der.seq(
                    der.oid(ts.OID_SIGNING_CERT_V2),
                    der.set_of(der.seq(der.seq(der.seq(der.octets(h))))),
                )
            )
        elif signing_cert == "v1":
            h = hashlib.sha1(cert_der).digest()  # ESSCertID v1 is SHA-1 by spec
            attrs.append(
                der.seq(
                    der.oid(ts.OID_SIGNING_CERT),
                    der.set_of(der.seq(der.seq(der.seq(der.octets(h))))),
                )
            )
        signed_attrs = der.set_of(*attrs)
        sig = self._sign(signed_attrs)
        if tamper_signature:
            sig = sig[:-1] + bytes([sig[-1] ^ 1])
        sha256 = der.seq(der.oid(ts.HASH_OIDS["sha256"]), der.null())
        sig_alg = der.seq(der.oid(SIG_OIDS[self.key_type]))
        issuer_serial = der.seq(
            self.cert.issuer.public_bytes(), der.integer(self.cert.serial_number)
        )
        signer_info = der.seq(
            der.integer(1),
            issuer_serial,
            sha256,
            der.implicit(0, signed_attrs),
            sig_alg,
            der.octets(sig),
        )
        sd_parts = [
            der.integer(3),
            der.set_of(sha256),
            der.seq(der.oid(ts.OID_TST_INFO), der.explicit(0, der.octets(tst))),
        ]
        if include_certs:
            sd_parts.append(der.enc(0xA0, cert_der))
        sd_parts.append(der.set_of(signer_info))
        token = der.seq(der.oid(ts.OID_SIGNED_DATA), der.explicit(0, der.seq(*sd_parts)))
        return der.seq(der.seq(der.integer(status)), token)

    def transport(self, **kw: object) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            assert request.headers["content-type"] == ts.TSQ_CONTENT_TYPE
            body = self.respond(request.content, **kw)  # type: ignore[arg-type]
            return httpx.Response(200, content=body, headers={"content-type": ts.TSR_CONTENT_TYPE})

        return httpx.MockTransport(handle)
