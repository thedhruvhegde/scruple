"""Outbound integrations for gate events: webhooks, change tickets, Slack approvals.

The audit log is the outbox (`events`, `dispatch`); work goes through the job queue
(`jobs`); every outbound request goes through `safehttp` (SSRF rules); every HMAC is
in `signing`. Settings are per tenant (`config`, `store`). docs/INTEGRATIONS.md.
"""
