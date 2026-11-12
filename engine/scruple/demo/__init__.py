"""The hosted read-only demo: `scruple serve --demo` (or SCRUPLE_DEMO=1).

- `scruple.demo.guard`: the visitor principal, the read-only rules and the banner text.
- `scruple.demo.host`: seeds a synthetic tenant, serves the API and console over it
  without login, refuses every write, and re-seeds itself once a day.
"""

BANNER = "Demo. Synthetic data. Read only."

__all__ = ["BANNER"]
