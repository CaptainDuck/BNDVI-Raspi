(() => {
  const metaForm = document.getElementById("meta-form");
  if (metaForm) {
    metaForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const id = metaForm.dataset.id;
      const data = new FormData(metaForm);
      const payload = {
        label: data.get("label") || null,
        notes: data.get("notes") || null,
      };
      const btn = metaForm.querySelector("button");
      btn.disabled = true;
      btn.textContent = "Saving…";
      try {
        const res = await fetch("/api/captures/" + id, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        if (!res.ok) throw new Error("HTTP " + res.status);
        btn.textContent = "Saved";
        setTimeout(() => { btn.textContent = "Save"; btn.disabled = false; }, 900);
      } catch (err) {
        btn.textContent = "Save failed";
        btn.disabled = false;
      }
    });
  }

  const delBtn = document.getElementById("delete-btn");
  if (delBtn) {
    delBtn.addEventListener("click", async () => {
      if (!confirm("Delete this capture? This removes the image files too.")) return;
      const id = delBtn.dataset.id;
      const res = await fetch("/api/captures/" + id, { method: "DELETE" });
      if (res.ok) {
        window.location.href = "/";
      } else {
        alert("Delete failed (HTTP " + res.status + ")");
      }
    });
  }
})();
