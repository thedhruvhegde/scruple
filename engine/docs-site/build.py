"""Assemble and build the public docs site: `make docs`.

1. Copy the hand-written pages (docs-site/pages) and the public docs from docs/
   (install, console, architecture, the pilot offer) into docs-site/build/docs.
   Links between copied pages are rewritten to their place on the site; links to files
   that are not on the site become the plain path in code, so nothing points at a
   page that does not exist.
2. Generate the HTTP API reference (api.md) from the server's own OpenAPI schema,
   with the permission each route declares (`scruple.auth.deps.protection_of`), and
   publish the schema itself as openapi.json.
3. Copy the console's self-hosted fonts (OFL, with their licences).
4. Run `mkdocs build --strict` (a broken link or a missing page fails the build).

Usage: uv run python docs-site/build.py [--no-build]
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
BUILD = HERE / "build"

# repository path -> site path (relative to docs_dir)
COPIED: dict[str, str] = {
    "docs/INSTALL.md": "install.md",
    "docs/CONSOLE.md": "console.md",
    "docs/ARCHITECTURE.md": "architecture.md",
    "docs/pilot/README.md": "pilot/README.md",
    "docs/pilot/SCOPE.md": "pilot/SCOPE.md",
    "docs/pilot/SUCCESS_CRITERIA.md": "pilot/SUCCESS_CRITERIA.md",
    "docs/pilot/DATA_CHECKLIST.md": "pilot/DATA_CHECKLIST.md",
    "docs/pilot/SECURITY.md": "pilot/SECURITY.md",
}
FONTS = (
    "martian-mono.woff2",
    "plex-sans-condensed-400.woff2",
    "plex-sans-condensed-500.woff2",
    "plex-sans-condensed-600.woff2",
    "OFL-martian-mono.txt",
    "OFL-ibm-plex-sans-condensed.txt",
)
LINK = re.compile(r"(?<!!)\[([^\]\n]+)\]\(([^)\s]+)\)")


def rewrite_links(text: str, source: str) -> str:
    """Point links at their site page, or turn them into a code path."""
    site_of = {k: v for k, v in COPIED.items()}
    here = posixpath.dirname(source)
    page = COPIED[source]

    def fix(m: re.Match[str]) -> str:
        label, target = m.group(1), m.group(2)
        if re.match(r"^[a-z][a-z0-9+.-]*:", target) or target.startswith("#"):
            return m.group(0)
        path, _, anchor = target.partition("#")
        repo_path = posixpath.normpath(posixpath.join(here, path)) if path else source
        if repo_path in site_of:
            rel = posixpath.relpath(site_of[repo_path], posixpath.dirname(page) or ".")
            return f"[{label}]({rel}{'#' + anchor if anchor else ''})"
        shown = repo_path if not repo_path.startswith("..") else path
        return f"{label} (`{shown}`)" if label.strip("`") != shown else f"`{shown}`"

    return LINK.sub(fix, text)


# --- API reference ---------------------------------------------------------------------


def openapi_and_permissions() -> tuple[dict[str, Any], dict[tuple[str, str], str]]:
    from scruple.api.app import create_app
    from scruple.auth.authn import Authenticator
    from scruple.auth.deps import iter_routes, protection_of
    from scruple.service import AgentGate
    from scruple.storage import SQLiteStorage

    app = create_app(AgentGate(SQLiteStorage(":memory:")), auth=Authenticator())
    perms: dict[tuple[str, str], str] = {}
    for route in iter_routes(app):
        reqs = protection_of(route)
        if not reqs:
            continue
        r = reqs[0]
        label = (
            f"`{r.permission.value}`"
            if r.kind == "permission" and r.permission is not None
            else ("public" if r.kind == "public" else f"signed ({r.scheme})")
        )
        for method in route.methods or ():
            perms[(method.lower(), route.path)] = label
    return app.openapi(), perms


def _type(schema: dict[str, Any]) -> str:
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        return f"[{name}](#{_anchor(name)})"
    if "anyOf" in schema:
        return " or ".join(_type(s) for s in schema["anyOf"])
    if "allOf" in schema and len(schema["allOf"]) == 1:
        return _type(schema["allOf"][0])
    t = schema.get("type", "any")
    if t == "array":
        return f"array of {_type(schema.get('items', {}))}"
    if "enum" in schema:
        return " \\| ".join(f"`{v}`" for v in schema["enum"])
    if "format" in schema:
        return f"{t} ({schema['format']})"
    return str(t)


def _anchor(name: str) -> str:
    return "schema-" + re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _cell(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def api_markdown(spec: dict[str, Any], perms: dict[tuple[str, str], str]) -> str:
    lines = [
        "# HTTP API reference",
        "",
        f"Generated from the server's OpenAPI schema (`{spec['info']['title']}` "
        f"{spec['info']['version']}, OpenAPI {spec['openapi']}) by `docs-site/build.py`. "
        "The schema itself: [openapi.json](openapi.json). A running server serves it at "
        "`GET /openapi.json` to authenticated readers.",
        "",
        "Every route declares what it needs: a permission (an API key, OIDC token or "
        "console session whose roles grant it), `public`, or `signed` (the route checks "
        "a signature of its own, as provider webhooks do). Without credentials a "
        "protected route answers 401; without the permission, 403. Unknown query "
        "parameters and unknown body fields are refused with 422. Every route sees only "
        "the caller's tenant.",
        "",
        "Not listed: the HTML console under `/console/` (see [Console](console.md)) and "
        "the SAML routes under `/auth/saml/`, mounted only when SAML is configured "
        "(see [Architecture](architecture.md)).",
        "",
    ]
    groups: dict[str, list[tuple[str, str, dict[str, Any]]]] = {}
    for path, ops in sorted(spec["paths"].items()):
        top = path.strip("/").split("/")[0] or "root"
        for method, op in ops.items():
            groups.setdefault(top, []).append((method, path, op))
    for top in sorted(groups):
        lines += [f"## /{top}", ""]
        for method, path, op in groups[top]:
            lines += [f"### `{method.upper()} {path}`", ""]
            lines.append(f"Permission: {perms.get((method, path), 'not declared')}")
            lines.append("")
            if op.get("summary"):
                lines += [_cell(op["summary"]) + ".", ""]
            if op.get("description"):
                lines += [op["description"].strip(), ""]
            params = op.get("parameters", [])
            if params:
                lines += ["| parameter | in | type | required |", "|---|---|---|---|"]
                for p in params:
                    lines.append(
                        f"| `{p['name']}` | {p['in']} | {_type(p.get('schema', {}))} | "
                        f"{'yes' if p.get('required') else 'no'} |"
                    )
                lines.append("")
            body = op.get("requestBody", {}).get("content", {})
            for ctype, media in body.items():
                lines += [f"Request body ({ctype}): {_type(media.get('schema', {}))}", ""]
            for status, resp in sorted(op.get("responses", {}).items()):
                content = resp.get("content", {})
                kinds = [
                    f"{_type(m.get('schema', {}))} ({c})"
                    for c, m in content.items()
                    if m.get("schema")
                ]
                what = "; ".join(kinds) if kinds else _cell(resp.get("description", ""))
                lines.append(f"- `{status}`: {what}")
            lines.append("")
    schemas = spec.get("components", {}).get("schemas", {})
    if schemas:
        lines += ["## Schemas", ""]
        for name in sorted(schemas):
            s = schemas[name]
            lines += [f'<a id="{_anchor(name)}"></a>', "", f"### {name}", ""]
            if s.get("description"):
                lines += [s["description"].strip(), ""]
            props = s.get("properties", {})
            if props:
                req = set(s.get("required", []))
                lines += ["| field | type | required |", "|---|---|---|"]
                for field, fs in props.items():
                    lines.append(f"| `{field}` | {_type(fs)} | {'yes' if field in req else 'no'} |")
                lines.append("")
            elif "enum" in s:
                lines += [f"One of {_type(s)}.", ""]
    return "\n".join(lines) + "\n"


# --- assemble and build -------------------------------------------------------------------


def assemble(out: Path) -> Path:
    """Write the site's markdown, schema and fonts into `out` (replaced)."""
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(HERE / "pages", out)
    OUT = out  # noqa: N806
    for source, dest in COPIED.items():
        target = OUT / dest
        target.parent.mkdir(parents=True, exist_ok=True)
        text = (ROOT / source).read_text(encoding="utf-8")
        target.write_text(rewrite_links(text, source), encoding="utf-8")
    spec, perms = openapi_and_permissions()
    (OUT / "openapi.json").write_text(json.dumps(spec, indent=1, sort_keys=True) + "\n")
    (OUT / "api.md").write_text(api_markdown(spec, perms), encoding="utf-8")
    fonts = OUT / "assets" / "fonts"
    fonts.mkdir(parents=True, exist_ok=True)
    for f in FONTS:
        shutil.copy2(ROOT / "scruple" / "console" / "static" / "fonts" / f, fonts / f)
    return OUT


def build(root: Path = BUILD, *, run_mkdocs: bool = True) -> int:
    """Assemble into `root/docs` and build the site into `root/site`."""
    assemble(root / "docs")
    if not run_mkdocs:
        return 0
    config = (HERE / "mkdocs.yml").read_text(encoding="utf-8")
    if root != BUILD:  # same config, other directories (tests build into a tmp dir)
        config = config.replace("docs_dir: build/docs", f"docs_dir: {root / 'docs'}")
        config = config.replace("site_dir: build/site", f"site_dir: {root / 'site'}")
        cfg = root / "mkdocs.yml"
        cfg.write_text(config, encoding="utf-8")
    else:
        cfg = HERE / "mkdocs.yml"
    env = {**os.environ, "NO_MKDOCS_2_WARNING": "1"}
    return subprocess.call(  # noqa: S603 - our own interpreter and config
        [sys.executable, "-m", "mkdocs", "build", "--strict", "-f", str(cfg)], env=env
    )


def main(argv: list[str]) -> int:
    return build(run_mkdocs="--no-build" not in argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
