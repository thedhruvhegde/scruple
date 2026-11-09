"""ISO 20022 camt.053 (BankToCustomerStatement) parser.

XML is parsed with defusedxml only: DTDs, entity declarations and external references
are refused, so billion-laughs and XXE payloads fail before any expansion.

Element names follow the ISO 20022 camt.053 message definition (versions 001.02 to
001.13 share these paths): `BkToCstmrStmt/Stmt` with `Id`, `Acct/Id/(IBAN|Othr/Id)`,
`Acct/Ccy`, `Bal` (`Tp/CdOrPrtry/(Cd|Prtry)`, `Amt@Ccy`, `CdtDbtInd`, `Dt/(Dt|DtTm)`)
and `Ntry` (`Amt@Ccy`, `CdtDbtInd`, `Sts` or `Sts/Cd`, `BookgDt`, `ValDt`,
`AcctSvcrRef`, `NtryRef`, `BkTxCd`, `NtryDtls/TxDtls` with `Refs/EndToEndId`,
`RtrInf/Rsn/Cd`, `RmtInf/Ustrd`, plus `AddtlNtryInf`). A business application header
wrapper (as in prowide-iso20022's public sample) is allowed: the first `Document` in a
camt.053 namespace is used.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from xml.etree.ElementTree import Element

from defusedxml import DefusedXmlException
from defusedxml import ElementTree as SafeET
from pydantic import ValidationError

from scruple.ingest.bank.models import (
    BankStatement,
    Direction,
    StatementBalance,
    StatementEntry,
    truncate,
)
from scruple.ingest.currency import decimal_to_minor, normalise_currency
from scruple.ingest.errors import (
    InputTooLargeError,
    MalformedInputError,
    SchemaMismatchError,
)
from scruple.ingest.limits import MAX_FILE_BYTES, check_size

_NS = re.compile(r"^\{urn:iso:std:iso:20022:tech:xsd:camt\.053\.001\.(0[2-9]|1[0-3])\}")
MAX_ENTRIES = 200_000
MAX_STATEMENTS = 1_000


def _local(el: Element) -> str:
    return el.tag.rsplit("}", 1)[-1] if isinstance(el.tag, str) else ""


def _kids(el: Element, name: str) -> list[Element]:
    return [c for c in el if _local(c) == name]


def _find(el: Element | None, path: str) -> Element | None:
    cur = el
    for part in path.split("/"):
        if cur is None:
            return None
        found = _kids(cur, part)
        cur = found[0] if found else None
    return cur


def _text(el: Element | None, path: str) -> str | None:
    node = _find(el, path)
    if node is None or node.text is None:
        return None
    t = node.text.strip()
    return t or None


def _date(el: Element | None) -> date | None:
    if el is None:
        return None
    d = _text(el, "Dt")
    try:
        if d is not None:
            return date.fromisoformat(d[:10])
        dt = _text(el, "DtTm")
        return datetime.fromisoformat(dt).date() if dt else None
    except ValueError:
        raise SchemaMismatchError("camt.053 date is not ISO 8601") from None


def _amount(el: Element, where: str) -> tuple[int, str]:
    amt = _find(el, "Amt")
    if amt is None or amt.text is None:
        raise SchemaMismatchError(f"{where} has no Amt")
    ccy = normalise_currency(amt.get("Ccy", ""))
    value = decimal_to_minor(amt.text, ccy)
    if value < 0:
        raise SchemaMismatchError(f"{where} Amt must not be negative (use CdtDbtInd)")
    return value, ccy


def _direction(el: Element, where: str) -> Direction:
    ind = _text(el, "CdtDbtInd")
    if ind == "CRDT":
        return Direction.CREDIT
    if ind == "DBIT":
        return Direction.DEBIT
    raise SchemaMismatchError(f"{where} CdtDbtInd must be CRDT or DBIT")


def _type_code(ntry: Element) -> str | None:
    bk = _find(ntry, "BkTxCd")
    domn = _text(bk, "Domn/Cd")
    if domn:
        parts = [domn, _text(bk, "Domn/Fmly/Cd"), _text(bk, "Domn/Fmly/SubFmlyCd")]
        return "/".join(p for p in parts if p)
    return _text(bk, "Prtry/Cd")


def _statement(stmt: Element) -> BankStatement:
    acct = _find(stmt, "Acct")
    account_id = _text(acct, "Id/IBAN") or _text(acct, "Id/Othr/Id")
    if not account_id:
        raise SchemaMismatchError("statement has no account id")
    ccy_text = _text(acct, "Ccy")
    balances: list[StatementBalance] = []
    entries: list[StatementEntry] = []
    ccy = normalise_currency(ccy_text) if ccy_text else None
    for bal in _kids(stmt, "Bal"):
        kind = _text(bal, "Tp/CdOrPrtry/Cd") or _text(bal, "Tp/CdOrPrtry/Prtry")
        if not kind:
            raise SchemaMismatchError("balance has no type code")
        value, bccy = _amount(bal, "Bal")
        ccy = ccy or bccy
        if bccy != ccy:
            raise SchemaMismatchError("balance currency differs from the account currency")
        sign = -1 if _direction(bal, "Bal") is Direction.DEBIT else 1
        balances.append(
            StatementBalance(
                kind=kind[:35], amount_minor=sign * value, as_of=_date(_find(bal, "Dt"))
            )
        )
    ntries = _kids(stmt, "Ntry")
    if len(ntries) > MAX_ENTRIES:
        raise InputTooLargeError(f"more than {MAX_ENTRIES} entries in one statement")
    for ntry in ntries:
        value, eccy = _amount(ntry, "Ntry")
        ccy = ccy or eccy
        if eccy != ccy:
            raise SchemaMismatchError("entry currency differs from the account currency")
        tx = _find(ntry, "NtryDtls/TxDtls")
        desc = _text(tx, "RmtInf/Ustrd") or _text(ntry, "AddtlNtryInf")
        entries.append(
            StatementEntry(
                amount_minor=value,
                direction=_direction(ntry, "Ntry"),
                booking_date=_date(_find(ntry, "BookgDt")),
                value_date=_date(_find(ntry, "ValDt")),
                type_code=truncate(_type_code(ntry), 64),
                bank_ref=truncate(_text(ntry, "AcctSvcrRef"), 64),
                customer_ref=truncate(_text(ntry, "NtryRef"), 64),
                end_to_end_id=truncate(_text(tx, "Refs/EndToEndId"), 64),
                return_reason=truncate(_text(tx, "RtrInf/Rsn/Cd"), 35),
                status=truncate(_text(ntry, "Sts/Cd") or _text(ntry, "Sts"), 16),
                description=truncate(desc, 500),
            )
        )
    if ccy is None:
        raise SchemaMismatchError("statement currency is unknown (no Acct/Ccy and no amounts)")
    return BankStatement(
        format="camt053",
        statement_id=truncate(_text(stmt, "Id"), 64),
        account_id=account_id[:64],
        currency=ccy,
        balances=tuple(balances),
        entries=tuple(entries),
    )


def parse_camt053(
    data: bytes | str, *, max_bytes: int = MAX_FILE_BYTES
) -> tuple[BankStatement, ...]:
    """Parse a camt.053 document into statements. Raises `IngestError`."""
    check_size(data, max_bytes, "camt.053 file")
    try:
        root = SafeET.fromstring(data, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except DefusedXmlException as e:
        raise MalformedInputError(f"unsafe XML refused: {type(e).__name__}") from None
    except (SafeET.ParseError, ValueError, UnicodeError, LookupError) as e:
        raise MalformedInputError(f"not well-formed XML: {str(e)[:100]}") from None
    doc = next((el for el in root.iter() if _local(el) == "Document" and _NS.match(el.tag)), None)
    if doc is None:
        raise SchemaMismatchError("no camt.053.001.02-13 Document element")
    body = _find(doc, "BkToCstmrStmt")
    stmts = _kids(body, "Stmt") if body is not None else []
    if not stmts:
        raise SchemaMismatchError("no BkToCstmrStmt/Stmt elements")
    if len(stmts) > MAX_STATEMENTS:
        raise InputTooLargeError(f"more than {MAX_STATEMENTS} statements")
    try:
        return tuple(_statement(s) for s in stmts)
    except ValidationError as e:
        raise SchemaMismatchError(
            f"camt.053 value out of range: {e.error_count()} errors"
        ) from None
