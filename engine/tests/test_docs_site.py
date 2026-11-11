"""The public docs site (docs-site/): link rewriting, the generated API reference
covers every route with its permission, the strict mkdocs build passes, and the
publishing workflow has the expected shape."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "docs_site_build", ROOT / "docs-site" / "build.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["docs_site_build"] = mod
    spec.loader.exec_module(mod)
    return mod


B = _load()


@pytest.mark.parametrize(
    ("text", "source", "expected"),
    [
        ("see [scope](SCOPE.md)", "docs/pilot/README.md", "see [scope](SCOPE.md)"),
        (
            "see [install](../INSTALL.md#docker)",
            "docs/pilot/README.md",
            "see [install](../install.md#docker)",
        ),
        ("[console](CONSOLE.md)", "docs/ARCHITECTURE.md", "[console](console.md)"),
        ("[terms](TERMS_DRAFT.md)", "docs/pilot/README.md", "terms (`docs/pilot/TERMS_DRAFT.md`)"),
        ("[`x.py`](../scruple/x.py)", "docs/INSTALL.md", "`x.py` (`scruple/x.py`)"),
        ("[`scruple/x.py`](../scruple/x.py)", "docs/INSTALL.md", "`scruple/x.py`"),
        ("[web](https://example.org/a)", "docs/INSTALL.md", "[web](https://example.org/a)"),
        ("[here](#section)", "docs/INSTALL.md", "[here](#section)"),
        ("![img](pic.png)", "docs/INSTALL.md", "![img](pic.png)"),
    ],
)
def test_links_are_rewritten(text: str, source: str, expected: str) -> None:
    assert B.rewrite_links(text, source) == expected


def test_every_copied_doc_exists_and_every_nav_page_is_built() -> None:
    for source in B.COPIED:
        assert (ROOT / source).is_file(), source
    cfg = yaml.safe_load((ROOT / "docs-site" / "mkdocs.yml").read_text())

    def pages(nav: object) -> list[str]:
        if isinstance(nav, str):
            return [nav]
        if isinstance(nav, list):
            return [p for item in nav for p in pages(item)]
        if isinstance(nav, dict):
            return [p for v in nav.values() for p in pages(v)]
        return []

    hand = {
        p.relative_to(ROOT / "docs-site" / "pages").as_posix()
        for p in (ROOT / "docs-site" / "pages").rglob("*.md")
    }
    built = hand | set(B.COPIED.values()) | {"api.md"}
    assert set(pages(cfg["nav"])) <= built
    assert cfg["strict"] is True


def test_api_reference_lists_every_route_with_its_permission() -> None:
    spec, perms = B.openapi_and_permissions()
    md = B.api_markdown(spec, perms)
    for path, ops in spec["paths"].items():
        for method in ops:
            assert f"### `{method.upper()} {path}`" in md
    assert "not declared" not in md
    assert "### `GET /usage`\n\nPermission: `admin`" in md
    assert "### `POST /rules`\n\nPermission: `propose`" in md
    assert "### `GET /health`\n\nPermission: public" in md
    for name in spec["components"]["schemas"]:
        assert f'<a id="{B._anchor(name)}"></a>' in md


def test_strict_build_passes(tmp_path: Path) -> None:
    assert B.build(tmp_path) == 0
    site = tmp_path / "site"
    for page in (
        "index.html",
        "quickstart/index.html",
        "api/index.html",
        "pilot/index.html",
    ):
        assert (site / page).is_file(), page
    assert (site / "openapi.json").is_file()
    assert (site / "assets" / "fonts" / "martian-mono.woff2").is_file()
    html = (site / "api" / "index.html").read_text()
    assert "GET /rules" in html and "fonts.googleapis" not in html


def test_publishing_workflow_shape() -> None:
    wf = yaml.safe_load((ROOT / "deploy" / "github-workflows" / "docs.yml").read_text())
    assert wf["permissions"] == {"contents": "read"}
    build = wf["jobs"]["build"]
    assert {"run": "make docs"} in build["steps"]
    deploy = wf["jobs"]["deploy"]
    assert deploy["permissions"] == {"pages": "write", "id-token": "write"}
    assert "pull_request" in deploy["if"]
    for job in wf["jobs"].values():
        for step in job["steps"]:
            uses = step.get("uses")
            assert uses is None or "@v" in uses, uses
