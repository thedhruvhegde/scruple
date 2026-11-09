"""`scruple ingest stripe|adyen|csv|jsonl --in FILE --store DB` and `scruple ingest verify`.

Exit codes: 0 every record stored; 1 some records failed (the rest were stored, unless
`--strict`); 2 the file or the options were rejected and nothing was stored. Keys come
from the environment only (`scruple.ingest.keys`).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from scruple.ingest.errors import IngestError
from scruple.ingest.keys import adyen_keys_from_env, cipher_from_env, tokenizer_from_env
from scruple.ingest.pipeline import IngestOptions, ingest
from scruple.ingest.store import CaseStore
from scruple.ingest.tabular import ImportConfig
from scruple.ingest.verify import summarise

SOURCES = ("stripe", "adyen", "csv", "jsonl")


def cmd_ingest(args: argparse.Namespace) -> int:
    path = Path(args.input)
    data = path.read_bytes()
    cfg = None
    if args.source in ("csv", "jsonl"):
        if not args.config:
            raise IngestError(f"{args.source} ingest needs --config (an ImportConfig JSON file)")
        cfg = ImportConfig.model_validate_json(Path(args.config).read_text(encoding="utf-8"))
    opts = IngestOptions(
        source=args.source,
        label=args.label or f"{path.name} ({args.source} export)",
        import_config=cfg,
        pan_columns=tuple(args.pan_column or ()),
        encrypt_columns=tuple(args.encrypt_column or ()),
        encrypt_paths=tuple(args.encrypt_path or ()),
        allow_live=args.allow_live,
        verify_adyen=not args.no_verify,
        strict=args.strict,
    )
    store = None if args.dry_run else CaseStore(args.store)
    try:
        report = ingest(
            data,
            opts,
            store,
            tokenizer=tokenizer_from_env(),
            cipher=cipher_from_env(),
            adyen_hmac_keys=adyen_keys_from_env() if args.source == "adyen" else (),
        )
    finally:
        if store is not None:
            store.close()
    print(report.summary())
    for e in report.errors[: args.show_errors]:
        col = f" [{e.column}]" if e.column else ""
        print(f"  row {e.row}{col}: {e.message}")
    if len(report.errors) > args.show_errors:
        print(f"  ... {len(report.errors) - args.show_errors} more in the report")
    for where, n in report.pans_in_input_not_stored.items():
        print(f"  card number in input field {where} ({n}x): not stored")
    if args.report:
        out = Path(args.report)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
        print(f"report written to {out}")
    return 1 if report.rows_failed else 0


def cmd_verify(args: argparse.Namespace) -> int:
    with CaseStore(args.store, create=False) as store:
        summary = summarise(store, k=args.k, cipher=cipher_from_env())
    if args.json:
        print(summary.model_dump_json(indent=2))
    else:
        print(f"case store {args.store}")
        for line in summary.lines():
            print(line)
    return 0 if summary.ok else 1


def _guard(fn: Any) -> Any:
    def run(args: argparse.Namespace) -> int:
        try:
            return int(fn(args))
        except IngestError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        except ValueError as e:  # pydantic validation of the options or config
            print(f"error: {str(e)[:500]}", file=sys.stderr)
            return 2

    return run


def add_ingest_parser(sub: Any) -> None:
    ig = sub.add_parser("ingest", help="validate, redact and store a dispute export")
    isub = ig.add_subparsers(dest="ingest_cmd", required=True)
    for source in SOURCES:
        sp = isub.add_parser(source, help=f"ingest a {source} export into a case store")
        sp.add_argument("--in", dest="input", required=True, help="the export file")
        sp.add_argument("--store", required=True, help="case store (SQLite file, created)")
        sp.add_argument("--report", default=None, help="write the JSON ingest report here")
        sp.add_argument("--label", default=None, help="dataset name kept with the run")
        sp.add_argument("--strict", action="store_true", help="store nothing if a record fails")
        sp.add_argument("--dry-run", action="store_true", help="validate only, write nothing")
        sp.add_argument("--allow-live", action="store_true", help="accept live-mode records")
        sp.add_argument("--show-errors", type=int, default=20, help="row errors to print")
        if source in ("csv", "jsonl"):
            sp.add_argument("--config", required=True, help="ImportConfig JSON")
            sp.add_argument(
                "--pan-column", action="append", help="column holding card numbers (tokenised)"
            )
            sp.add_argument("--encrypt-column", action="append", help="column kept only encrypted")
        else:
            sp.add_argument(
                "--encrypt-path",
                action="append",
                help="dotted path in each record kept only encrypted (repeatable)",
            )
        if source == "adyen":
            sp.add_argument("--no-verify", action="store_true", help="skip HMAC checks")
        sp.set_defaults(
            fn=_guard(cmd_ingest),
            source=source,
            pan_column=None,
            encrypt_column=None,
            encrypt_path=None,
            no_verify=False,
            config=None,
        )
    v = isub.add_parser("verify", help="summarise a case store (suppression-safe)")
    v.add_argument("--store", required=True)
    v.add_argument("--k", type=int, default=10, help="small-cell threshold (default 10)")
    v.add_argument("--json", action="store_true")
    v.set_defaults(fn=_guard(cmd_verify))


__all__ = ["add_ingest_parser"]
