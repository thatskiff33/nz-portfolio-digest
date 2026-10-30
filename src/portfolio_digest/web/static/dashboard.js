/* All financial values arrive as decimal strings. Conversion here is for chart pixels only. */
htmx.config.allowEval = false;
htmx.config.allowScriptTags = false;
htmx.config.includeIndicatorStyles = false;
htmx.config.historyCacheSize = 0;
htmx.config.selfRequestsOnly = true;
function drawCharts() {
  document.querySelectorAll(".portfolio-chart").forEach((canvas) => {
    if (Chart.getChart(canvas)) return;
    const rows = [
      ...canvas.closest(".panel").querySelectorAll("[data-chart-date]"),
    ];
    const dark = matchMedia("(prefers-color-scheme: dark)").matches;
    new Chart(canvas, {
      type: "line",
      data: {
        labels: rows.map((row) => row.dataset.chartDate),
        datasets: [
          {
            data: rows.map((row) =>
              row.dataset.chartValue === ""
                ? null
                : Number(row.dataset.chartValue),
            ),
            borderColor: dark ? "#74d7ba" : "#18725d",
            backgroundColor: dark ? "#74d7ba16" : "#18725d0c",
            fill: true,
            tension: 0.15,
            pointRadius: rows.length < 3 ? 3 : 0,
            borderWidth: 2,
            spanGaps: false,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: !matchMedia("(prefers-reduced-motion: reduce)").matches,
        plugins: { legend: { display: false } },
        scales: {
          x: {
            grid: { display: false },
            ticks: { maxTicksLimit: 6, color: dark ? "#b5c5c0" : "#52645e" },
          },
          y: {
            ticks: { color: dark ? "#b5c5c0" : "#52645e" },
            grid: { color: dark ? "#ffffff12" : "#15382d0c" },
          },
        },
      },
    });
  });
}
document.addEventListener("DOMContentLoaded", drawCharts);
document.addEventListener("htmx:load", drawCharts);
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
  document
    .querySelectorAll(".portfolio-chart")
    .forEach((canvas) => Chart.getChart(canvas)?.destroy());
  drawCharts();
});
document.addEventListener("htmx:beforeSwap", (event) => {
  // HTMX leaves error responses unswapped by default. Show our accessible error page
  // so a refused write or expired session gives visible feedback.
  const response = event.detail.xhr;
  if (
    response.status >= 400 &&
    response.status < 500 &&
    response.getResponseHeader("Content-Type")?.startsWith("text/html")
  ) {
    event.detail.shouldSwap = true;
    event.detail.isError = false;
  }
  if (!event.detail.shouldSwap) return;
  document
    .querySelectorAll(".portfolio-chart")
    .forEach((canvas) => Chart.getChart(canvas)?.destroy());
});
