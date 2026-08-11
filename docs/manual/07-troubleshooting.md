# 7 · Troubleshooting

[← Operating](06-operating.md) · [Manual index](README.md)

The symptom-indexed [Troubleshooting Guide](../guides/TROUBLESHOOTING.md) is the
single owner for server, database, Redis, client, dashboard, transport, and
recovery diagnostics. Install-specific failures live in the
[playbook](../install/PLAYBOOK.md#troubleshooting).

For questions about interpreting EISV, warmup, calibration, gaming limits, or
how much evidence the current signal supports, use
[Reading the signals](05-reading-the-signals.md) and the
[Reviewer Guide](../REVIEWER_GUIDE.md). This chapter remains as a stable manual
route without restating those claims.

Do not run `DROP`, `TRUNCATE`, or `DELETE` against the governance database
without a backup and deliberate operator intent. Report security issues through
[`SECURITY.md`](../../SECURITY.md).
