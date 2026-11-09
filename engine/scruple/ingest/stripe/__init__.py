"""Stripe disputes adapter: wire models, mapping and a read-only API client."""

from scruple.ingest.stripe.client import StripeAPIError, StripeClient
from scruple.ingest.stripe.mapping import ingest_dispute, map_dispute, parse_dispute

__all__ = ["StripeAPIError", "StripeClient", "ingest_dispute", "map_dispute", "parse_dispute"]
