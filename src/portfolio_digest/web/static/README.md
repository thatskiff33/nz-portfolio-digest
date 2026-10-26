# Browser dependencies

Vendored unmodified releases, served locally so financial pages make no external requests:

- HTMX 2.0.4: https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js (0BSD).
- Chart.js 4.4.8: https://cdn.jsdelivr.net/npm/chart.js@4.4.8/dist/chart.umd.js (MIT).

Licences are included alongside them. Update filenames and template references together.
Chart.js converts decimal strings to browser numbers only to draw pixels. Tables and all
financial calculations use server-side Decimal values. HTMX history caching and script
evaluation are disabled; chart tables provide accessible data without JavaScript.
