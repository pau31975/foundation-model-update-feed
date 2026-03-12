/**
 * AI Model Feed – minimal client-side JS.
 *
 * Handles the "Run collectors now" button without a full page reload
 * so the user gets immediate feedback before reloading the feed.
 */

/**
 * POST /api/collect and display the result inline.
 */
async function triggerCollect() {
  const btn = document.getElementById("btn-collect");
  const statusEl = document.getElementById("collect-status");

  btn.disabled = true;
  btn.textContent = "Running…";
  statusEl.style.display = "block";
  statusEl.textContent = "Contacting providers, please wait…";
  statusEl.className = "collect-status";

  try {
    const response = await fetch("/api/collect", { method: "POST" });
    const data = await response.json();

    if (!response.ok) {
      statusEl.textContent = `Error ${response.status}: ${JSON.stringify(data)}`;
      statusEl.classList.add("collect-status--error");
    } else {
      const { added, skipped, errors } = data;
      let msg = `Done — added ${added}, skipped ${skipped} duplicate(s).`;
      if (errors && errors.length > 0) {
        msg += ` Errors: ${errors.join(" | ")}`;
      }
      statusEl.textContent = msg;
      statusEl.classList.add("collect-status--ok");

      if (added > 0) {
        // Reload after a brief pause so the user can read the result
        setTimeout(() => window.location.reload(), 1200);
      }
    }
  } catch (err) {
    statusEl.textContent = `Network error: ${err.message}`;
    statusEl.classList.add("collect-status--error");
  } finally {
    btn.disabled = false;
    btn.textContent = "Run collectors now";
  }
}
