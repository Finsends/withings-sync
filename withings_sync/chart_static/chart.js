"use strict";

(() => {
  const form = document.getElementById("filters");
  const startInput = document.getElementById("start-date");
  const endInput = document.getElementById("end-date");
  const metricInput = document.getElementById("metric");
  const title = document.getElementById("chart-title");
  const description = document.getElementById("metric-description");
  const status = document.getElementById("chart-status");
  const panel = document.getElementById("chart-panel");
  const container = document.getElementById("chart-container");
  const canvas = document.getElementById("metric-chart");
  const dateFormatter = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Europe/Amsterdam", day: "numeric", month: "short", year: "numeric"
  });
  const timestampFormatter = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Europe/Amsterdam", day: "numeric", month: "short", year: "numeric",
    hour: "2-digit", minute: "2-digit", timeZoneName: "short"
  });
  const valueFormatter = new Intl.NumberFormat("en-GB", { maximumFractionDigits: 3 });
  let metrics = [];
  let graph = null;
  let pending = null;
  let requestNumber = 0;

  function setStatus(message, error = false) {
    status.textContent = message;
    status.dataset.error = String(error);
    status.hidden = !message;
  }

  function clearGraph() {
    if (graph) graph.destroy();
    graph = null;
    container.hidden = true;
  }

  function metricTitle(metric) {
    return metric.unit ? `${metric.label} (${metric.unit})` : metric.label;
  }

  function showMetric(metric) {
    title.textContent = metricTitle(metric);
    document.title = `${metricTitle(metric)} · Withings`;
    description.textContent = metric.description || "";
    description.hidden = !metric.description;
    canvas.setAttribute("aria-label", `${metricTitle(metric)} by date`);
  }

  async function getJSON(url, signal) {
    const response = await fetch(url, { signal, cache: "no-store", credentials: "same-origin" });
    if (!response.ok) {
      let message = "Could not load measurements. Please try again.";
      try {
        const body = await response.json();
        if (typeof body.error === "string") message = body.error;
      } catch (_) { /* Non-JSON responses use the safe default. */ }
      throw new Error(message);
    }
    return response.json();
  }

  async function updateGraph() {
    const current = ++requestNumber;
    if (pending) pending.abort();
    pending = new AbortController();
    clearGraph();
    const metric = metrics.find((item) => String(item.id) === metricInput.value);
    if (!metric) return;
    showMetric(metric);
    panel.setAttribute("aria-busy", "false");
    if (!startInput.value || !endInput.value) {
      setStatus("Choose a start and end date.");
      return;
    }
    if (startInput.value > endInput.value) {
      setStatus("Start date must be on or before end date.", true);
      return;
    }
    panel.setAttribute("aria-busy", "true");
    setStatus("Loading measurements…");
    const query = new URLSearchParams({
      metric: metricInput.value, start: startInput.value, end: endInput.value
    });
    try {
      const data = await getJSON(`api/series?${query}`, pending.signal);
      if (current !== requestNumber) return;
      // Use the selected metadata for both title and chart, so an old response
      // cannot put values under a newly selected metric's heading.
      if (data.metric.id !== metric.id) throw new Error("The returned metric did not match your selection.");
      if (!data.points.length) {
        setStatus("No measurements for this metric in the selected dates.");
        return;
      }
      container.hidden = false;
      graph = new Chart(canvas, {
        type: "line",
        data: {
          datasets: [{
            label: metric.label,
            data: data.points.map((point) => ({ x: point.timestamp * 1000, y: point.value })),
            borderColor: "#2167d5",
            backgroundColor: "#2167d5",
            borderWidth: 2,
            pointRadius: data.points.length === 1 ? 5 : 2,
            pointHoverRadius: 5,
            tension: 0,
            fill: false
          }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          animation: false,
          parsing: false,
          interaction: { mode: "nearest", intersect: false },
          plugins: {
            legend: { display: false },
            tooltip: {
              callbacks: {
                title: (items) => timestampFormatter.format(items[0].parsed.x),
                label: (item) => `${metric.label}: ${valueFormatter.format(item.parsed.y)}${metric.unit ? ` ${metric.unit}` : ""}`
              }
            }
          },
          scales: {
            x: {
              type: "linear",
              min: data.start_timestamp * 1000,
              max: data.end_timestamp * 1000 - 1,
              title: { display: true, text: "Date" },
              grid: { color: "#edf2f8" },
              ticks: { maxTicksLimit: 6, maxRotation: 0, callback: (value) => dateFormatter.format(value) }
            },
            y: {
              title: { display: true, text: metricTitle(metric) },
              grid: { color: "#edf2f8" }
            }
          }
        }
      });
      setStatus("");
    } catch (error) {
      if (current !== requestNumber || error.name === "AbortError") return;
      clearGraph();
      setStatus(error.message || "Could not load measurements. Please try again.", true);
    } finally {
      if (current === requestNumber) panel.setAttribute("aria-busy", "false");
    }
  }

  form.addEventListener("submit", (event) => { event.preventDefault(); updateGraph(); });
  form.addEventListener("change", updateGraph);

  async function initialize() {
    try {
      if (typeof Chart === "undefined") throw new Error("The graph could not load. Please reload this page.");
      const data = await getJSON("api/metrics");
      metrics = data.metrics;
      if (!metrics.length) throw new Error("No metrics are available yet.");
      for (const metric of metrics) {
        const option = document.createElement("option");
        option.value = String(metric.id);
        option.textContent = metric.label;
        metricInput.append(option);
      }
      metricInput.value = String((metrics.find((metric) => metric.id === 1) || metrics[0]).id);
      startInput.value = data.start || "";
      endInput.value = data.end || "";
      for (const input of [startInput, endInput, metricInput]) input.disabled = false;
      await updateGraph();
    } catch (error) {
      title.textContent = "Withings data";
      panel.setAttribute("aria-busy", "false");
      setStatus(error.message || "Could not load available metrics. Please reload this page.", true);
    }
  }

  initialize();
})();
