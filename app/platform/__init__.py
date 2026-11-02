"""Shared foundation: the part of Power Apps every internal tool reuses.

auth      who you are (OIDC in prod, dev login here)
rbac      what you may do
audit     append-only record of what happened
approvals maker/checker state machine with thresholds
events    outbox + in-process dispatch so apps stay decoupled
notify    Teams/Slack adapter (fake here)
"""
