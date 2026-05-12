(() => {
  const form = document.getElementById("capture-form");
  const btn = document.getElementById("capture-btn");
  const status = document.getElementById("capture-status");

  function setStatus(msg, kind) {
    status.textContent = msg;
    status.className = "status " + kind;
    status.classList.remove("hidden");
  }

  if (form) {
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      btn.disabled = true;
      btn.textContent = "Capturing…";
      setStatus("Triggering camera — this can take 5–15 s on a Pi 4.", "info");

      const correctNir = document.getElementById("correct-nir");
      const kField = document.getElementById("nir-k");
      const payload = {
        label: document.getElementById("label").value || null,
        notes: document.getElementById("notes").value || null,
        correct_nir_leakage: correctNir ? correctNir.checked : false,
        nir_leak_coef: kField ? parseFloat(kField.value) : 0.6,
      };

      try {
        const res = await fetch("/api/captures", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.error || ("HTTP " + res.status));
        }
        setStatus("Capture complete — reloading…", "ok");
        setTimeout(() => window.location.reload(), 600);
      } catch (err) {
        setStatus("Capture failed: " + err.message, "error");
        btn.disabled = false;
        btn.textContent = "Capture now";
      }
    });
  }

  // Time-series chart
  const points = window.__chartPoints || [];
  if (points.length > 0 && window.Chart) {
    const ctx = document.getElementById("trend-chart").getContext("2d");
    new window.Chart(ctx, {
      type: "line",
      data: {
        labels: points.map((p) => p.timestamp.replace("T", " ")),
        datasets: [{
          label: "Mean BNDVI",
          data: points.map((p) => p.mean),
          borderColor: "#6ea8fe",
          backgroundColor: "rgba(110, 168, 254, 0.15)",
          tension: 0.25,
          fill: true,
          pointRadius: 4,
          pointHoverRadius: 6,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { labels: { color: "#eef1f8" } } },
        scales: {
          x: { ticks: { color: "#8a93ad" }, grid: { color: "#2a3050" } },
          y: {
            min: -1, max: 1,
            ticks: { color: "#8a93ad" },
            grid: { color: "#2a3050" },
          },
        },
      },
    });
  }
})();
