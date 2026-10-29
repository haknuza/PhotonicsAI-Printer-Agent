import { ModelViewer } from './model-viewer.js';

const el = id => document.getElementById(id);
const editor = el('model-source');
const state = el('model-state');
let current = null;
let dirty = true;
let busy = false;
let available = false;
let previewReady = false;
let viewer = null;
let viewerError = null;
let loadController = new AbortController();
let selection = 0;
let generationAvailable = false;
let pendingClarification = null;

try { viewer = new ModelViewer(el('model-viewer')); }
catch (error) { viewerError = error.message; }

async function api(path, options = {}) {
  const response = await fetch(path, {cache: 'no-store', ...options});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error?.message || `Request failed (${response.status})`);
  return result;
}
function post(path, data) {
  return api(path, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Lab-Control': 'local'}, body: JSON.stringify(data)});
}
function controls() {
  window.dispatchEvent(new CustomEvent('model-selection', {detail: {
    job: current, usable: !busy && !dirty && previewReady && Boolean(current?.approval_current)
  }}));
  editor.disabled = busy;
  for (const id of ['load-example', 'model-file', 'new-model', 'model-revisions', 'model-description', 'generation-history']) el(id).disabled = busy;
  el('compile-model').disabled = busy || !available || !editor.value.trim();
  el('generate-model').disabled = busy || !available || !generationAvailable || !el('model-description').value.trim();
  el('generate-model').textContent = pendingClarification ? 'Send answer & generate' : 'Generate model';
  el('fit-model').disabled = !previewReady;
  el('approve-model').disabled = busy || dirty || !previewReady || !current?.is_latest || current?.status !== 'ready' || !el('model-confirm').checked || Boolean(current?.approval);
  el('model-confirm').disabled = busy || dirty || !previewReady || !current?.is_latest || current?.status !== 'ready' || Boolean(current?.approval);
}
function clearPreview() {
  loadController.abort();
  loadController = new AbortController();
  previewReady = false;
  if (viewer) viewer.clear();
  el('model-artifacts').hidden = true;
  el('model-dimensions').textContent = 'Dimensions: no compiled model';
  el('preview-state').textContent = 'No compiled preview is selected.';
  el('model-confirm').checked = false;
}
function changed() {
  resetClarification();
  dirty = true;
  clearPreview();
  state.textContent = 'Source changed. Compile a new revision; approval will be required again.';
  el('model-approval').textContent = 'This source has no model approval.';
  controls();
}
async function revisions(selectedId) {
  const result = await api('/api/models');
  const select = el('model-revisions');
  select.replaceChildren(new Option('New model', ''));
  for (const job of result.jobs) {
    select.add(new Option(`${job.model_id.slice(0, 8)} · v${job.revision} · ${job.status}`, job.job_id));
  }
  select.value = selectedId || '';
  return result.jobs;
}
async function show(job, ticket) {
  if (ticket !== selection) return;
  current = job;
  dirty = false;
  clearPreview();
  const label = `${job.model_id.slice(0, 8)} · revision ${job.revision} · ${job.job_id}`;
  el('model-version').textContent = label;
  const provenance = job.provenance;
  el('model-origin').textContent = provenance?.origin === 'ai-generated' ?
    `AI-generated with ${provenance.provider} / ${provenance.model}: ${provenance.title}. ${provenance.explanation}` :
    'Supplied or manually edited OpenSCAD source; not an AI generation claim.';
  el('model-diagnostics').textContent = [job.stdout, job.stderr].filter(Boolean).join('\n') || 'No compiler diagnostics.';
  el('model-approval').textContent = job.approval_current ? 'This exact model version is approved. No printer action is authorized.' : job.approval ? 'Historical approval for this older version. Newer revisions need their own approval.' : 'This model version is not approved.';
  el('source-download').href = job.artifacts.source;
  el('source-download').hidden = false;
  el('stl-download').hidden = true;
  el('model-artifacts').hidden = false;
  if (job.status === 'error') {
    state.textContent = `Compilation error: ${job.error.code} — ${job.error.message}`;
    el('preview-state').textContent = 'No STL preview is available for this failed revision.';
  } else if (job.status === 'ready') {
    state.textContent = 'Compilation succeeded. Review this model version before approval.';
    el('stl-download').href = job.artifacts.stl;
    el('stl-download').hidden = false;
    const d = job.dimensions_mm;
    el('model-dimensions').textContent = `Dimensions (X × Y × Z): ${d.x.toFixed(3)} × ${d.y.toFixed(3)} × ${d.z.toFixed(3)} mm`;
    el('preview-state').textContent = 'Loading STL preview…';
    try {
      if (!viewer) throw new Error(viewerError || 'WebGL is unavailable');
      const size = await viewer.load(job.artifacts.stl, loadController.signal);
      if (ticket !== selection || !size) return;
      previewReady = true;
      el('preview-state').textContent = `STL preview ready · ${job.triangles} triangles. Drag to rotate; scroll or pinch to zoom.`;
    } catch (error) {
      if (error.name === 'AbortError') return;
      el('preview-state').textContent = `Preview unavailable: ${error.message}. Source/STL downloads remain available.`;
    }
  } else {
    state.textContent = job.status === 'queued' ? 'Queued for compilation…' : 'Compiling with OpenSCAD…';
    el('preview-state').textContent = 'Waiting for successful STL compilation.';
  }
  controls();
}
async function poll(job, ticket) {
  while (ticket === selection) {
    if (!['queued', 'compiling'].includes(job.status)) return job;
    state.textContent = job.status === 'queued' ? 'Queued for compilation…' : 'Compiling with OpenSCAD…';
    await new Promise(resolve => setTimeout(resolve, 350));
    job = await api(`/api/models/${job.job_id}`);
  }
  return null;
}

el('compile-model').addEventListener('click', async () => {
  resetClarification();
  const ticket = ++selection;
  busy = true;
  clearPreview();
  controls();
  state.textContent = 'Submitting a new model revision…';
  el('model-approval').textContent = 'New revisions require new model approval.';
  try {
    const request = {source: editor.value, filename: el('model-filename').textContent || 'model.scad'};
    if (current) request.parent_job_id = current.job_id;
    const accepted = await post('/api/models', request);
    current = accepted;
    const job = await poll(accepted, ticket);
    if (job && ticket === selection) { await show(job, ticket); await revisions(job.job_id); }
  } catch (error) {
    dirty = true;
    state.textContent = `Compilation request failed: ${error.message}`;
  } finally { busy = false; controls(); }
});

el('model-revisions').addEventListener('change', async event => {
  resetClarification();
  const id = event.target.value;
  const ticket = ++selection;
  clearPreview();
  if (!id) {
    current = null;
    editor.value = '';
    el('model-filename').textContent = 'model.scad';
    changed();
    return;
  }
  busy = true; controls();
  try {
    const job = await api(`/api/models/${id}`);
    const source = await fetch(job.artifacts.source, {cache: 'no-store'});
    if (!source.ok) throw new Error('Could not retrieve this source version');
    editor.value = await source.text();
    el('model-filename').textContent = job.filename;
    await show(await poll(job, ticket), ticket);
  } catch (error) { state.textContent = error.message; }
  finally { busy = false; controls(); }
});

el('load-example').addEventListener('click', async () => {
  try {
    const example = await api('/api/models/example');
    editor.value = example.source;
    el('model-filename').textContent = example.filename;
    changed();
    state.textContent = 'Hand-written parametric spacer example loaded (not AI-generated). Compile it to preview.';
  } catch (error) { state.textContent = error.message; }
});
el('model-file').addEventListener('change', async event => {
  const file = event.target.files[0];
  if (!file) return;
  if (!/\.scad$/i.test(file.name) || file.size > 128*1024) {
    state.textContent = 'Select a .scad file no larger than 128 KiB.'; return;
  }
  editor.value = await file.text();
  el('model-filename').textContent = file.name;
  changed();
});
editor.addEventListener('input', changed);
el('model-confirm').addEventListener('change', controls);
el('fit-model').addEventListener('click', () => { if (viewer) viewer.fit(); });
el('new-model').addEventListener('click', () => {
  resetClarification();
  el('model-description').value = '';
  el('generation-history').value = '';
  ++selection; current = null; editor.value = ''; el('model-revisions').value = '';
  el('model-filename').textContent = 'model.scad'; el('model-version').textContent = 'New model'; changed();
});
el('approve-model').addEventListener('click', async () => {
  busy = true; controls();
  try {
    await show(await post(`/api/models/${current.job_id}/approve`, {
      source_sha256: current.source_sha256, stl_sha256: current.stl_sha256, confirmed: true
    }), selection);
  } catch (error) { el('model-approval').textContent = `Approval failed: ${error.message}`; }
  finally { busy = false; controls(); }
});

function resetClarification() {
  pendingClarification = null;
  el('design-questions').replaceChildren();
}
function designText(request) {
  const container = el('design-questions');
  container.replaceChildren();
  if (!request.proposal) return;
  const explanation = document.createElement('p');
  explanation.textContent = request.proposal.explanation;
  container.append(explanation);
  for (const [heading, items] of [['Design questions', request.proposal.questions], ['Assumptions to review', request.proposal.assumptions]]) {
    if (!items?.length) continue;
    const title = document.createElement('strong'); title.textContent = heading;
    const list = document.createElement('ul');
    for (const item of items) { const line = document.createElement('li'); line.textContent = item; list.append(line); }
    container.append(title, list);
  }
}
async function generationHistory(selectedId) {
  const response = await api('/api/models/generations');
  const select = el('generation-history');
  select.replaceChildren(new Option('Select a saved request', ''));
  for (const request of response.requests) {
    select.add(new Option(`${request.description.slice(0, 65)} · ${request.status}`, request.request_id));
  }
  select.value = selectedId || '';
}
async function showGeneration(request, ticket) {
  designText(request);
  if (request.job) {
    const response = await fetch(request.job.artifacts.source, {cache:'no-store'});
    if (!response.ok) throw new Error('Generated source could not be retrieved');
    const source = await response.text();
    if (ticket !== selection) return;
    editor.value = source;
    el('model-filename').textContent = request.job.filename;
    await show(request.job, ticket);
    await revisions(request.job.job_id);
  } else if (request.generated_source) {
    editor.value = request.generated_source;
    dirty = true;
    el('model-filename').textContent = 'generated_model.scad';
    el('model-origin').textContent = `AI-generated draft from ${request.provider} / ${request.model}; source preserved, compilation did not start.`;
  } else {
    el('model-origin').textContent = 'No new model source was accepted or compiled for this design request.';
  }
  if (request.status === 'needs_clarification') {
    pendingClarification = request.continued_by ? null : request.request_id;
    el('generation-state').textContent = request.continued_by ?
      'This design request already has a saved follow-up. Select that newer request to continue.' :
      'Please answer the design questions, then send your answer. No model was generated or compiled.';
    el('model-description').value = '';
  } else if (request.status === 'unsupported') {
    el('generation-state').textContent = `The AI could not design this object: ${request.proposal.explanation}`;
  } else if (request.status === 'error') {
    el('generation-state').textContent = `Generation/compilation failed: ${request.error.code} — ${request.error.message}`;
  } else {
    el('generation-state').textContent = 'AI source generated and compiled. Review the assumptions, source and preview before model approval.';
  }
}
async function finishGeneration(request, ticket) {
  while (['queued', 'generating', 'compiling'].includes(request.status) && ticket === selection) {
    el('generation-state').textContent = request.status === 'compiling' ?
      'OpenSCAD source generated. Compiling the model locally…' :
      request.status === 'queued' ? 'AI generation queued…' : 'Generating a parametric OpenSCAD model…';
    await new Promise(resolve => setTimeout(resolve, 400));
    request = await api(`/api/models/generations/${request.request_id}`);
  }
  if (ticket !== selection) return;
  await showGeneration(request, ticket);
  await generationHistory(request.request_id);
}
el('model-description').addEventListener('input', controls);
el('generate-model').addEventListener('click', async () => {
  const ticket = ++selection;
  const data = {description:el('model-description').value};
  if (pendingClarification) data.previous_request_id = pendingClarification;
  else {
    if (current) data.parent_job_id = current.job_id;
    data.base_source = editor.value;
  }
  pendingClarification = null;
  busy = true; dirty = true; clearPreview(); controls();
  el('model-origin').textContent = 'Requesting a new AI model version; no approval is carried forward.';
  el('model-approval').textContent = 'AI-generated revisions require their own model approval.';
  el('generation-state').textContent = 'Submitting your design request…';
  try { await finishGeneration(await post('/api/models/generate', data), ticket); }
  catch (error) { el('generation-state').textContent = `Generation request failed: ${error.message}`; }
  finally { busy = false; controls(); }
});
el('generation-history').addEventListener('change', async event => {
  if (!event.target.value) return;
  const ticket = ++selection;
  resetClarification(); busy = true; dirty = true; clearPreview(); controls();
  try {
    const request = await api(`/api/models/generations/${event.target.value}`);
    await finishGeneration(request, ticket);
  } catch (error) { el('generation-state').textContent = error.message; }
  finally { busy = false; controls(); }
});

async function start() {
  try {
    const config = await api('/api/models/config');
    available = config.available;
    el('compiler-config').textContent = available ? `Compiler: ${config.executable} · Linux Bubblewrap isolation · external assets disabled` : config.error.message;
    generationAvailable = Boolean(config.generation?.available);
    const backend = config.backend_pid ? ` · Backend PID: ${config.backend_pid}` : '';
    el('generation-config').textContent = generationAvailable ?
      `AI provider: ${config.generation.provider} / ${config.generation.model}${backend}` :
      `AI generation unavailable: ${config.generation?.error?.message || 'Configure the backend provider.'}${backend}`;
    await revisions();
    if (generationAvailable) await generationHistory();
  } catch (error) { el('compiler-config').textContent = error.message; }
  controls();
}
start();
