# Price and movement alerts

After each scheduled price fetch, alerts check for a holding moving more than 10% in
one trading day, portfolio value falling more than 5% over 7 calendar days, or a price
missing for more than 3 completed trading days. Rules and per-holding thresholds live
in `schedules.toml`. Email uses the existing `MAILER` and report recipients, with an
optional ntfy or Pushover notification configured through environment variables.

```bash
portfolio-digest alerts check --dry-run  # print due alerts without sending or recording
portfolio-digest alerts check            # send due alerts now
```

Each rule sends once until it clears, unless you set a repeat cool-down. See
[alert configuration and delivery notes](DEPLOY.md#price-and-movement-alerts).
