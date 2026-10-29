const el = id => document.getElementById(id);
let selectedModel = null;
let modelUsable = false;
let configured = false;
let busy = false;
let current = null;
let selection = 0;

async function api(path, data) {
  const options = data === undefined ? {} : {method: 'POST',
    headers: {'Content-Type': 'application/json', 'X-Lab-Control': 'local'}, body: JSON.stringify(data)};
  const response = await fetch(path, {cache: 'no-store', ...options});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error?.message || `Request failed (${response.status})`);
  return result;
}
function controls() {
  const matches = modelUsable && current?.model_job_id === selectedModel?.job_id
    && current?.profile.id === el('print-profile').value;
  const reviewable = configured && !busy && matches && ['ready', 'approved'].includes(current?.status);
  el('prepare-print').disabled = busy || !configured || !modelUsable || !el('print-profile').value;
  el('print-profile').disabled = busy;
  el('print-history').disabled = busy;
  el('print-review-confirm').disabled = !reviewable;
  el('print-setup-confirm').disabled = !reviewable;
  el('start-print').disabled = !reviewable || !el('print-review-confirm').checked || !el('print-setup-confirm').checked;
  el('refresh-print').disabled = busy || !configured || !current;
  el('read-printer').disabled = busy || !configured;
}
function clearApproval() {
  el('print-review-confirm').checked = false;
  el('print-setup-confirm').checked = false;
}
window.addEventListener('model-selection', event => {
  if (selectedModel?.job_id !== event.detail.job?.job_id || !event.detail.usable) clearApproval();
  selectedModel = event.detail.job;
  modelUsable = event.detail.usable;
  controls();
});
function show(job) {
  current = job;
  clearApproval();
  el('print-state').textContent = `${job.simulated ? 'SIMULATED TEST · ' : ''}${job.status}${job.error ? ' · ' + job.error.message : ''}`;
  const profile = job.profile;
  const summary = job.summary;
  el('print-summary').textContent = [
    `Printer: ${job.printer_identity}`,
    `Slicer: ${job.slicer_engine === 'creality-print' ? 'Creality Print' : job.slicer_engine}`,
    `Model revision ${job.model_revision} · ${job.model_job_id}`,
    `Profile: ${profile.name}`,
    `Nozzle ${profile.nozzle_c}°C · Bed ${profile.bed_c}°C`,
    `${profile.layer_height_mm} mm layers · ${profile.infill_percent}% infill · supports disabled`,
    'Model centered on the bed, original orientation and scale retained.',
    ...(profile.end_behavior ? [profile.end_behavior] : []),
    profile.qualification,
    ...(summary ? [`Estimated time: ${summary.estimated_time}`,
      `Filament: ${summary.filament_mm === null ? 'unavailable' : summary.filament_mm.toFixed(0) + ' mm'}`,
      `G-code SHA-256: ${job.gcode_sha256}`] : []),
    ...(job.printer ? [`Printer state: ${job.printer.print_stats.state}`,
      `Nozzle ${job.printer.extruder.temperature}°C · Bed ${job.printer.heater_bed.temperature}°C`,
      `File progress: ${(100 * (job.printer.virtual_sdcard.progress || 0)).toFixed(1)}%`] : [])
  ].join('\n');
  el('gcode-download').hidden = !job.artifact;
  if (job.artifact) el('gcode-download').href = job.artifact;
  const svg = el('print-toolpath');
  svg.replaceChildren();
  svg.toggleAttribute('hidden', !summary);
  el('print-path-note').hidden = !summary;
  if (summary) {
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', summary.preview_segments.map(([x1,y1,x2,y2]) => `M${x1},${220-y1}L${x2},${220-y2}`).join(' '));
    path.setAttribute('stroke', '#22643b'); path.setAttribute('stroke-width', '.35'); path.setAttribute('fill', 'none');
    svg.append(path);
    el('print-path-note').textContent = 'Top view of extrusion paths across all layers. '
      + (summary.preview_truncated ? 'Long toolpath preview is truncated. ' : '')
      + 'This is not a layer-by-layer or collision simulation.';
  }
  controls();
}
async function history(id) {
  const result = await api('/api/prints');
  const select = el('print-history');
  select.replaceChildren(new Option('Select a prepared job', ''));
  for (const job of result.jobs) select.add(new Option(`v${job.model_revision} · ${job.profile.material} · ${job.status} · ${job.job_id.slice(0,8)}`, job.job_id));
  select.value = id || '';
}
el('print-profile').addEventListener('change', () => { clearApproval(); controls(); });
for (const id of ['print-review-confirm', 'print-setup-confirm']) el(id).addEventListener('change', controls);
el('prepare-print').addEventListener('click', async () => {
  busy = true; clearApproval(); controls();
  const ticket = ++selection;
  el('print-state').textContent = 'Checking printer readiness and slicing the approved model…';
  try {
    let job = await api('/api/prints', {model_job_id: selectedModel.job_id, profile_id: el('print-profile').value});
    while (['queued', 'slicing'].includes(job.status) && ticket === selection) {
      await new Promise(resolve => setTimeout(resolve, 700));
      job = await api(`/api/prints/${job.job_id}`);
    }
    if (ticket === selection) { show(job); await history(job.job_id); }
  } catch (error) { el('print-state').textContent = `Preparation failed: ${error.message}`; }
  finally { busy = false; controls(); }
});
el('print-history').addEventListener('change', async event => {
  const ticket = ++selection;
  clearApproval();
  if (!event.target.value) { current = null; controls(); return; }
  busy = true; controls();
  try {
    const job = await api(`/api/prints/${event.target.value}`);
    if (ticket === selection) { el('print-profile').value = job.profile.id; show(job); }
  } catch (error) { el('print-state').textContent = error.message; }
  finally { busy = false; controls(); }
});
el('start-print').addEventListener('click', async () => {
  busy = true; controls();
  el('print-state').textContent = 'Validating final approval and submitting this exact job…';
  try {
    const approved = await api(`/api/prints/${current.job_id}/approve`, {
      gcode_sha256: current.gcode_sha256,
      confirmed: el('print-review-confirm').checked, setup_confirmed: el('print-setup-confirm').checked
    });
    const started = await api(`/api/prints/${current.job_id}/start`, {token: approved.token});
    show(started);
    el('print-state').textContent = 'Print start accepted. Check the printer and read progress; acceptance is not completion.';
    await history(started.job_id);
  } catch (error) {
    if (current) {
      try { show(await api(`/api/prints/${current.job_id}`)); } catch (_) { /* Preserve the original start error. */ }
    }
    el('print-state').textContent = `Start failed or uncertain: ${error.message}. Check the printer before another action.`;
  } finally { clearApproval(); busy = false; controls(); }
});
el('refresh-print').addEventListener('click', async () => {
  busy = true; controls();
  try { show(await api(`/api/prints/${current.job_id}/status`)); }
  catch (error) { el('print-state').textContent = `Status unavailable: ${error.message}`; }
  finally { busy = false; controls(); }
});
el('read-printer').addEventListener('click', async () => {
  busy = true; controls();
  try {
    const printer = await api('/api/prints/printer');
    el('print-readiness').textContent = `${printer.printer_identity} · ${printer.print_stats.state}`
      + ` · nozzle ${printer.extruder.temperature}°C · bed ${printer.heater_bed.temperature}°C · `
      + (printer.ready_for_print ? 'Controller readiness checks passed. Confirm physical setup before starting.' : printer.readiness_error);
  } catch (error) { el('print-readiness').textContent = `Connection unavailable: ${error.message}`; }
  finally { busy = false; controls(); }
});
try {
  const config = await api('/api/prints/config');
  configured = config.available;
  el('print-config').textContent = configured ? `${config.slicer} and SSH print-job adapter configured. Printer readiness is checked when you prepare a job.` : config.error.message;
  for (const profile of config.profiles) el('print-profile').add(new Option(profile.name, profile.id));
  await history();
} catch (error) { el('print-config').textContent = `Printing unavailable: ${error.message}`; }
controls();
