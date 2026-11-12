"""Server-side search, facet filters and pagination for the console's long lists.

Every list page (rule versions, gate runs, cases, approval history) takes the same
query parameters:

    q      free text, up to 100 characters; every whitespace-separated term must
           appear (case-insensitive) in the row's searchable text
    <key>  one value per facet the list declares (status, stage, family, ...);
           a value must match `VALUE_PATTERN` and is compared exactly
    page   1-based page number (clamped to the last page)
    per    rows per page, one of `PER_PAGE`

Filtering happens on the server over the tenant's own rows; the templates print the
search term and every value escaped (Jinja autoescape), and links are built with
`urlencode`, so a term is never interpreted as markup or as another parameter.

A filter's canonical query string (`Filter.canonical`) is what a saved filter
stores: only `q` and the list's facet keys, in a fixed order, values validated.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from math import ceil
from urllib.parse import parse_qsl, urlencode

MAX_Q = 100
VALUE_PATTERN = r"^[A-Za-z0-9_.@:-]{0,64}$"
PER_PAGE = (10, 25, 50, 100)
DEFAULT_PER = 25
_VALUE = re.compile(VALUE_PATTERN)


@dataclass(frozen=True)
class Facet:
    key: str
    label: str


@dataclass(frozen=True)
class ListSpec:
    """One console list: its saved-filter name, base path and facets."""

    name: str
    facets: tuple[Facet, ...]

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(f.key for f in self.facets)


VERSIONS = ListSpec("versions", (Facet("status", "Status"), Facet("verdict", "Verdict")))
RUNS = ListSpec("runs", (Facet("stage", "Stage"), Facet("outcome", "Outcome")))
CASES = ListSpec(
    "cases",
    (
        Facet("family", "Case type"),
        Facet("action", "Action"),
        Facet("gate", "Gate"),
        Facet("correct", "Correct"),
    ),
)
APPROVALS = ListSpec("approvals", (Facet("event", "Event"),))
SPECS: dict[str, ListSpec] = {s.name: s for s in (VERSIONS, RUNS, CASES, APPROVALS)}


class FilterError(ValueError):
    pass


@dataclass(frozen=True)
class Filter:
    spec: ListSpec
    q: str = ""
    facets: tuple[tuple[str, str], ...] = ()
    page: int = 1
    per: int = DEFAULT_PER

    @classmethod
    def build(
        cls,
        spec: ListSpec,
        q: str | None,
        facets: Mapping[str, str | None],
        page: int | None = None,
        per: int | None = None,
    ) -> Filter:
        """Validated filter. Unknown facet keys are an error (the route declares its
        parameters, so this only fires for saved query strings)."""
        q = " ".join((q or "").split())
        if len(q) > MAX_Q:
            raise FilterError(f"search is limited to {MAX_Q} characters")
        unknown = set(facets) - set(spec.keys)
        if unknown:
            raise FilterError(f"unknown filter {', '.join(sorted(unknown))}")
        chosen: list[tuple[str, str]] = []
        for key in spec.keys:
            v = facets.get(key)
            if v:
                if not _VALUE.match(v):
                    raise FilterError(f"bad value for {key}")
                chosen.append((key, v))
        if per is not None and per not in PER_PAGE:
            raise FilterError(f"per must be one of {', '.join(map(str, PER_PAGE))}")
        if page is not None and page < 1:
            raise FilterError("page starts at 1")
        return cls(spec, q, tuple(chosen), page or 1, per or DEFAULT_PER)

    @classmethod
    def parse(cls, spec: ListSpec, query: str) -> Filter:
        """A filter from a (saved) query string; only q and facet keys."""
        # A character percent-encodes to at most 12 bytes (4 UTF-8 bytes as %XX).
        if len(query) > 12 * MAX_Q + 80 * len(spec.facets):
            raise FilterError("filter too long")
        pairs = parse_qsl(query, keep_blank_values=False)
        seen: dict[str, str] = {}
        for k, v in pairs:
            if k in seen:
                raise FilterError(f"{k} given twice")
            seen[k] = v
        q = seen.pop("q", None)
        return cls.build(spec, q, seen)

    def value(self, key: str) -> str:
        return dict(self.facets).get(key, "")

    @property
    def active(self) -> bool:
        return bool(self.q or self.facets)

    def canonical(self) -> str:
        """`q` and facets only: what a saved filter stores."""
        pairs = ([("q", self.q)] if self.q else []) + list(self.facets)
        return urlencode(pairs)

    def query(self, page: int | None = None) -> str:
        """The full query string for a link (page and per only when not default)."""
        pairs = ([("q", self.q)] if self.q else []) + list(self.facets)
        p = self.page if page is None else page
        if self.per != DEFAULT_PER:
            pairs.append(("per", str(self.per)))
        if p != 1:
            pairs.append(("page", str(p)))
        return urlencode(pairs)


@dataclass(frozen=True)
class Page[T]:
    items: list[T]
    total: int  # rows before filtering
    matched: int  # rows after filtering
    pages: int
    filter: Filter
    options: dict[str, tuple[str, ...]]  # facet key -> values present in the data

    @property
    def page(self) -> int:
        return self.filter.page

    @property
    def first(self) -> int:
        return 0 if not self.matched else (self.page - 1) * self.filter.per + 1

    @property
    def last(self) -> int:
        return min(self.page * self.filter.per, self.matched)

    def href(self, page: int) -> str:
        """The query part of a link to `page` (append to the page's own path)."""
        qs = self.filter.query(page)
        return f"?{qs}" if qs else ""

    def window(self, width: int = 2) -> list[int]:
        """Page numbers to link: first, last and `width` either side of the current."""
        keep = {1, self.pages, *range(self.page - width, self.page + width + 1)}
        return sorted(p for p in keep if 1 <= p <= self.pages)


def matches(text: str, q: str) -> bool:
    hay = text.casefold()
    return all(term.casefold() in hay for term in q.split())


def paginate[T](
    rows: Sequence[T],
    filt: Filter,
    text: Callable[[T], str],
    fields: Callable[[T], Mapping[str, str]],
) -> Page[T]:
    """Filter, then page. The page number is clamped to the last page."""
    options: dict[str, set[str]] = {k: set() for k in filt.spec.keys}
    kept: list[T] = []
    want = dict(filt.facets)
    for row in rows:
        f = fields(row)
        for k in options:
            if f.get(k):
                options[k].add(f[k])
        if any(f.get(k) != v for k, v in want.items()):
            continue
        if filt.q and not matches(text(row), filt.q):
            continue
        kept.append(row)
    pages = max(1, ceil(len(kept) / filt.per))
    page = min(filt.page, pages)
    filt = replace(filt, page=page)
    start = (page - 1) * filt.per
    return Page(
        items=kept[start : start + filt.per],
        total=len(rows),
        matched=len(kept),
        pages=pages,
        filter=filt,
        options={k: tuple(sorted(v)) for k, v in options.items()},
    )


def joined(parts: Iterable[object]) -> str:
    return " ".join(str(p) for p in parts if p is not None)


__all__ = [
    "APPROVALS",
    "CASES",
    "DEFAULT_PER",
    "MAX_Q",
    "PER_PAGE",
    "RUNS",
    "SPECS",
    "VALUE_PATTERN",
    "VERSIONS",
    "Facet",
    "Filter",
    "FilterError",
    "ListSpec",
    "Page",
    "joined",
    "matches",
    "paginate",
]
