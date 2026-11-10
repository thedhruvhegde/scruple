"""scripts/changelog.py: CHANGELOG.md from the keyed commit history."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
from hypothesis import given
from hypothesis import strategies as st

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "changelog.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("changelog_script", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses look the module up by name
    spec.loader.exec_module(mod)
    return mod


cl = _load()

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not on PATH")


@pytest.mark.parametrize(
    ("subject", "section", "text"),
    [
        ("[+] add a thing", "Added", "add a thing"),
        ("[!] fix the thing", "Fixed", "fix the thing"),
        ("[~] speed up the thing", "Changed", "speed up the thing"),
        ("[#] explain the thing", "Docs", "explain the thing"),
        ("  [+]   padded   ", "Added", "padded"),
        ("Add marketing site", "Other", "Add marketing site"),
        ("[x] unknown key", "Other", "[x] unknown key"),
        ("[+]no space", "Other", "[+]no space"),
        ("[+] ", "Other", "[+]"),
        ("prefix [+] later", "Other", "prefix [+] later"),
    ],
)
def test_parse_subject(subject: str, section: str, text: str) -> None:
    e = cl.parse_subject(subject, "abc")
    assert (e.section, e.text, e.commit) == (section, text, "abc")


def test_render_groups_in_fixed_order_and_skips_empty_releases() -> None:
    rel = cl.Release(
        "v1.0.0",
        "2026-01-02",
        (
            cl.Entry("Docs", "write docs", "d" * 40),
            cl.Entry("Added", "add a", "a" * 40),
            cl.Entry("Other", "old style", ""),
            cl.Entry("Fixed", "fix b", "b" * 40),
            cl.Entry("Added", "add c", "c" * 40),
        ),
    )
    md = cl.render([cl.Release("Unreleased"), rel])
    assert md.startswith(cl.HEADER)
    assert "## Unreleased" not in md
    assert "## v1.0.0 (2026-01-02)" in md
    heads = [ln for ln in md.splitlines() if ln.startswith("### ")]
    assert heads == ["### Added", "### Fixed", "### Docs", "### Other"]
    assert md.index("add a") < md.index("add c")  # history order kept inside a section
    assert "- add a (aaaaaaa)" in md
    assert "- old style\n" in md


def test_render_escapes_markup() -> None:
    md = cl.render([cl.Release("x", "", (cl.Entry("Added", "add `x` and <b>[y]</b>_z"),))])
    assert "- add \\`x\\` and \\<b\\>\\[y\\]\\</b\\>\\_z\n" in md


@given(
    st.lists(
        st.tuples(
            st.sampled_from(["[+] ", "[!] ", "[~] ", "[#] ", ""]),
            st.text(
                alphabet=st.characters(blacklist_categories=("Cs", "Cc", "Zl", "Zp")),
                min_size=1,
                max_size=40,
            ).filter(lambda t: t.strip()),
        ),
        max_size=30,
    )
)
def test_every_commit_lands_in_exactly_one_bullet(subjects: list[tuple[str, str]]) -> None:
    entries = tuple(
        cl.parse_subject(k + t, f"{i:07x}".ljust(40, "f")) for i, (k, t) in enumerate(subjects)
    )
    md = cl.render([cl.Release("Unreleased", "", entries)])
    bullets = [ln for ln in md.splitlines() if ln.startswith("- ")]
    assert len(bullets) == len(entries)
    for i in range(len(entries)):
        assert sum(ln.endswith(f"({i:07x})") for ln in bullets) == 1
    assert md == cl.render([cl.Release("Unreleased", "", entries)])  # deterministic


# --- against real git repositories -------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
        "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
    }
    return subprocess.run(
        ["git", "-C", str(repo), *args], env=env, capture_output=True, text=True, check=True
    ).stdout


def _commit(repo: Path, subject: str) -> None:
    _git(repo, "commit", "-q", "--allow-empty", "--no-gpg-sign", "-m", subject)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    return tmp_path


@needs_git
def test_groups_by_tag_newest_first(repo: Path) -> None:
    _commit(repo, "Scaffold project")
    _commit(repo, "[+] add the registry")
    _git(repo, "tag", "v0.1.0")
    _commit(repo, "[!] fix the registry")
    _commit(repo, "[#] document the registry")
    _git(repo, "tag", "-a", "v0.2.0", "-m", "release")  # annotated tags work too
    _commit(repo, "[~] tidy the registry")

    releases = cl.collect(repo)
    assert [r.title for r in releases] == ["Unreleased", "v0.2.0", "v0.1.0"]
    assert [e.text for e in releases[0].entries] == ["tidy the registry"]
    assert {e.section for e in releases[1].entries} == {"Fixed", "Docs"}
    assert [(e.section, e.text) for e in releases[2].entries] == [
        ("Added", "add the registry"),
        ("Other", "Scaffold project"),
    ]
    assert releases[1].date == "2026-01-01"

    md = cl.render(releases)
    assert md.index("## Unreleased") < md.index("## v0.2.0") < md.index("## v0.1.0")


@needs_git
def test_tags_off_the_current_branch_are_ignored_and_merges_skipped(repo: Path) -> None:
    _commit(repo, "[+] base")
    _git(repo, "checkout", "-q", "-b", "side")
    _commit(repo, "[+] side work")
    _git(repo, "tag", "side-tag")
    _git(repo, "checkout", "-q", "main")
    _commit(repo, "[+] main work")
    releases = cl.collect(repo)
    assert [r.title for r in releases] == ["Unreleased"]
    _git(repo, "merge", "-q", "--no-ff", "--no-edit", "side")
    texts = [e.text for r in cl.collect(repo) for e in r.entries]
    assert "side work" in texts
    assert not [t for t in texts if t.startswith("Merge")]


@needs_git
def test_empty_repository(repo: Path) -> None:
    assert cl.collect(repo) == []
    assert cl.render([]) == cl.HEADER


@needs_git
def test_cli_write_check_and_notes(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _commit(repo, "[+] first")
    _git(repo, "tag", "v1.0.0")
    _commit(repo, "[!] second")
    out = tmp_path / "out" / "CHANGELOG.md"
    out.parent.mkdir()

    assert cl.main(["--repo", str(repo), "--output", str(out), "--check"]) == 1
    assert cl.main(["--repo", str(repo), "--output", str(out)]) == 0
    assert cl.main(["--repo", str(repo), "--output", str(out), "--check"]) == 0
    assert "### Fixed" in out.read_text()

    capsys.readouterr()
    assert cl.main(["--repo", str(repo), "--notes", "v1.0.0"]) == 0
    notes = capsys.readouterr().out
    assert notes.startswith("## v1.0.0")
    assert "first" in notes and "second" not in notes
    assert cl.main(["--repo", str(repo), "--notes", "v9.9.9"]) == 1

    _commit(repo, "[+] third")
    assert cl.main(["--repo", str(repo), "--output", str(out), "--check"]) == 1


@needs_git
def test_this_repository_has_only_known_sections() -> None:
    if not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    releases = cl.collect(ROOT)
    assert releases
    sections = {e.section for r in releases for e in r.entries}
    assert sections <= set(cl.ORDER)


def test_changelog_file_was_generated_by_the_script() -> None:
    text = (ROOT / "CHANGELOG.md").read_text()
    assert text.startswith(cl.HEADER)
