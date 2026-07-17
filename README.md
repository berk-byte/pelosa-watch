# pelosa-watch

Watches a public beach booking availability page and sends a notification
(ntfy push + email) when open places appear for any listed date.

Runs on GitHub Actions: an hourly scheduled job keeps a polling loop alive
around the clock. Recipient addresses and the ntfy topic are stored as
repository secrets (`ALERT_EMAILS`, `NTFY_TOPIC`).
