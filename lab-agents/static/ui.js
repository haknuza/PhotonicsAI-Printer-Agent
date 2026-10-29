"use strict";

const refresh = document.querySelector("#refresh");
const notice = document.querySelector("#notice");
const data = document.querySelector("#printer-data");

async function refreshStatus() {
  refresh.disabled = true;
  notice.textContent = "Reading simulated printer…";
  try {
    const results = await Promise.all(["status", "position", "capabilities"].map(async command => {
      const response = await fetch(`/api/printer/${command}`, {cache: "no-store"});
      const reply = await response.json();
      if (!response.ok || !reply.ok || reply.simulated !== true) {
        throw new Error((reply.error && reply.error.message) || "Mock status unavailable");
      }
      return {command, reply};
    }));
    data.replaceChildren();
    for (const {command, reply} of results) {
      const details = document.createElement("details");
      details.open = command === "position";
      const summary = document.createElement("summary");
      summary.textContent = `Mock ${command}`;
      const output = document.createElement("pre");
      output.textContent = JSON.stringify(reply.result.data, null, 2);
      details.append(summary, output);
      data.append(details);
    }
    notice.textContent = "Simulated values refreshed. No hardware was contacted.";
  } catch (error) {
    data.replaceChildren();
    notice.textContent = `Status unavailable: ${error.message}`;
  } finally {
    refresh.disabled = false;
  }
}

refresh.addEventListener("click", refreshStatus);
refreshStatus();
