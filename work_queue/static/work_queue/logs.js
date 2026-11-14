(function () {
  var panel = document.querySelector(".runner-logs");
  if (!panel || panel.dataset.running !== "true") { return; }
  var output = document.getElementById("runner-log-output");
  var status = document.getElementById("log-refresh-status");
  var cursor = Number(panel.dataset.cursor);
  async function refresh() {
    var url = new URL(panel.dataset.feed, window.location.origin);
    if (panel.dataset.run) { url.searchParams.set("run", panel.dataset.run); }
    url.searchParams.set("after", String(cursor));
    try {
      var response = await fetch(url, {cache: "no-store", credentials: "same-origin"});
      if (!response.ok) { throw new Error("Log request failed"); }
      var data = await response.json();
      var follow = output.scrollHeight - output.scrollTop - output.clientHeight < 50;
      data.events.forEach(function (event) {
        output.appendChild(document.createTextNode(event.text));
        cursor = event.sequence;
        document.getElementById("log-empty").hidden = true;
      });
      if (follow) { output.scrollTop = output.scrollHeight; }
      document.getElementById("log-item-status").textContent = data.status;
      if (!data.running) {
        status.textContent = "Execution is no longer running. Saved output is shown.";
        return;
      }
      status.textContent = "Live updates every 3 seconds.";
    } catch (error) {
      status.textContent = "Unable to refresh logs. Retrying shortly…";
    }
    window.setTimeout(refresh, 3000);
  }
  window.setTimeout(refresh, 3000);
})();
