const $ = selector => document.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];
const canvas = $('#canvas');
const wrap = $('#canvas-wrap');
const ctx = canvas.getContext('2d');
const offscreen = () => document.createElement('canvas');
const rembgCanvas = offscreen();
const editsCanvas = offscreen();
const initialEditsCanvas = offscreen();
const maskCanvas = offscreen();
const overlayCanvas = offscreen();
const edgeCanvas = offscreen();

let items = [];
let current = null;
let state = null;
let sourceImage = null;
let rembgImage = null;
let maskForeground = new Uint8Array();
let viewMode = 'overlay';
let tool = 'add';
let editsDirty = false;
let metadataDirty = false;
let strokes = [];
let activeStroke = null;
let hoverPoint = null;
let activeLength = null;
let panning = null;
let suppressPasteUntil = 0;
let autosaveTimer = null;
let saveChain = Promise.resolve();
let hostedMode = false;
let claimHeartbeat = null;
const view = { zoom: 1, x: 0, y: 0, fit: 1 };

function setStatus(message, error = false) {
  $('#status').textContent = message;
  $('#status').classList.toggle('error', error);
}

async function responseError(response) {
  let message = `${response.status} ${response.statusText}`;
  try { message = (await response.json()).detail || message; } catch (_) {}
  const error = new Error(message);
  error.status = response.status;
  return error;
}

async function api(url, options = {}) {
  const response = await fetch(url, options);
  if (response.status === 401 && hostedMode) location.href = '/';
  if (!response.ok) throw await responseError(response);
  return response.json();
}

function loadImage(url, cacheBust = true) {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error(`Could not load ${url}`));
    image.src = cacheBust ? `${url}?v=${Date.now()}` : url;
  });
}

async function loadPublishedSvg(url) {
  const response = await fetch(`${url}${url.includes('?') ? '&' : '?'}v=${Date.now()}`);
  if (!response.ok) throw new Error('Could not load published SVG');
  const document = new DOMParser().parseFromString(await response.text(), 'image/svg+xml');
  const root = document.documentElement;
  const viewBox = root.getAttribute('viewBox')?.trim().split(/\s+/).map(Number);
  if (!viewBox || viewBox.length !== 4 || !viewBox.every(Number.isFinite)) {
    throw new Error('Published SVG has no valid viewBox');
  }
  const scale = 1200 / Math.max(viewBox[2], viewBox[3]);
  root.setAttribute('width', String(Math.max(1, Math.round(viewBox[2] * scale))));
  root.setAttribute('height', String(Math.max(1, Math.round(viewBox[3] * scale))));
  const objectUrl = URL.createObjectURL(new Blob(
    [new XMLSerializer().serializeToString(root)], { type: 'image/svg+xml' },
  ));
  try {
    return await loadImage(objectUrl, false);
  } finally {
    URL.revokeObjectURL(objectUrl);
  }
}

async function refreshItems() {
  const response = await api('/api/items');
  items = response.items;
  refreshProgressText();
  return response;
}

function visibleItems() {
  const filter = $('#filter').value;
  return items.filter(item => {
    const workflowFilter = ['never_worked', 'pending_review', 'in_catalog'].includes(filter);
    const statusMatches = filter === 'all' ||
      (workflowFilter ? item.workflow_status === filter : item.rating === filter);
    const claimMatches = filter !== 'never_worked' || !item.claimed_by;
    return statusMatches && claimMatches && matchesCatalogFilters(item);
  });
}

function prefetchPriority(visible = visibleItems(), currentId = current?.id) {
  if (!currentId) return [];
  const eligible = item =>
    !item.independent && item.workflow_status === 'never_worked' && !item.claimed_by;
  const currentItem = items.find(item => item.id === currentId);
  if (!currentItem || !eligible(currentItem)) return [];

  const priority = [currentItem];
  const candidates = visible.filter(eligible);
  const index = candidates.findIndex(item => item.id === currentId);
  if (index >= 0 && candidates.length > 1) {
    for (const offset of [1, -1, 2, -2]) {
      const candidate = candidates[
        (index + offset + candidates.length) % candidates.length
      ];
      if (!priority.some(item => item.id === candidate.id)) {
        priority.push(candidate);
      }
    }
  }
  return priority.slice(0, 5);
}

async function syncPrefetch(visible = visibleItems(), currentId = current?.id) {
  try {
    await api('/api/prefetch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        item_ids: prefetchPriority(visible, currentId).map(item => item.id),
      }),
    });
  } catch (error) {
    setStatus(`Could not update prefetch queue: ${error.message}`, true);
  }
}

function matchesCatalogFilters(item) {
  const name = $('#name-filter').value.trim().toLowerCase();
  const vendor = $('#vendor-filter').value;
  const type = $('#type-filter').value.trim().toLowerCase();
  const products = item.products?.length
    ? item.products
    : [{ n: item.id, vn: '', pt: '' }];
  return products.some(product =>
    String(product.n).toLowerCase().includes(name) &&
    matchesVendorFilter(product.vn, vendor) &&
    (!type || String(product.pt).toLowerCase() === type));
}

function matchesVendorFilter(vendor, query) {
  const value = String(vendor).toLowerCase();
  const terms = query.toLowerCase().split(/\s+/).filter(term => term && term !== '-');
  return terms.every(term => term.startsWith('-')
    ? !value.includes(term.slice(1))
    : value.includes(term));
}

function startClaimHeartbeat(itemId) {
  clearInterval(claimHeartbeat);
  claimHeartbeat = setInterval(async () => {
    if (!current || current.id !== itemId || current.read_only) return;
    try {
      await api(`/api/items/${encodeURIComponent(itemId)}/claim`, {method:'POST'});
      if ($('#status').textContent === 'Connection interrupted; claim check will retry') {
        setStatus('Claim active');
      }
    } catch (error) {
      if (!error.status || error.status >= 500) {
        setStatus('Connection interrupted; claim check will retry', true);
        return;
      }
      clearInterval(claimHeartbeat);
      current.read_only = true;
      state = null;
      setEditorDisabled(true);
      setSidebarMode('read-only');
      $('#read-only-summary').textContent = 'Editing stopped';
      $('#read-only-info').textContent = error.message;
      $('#rereview').hidden = true;
      $('#edit-metadata').hidden = true;
      setStatus(error.message, true);
      render();
    }
  }, 60_000);
}

async function releaseCurrentClaim() {
  clearInterval(claimHeartbeat);
  if (!hostedMode || !current || current.read_only) return;
  const itemId = current.id;
  metadataDirty = editsDirty = false;
  try {
    await fetch(`/api/items/${encodeURIComponent(itemId)}/release`, {method:'POST'});
  } catch (_) {}
}

function showEmptyState() {
  clearInterval(claimHeartbeat);
  const matchingCatalog = items.filter(matchesCatalogFilters);
  const complete = matchingCatalog.length > 0 &&
    $('#filter').value === 'never_worked' &&
    matchingCatalog.every(item => item.workflow_status !== 'never_worked');
  $('#empty-title').textContent = complete ? 'Queue complete' : 'No matching products';
  $('#empty-message').textContent = complete
    ? 'All products matching these catalog filters have been reviewed.'
    : 'Change or clear the queue and catalog filters to continue.';
  $('#empty-state').classList.add('visible');
  $('#filename').textContent = complete ? 'Queue complete' : 'No matching products';
  sourceImage = null;
  rembgImage = null;
  state = null;
  current = null;
  $('#published-state').classList.remove('visible');
  setSidebarMode('empty');
  setEditorDisabled(false);
  $('#reset-catalog').hidden = true;
  $('#open-product').hidden = true;
  $('#rereview').hidden = true;
  $('#edit-metadata').hidden = true;
  render();
}

function setEditorDisabled(disabled) {
  $$('.editor button, .editor input, button.editor').forEach(control => control.disabled = disabled);
}

function setSidebarMode(mode) {
  $('#edit-panel').hidden = mode !== 'edit';
  $('#read-only-panel').hidden = mode !== 'read-only';
}

function setItemTitle(item) {
  const names = item.products?.map(product => product.n).join(' / ');
  const vendors = [...new Set(item.products?.map(product => product.vn) || [])].join(' / ');
  $('#filename').textContent = names ? `${names} — ${vendors}` : item.filename;
  const product = item.independent ? null : item.products?.find(product => product.link);
  $('#open-product').hidden = !product;
  $('#open-product').dataset.url = product?.link || '';
}

async function showPublishedItem(item) {
  clearInterval(claimHeartbeat);
  current = item;
  state = null;
  sourceImage = null;
  rembgImage = null;
  editsDirty = metadataDirty = false;
  setItemTitle(item);
  setEditorDisabled(true);
  setSidebarMode('read-only');
  $('#published-state').classList.add('visible');
  const source = item.provenance === 'alternative' ? 'alternative image' :
    item.provenance === 'mixed' ? 'mixed image sources' : 'catalog image';
  const stage = item.pending_review ? 'Pending review' : 'In catalog';
  $('#published-note').textContent = `${stage} · ${item.rating?.replace('_', ' ') || 'mixed rating'} · ${source}`;
  $('#read-only-summary').textContent = `${stage} · ${item.rating?.replace('_', ' ') || 'mixed rating'} · ${source}`;
  $('#view-hint').textContent = 'Wheel zoom · drag pans';
  $('#read-only-info').textContent = item.pending_review
    ? 'This submission is waiting for moderation and cannot be edited right now.'
    : item.independent
      ? 'This independent entry is in the catalog. You can update its metadata without changing the outline.'
      : 'This entry is in the catalog. Re-review it to replace the published outline.';
  $('#reset-catalog').hidden = hostedMode || !item.has_alternative;
  $('#rereview').hidden = item.independent || item.pending_review || !item.published;
  $('#edit-metadata').hidden = !item.independent || item.pending_review;
  $$('.rating button').forEach(button =>
    button.classList.toggle('active', button.dataset.rating === item.rating));
  setStatus(`${stage} (read-only)`);
  canvas.style.cursor = 'grab';
  if (!item.svg_url) return render();
  $('#loading').textContent = 'Loading published silhouette…';
  $('#loading').classList.add('visible');
  try {
    sourceImage = await loadPublishedSvg(item.svg_url);
    fitView();
  } catch (error) {
    sourceImage = null;
    render();
    setStatus(error.message, true);
  } finally {
    $('#loading').classList.remove('visible');
  }
}

async function applyFilters() {
  if (!hostedMode && current && (metadataDirty || editsDirty)) {
    try { await save('pending'); } catch (_) { return; }
  }
  const visible = visibleItems();
  refreshProgressText();
  if (!visible.length) {
    await releaseCurrentClaim();
    await syncPrefetch([], null);
    return showEmptyState();
  }
  $('#empty-state').classList.remove('visible');
  if (!current || !visible.some(item => item.id === current.id)) {
    await loadItem(visible[0].id);
  } else {
    await syncPrefetch(visible, current.id);
  }
}

function configureOffscreen(width, height) {
  for (const target of [rembgCanvas, editsCanvas, initialEditsCanvas, maskCanvas, overlayCanvas, edgeCanvas]) {
    target.width = width;
    target.height = height;
  }
}

async function loadItem(itemId) {
  clearTimeout(autosaveTimer);
  if (hostedMode && current && current.id !== itemId) await releaseCurrentClaim();
  $('#empty-state').classList.remove('visible');
  const listed = items.find(item => item.id === itemId);
  if (listed?.read_only) {
    await syncPrefetch([], null);
    await showPublishedItem(listed);
    return;
  }
  $('#published-state').classList.remove('visible');
  setEditorDisabled(false);
  setSidebarMode('edit');
  $('#rereview').hidden = true;
  $('#edit-metadata').hidden = true;
  $('#view-hint').textContent = 'Wheel zoom · middle drag pans';
  $('#loading').textContent = 'Preparing mask…';
  $('#loading').classList.add('visible');
  setStatus('');
  try {
    const details = await api(`/api/items/${encodeURIComponent(itemId)}/prepare`, { method: 'POST' });
    const images = [loadImage(details.source_url), loadImage(details.rembg_url)];
    if (details.edits_url) images.push(loadImage(details.edits_url));
    const loaded = await Promise.all(images);

    current = items.find(item => item.id === itemId);
    state = details.state;
    sourceImage = loaded[0];
    rembgImage = loaded[1];
    configureOffscreen(details.width, details.height);
    rembgCanvas.getContext('2d').drawImage(rembgImage, 0, 0);
    const editsCtx = editsCanvas.getContext('2d');
    editsCtx.clearRect(0, 0, details.width, details.height);
    if (loaded[2]) editsCtx.drawImage(loaded[2], 0, 0);
    initialEditsCanvas.getContext('2d').drawImage(editsCanvas, 0, 0);
    strokes = [];
    activeStroke = null;
    activeLength = null;
    editsDirty = false;
    metadataDirty = false;
    if (hostedMode) startClaimHeartbeat(itemId);

    setItemTitle(current);
    setTool(tool);
    $('#source-info').textContent = current.has_alternative
      ? 'Alternative image active. Drop or paste another to replace it.'
      : 'Drop or paste an image onto the canvas.';
    $('#reset-catalog').hidden = !current.has_alternative;
    $('#threshold').value = state.alpha_threshold;
    $('#threshold-value').textContent = state.alpha_threshold;
    $$('.rating button').forEach(button =>
      button.classList.toggle('active', button.dataset.rating === state.rating));
    updateLineInfo();
    recomputeMask();
    fitView();
    setStatus(hostedMode ? 'Claimed for up to 15 minutes' :
      state.status === 'done' ? 'Completed' : 'Draft autosaves locally');
    await syncPrefetch(visibleItems(), itemId);
  } catch (error) {
    if (hostedMode) {
      current = state = sourceImage = rembgImage = null;
      setEditorDisabled(true);
      setSidebarMode('empty');
      render();
    }
    setStatus(error.message, true);
  } finally {
    $('#loading').classList.remove('visible');
  }
}

async function uploadAlternative(file) {
  if (!current || !file) return;
  if ((current.read_only || state?.rating || state?.main_length || editsDirty) &&
      !confirm('Replace this source image and clear its current mask edits, rating, and length line?')) return;
  clearTimeout(autosaveTimer);
  await saveChain;
  const itemId = current.id;
  const form = new FormData();
  form.append('image', file, file.name || 'clipboard-image.png');
  $('#loading').textContent = 'Masking alternative image…';
  $('#loading').classList.add('visible');
  setStatus('Uploading alternative image…');
  try {
    await api(`/api/items/${encodeURIComponent(itemId)}/alternative`, { method: 'POST', body: form });
    await refreshItems();
    await loadItem(itemId);
    setStatus('Alternative image active');
  } catch (error) {
    setStatus(error.message, true);
  } finally {
    $('#loading').classList.remove('visible');
  }
}

async function resetToCatalog() {
  if (!current?.has_alternative) return;
  if (!confirm('Reset to the catalog image and clear the current mask edits, rating, and length line?')) return;
  clearTimeout(autosaveTimer);
  await saveChain;
  const itemId = current.id;
  $('#loading').textContent = 'Restoring catalog image…';
  $('#loading').classList.add('visible');
  setStatus('Restoring catalog image…');
  try {
    await api(`/api/items/${encodeURIComponent(itemId)}/alternative`, { method: 'DELETE' });
    await refreshItems();
    await loadItem(itemId);
    setStatus('Catalog image restored');
  } catch (error) {
    setStatus(error.message, true);
  } finally {
    $('#loading').classList.remove('visible');
  }
}

async function rereview() {
  if (!current?.published) return;
  if (!confirm('Re-review this item? Its published dataset record will be replaced when you finish.')) return;
  const itemId = current.id;
  $('#loading').textContent = 'Starting re-review…';
  $('#loading').classList.add('visible');
  try {
    await api(`/api/items/${encodeURIComponent(itemId)}/rereview`, { method: 'POST' });
    await refreshItems();
    await loadItem(itemId);
  } catch (error) {
    setStatus(error.message, true);
  } finally {
    $('#loading').classList.remove('visible');
  }
}

async function editIndependentMetadata() {
  if (!current?.independent || current.pending_review) return;
  const metadata = await MetadataDialog.open(
    current.metadata,
    'Submit metadata update',
  );
  if (!metadata) return;
  try {
    const itemId = current.id;
    await api(`/api/community/${encodeURIComponent(itemId)}/metadata`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(metadata),
    });
    await refreshItems();
    await loadItem(itemId);
    setStatus('Metadata update submitted for review');
  } catch (error) {
    setStatus(error.message, true);
  }
}

function fitView() {
  if (!sourceImage) return;
  const width = wrap.clientWidth;
  const height = wrap.clientHeight;
  view.fit = Math.min(width / sourceImage.naturalWidth, height / sourceImage.naturalHeight) * 0.94;
  view.zoom = view.fit;
  view.x = (width - sourceImage.naturalWidth * view.zoom) / 2;
  view.y = (height - sourceImage.naturalHeight * view.zoom) / 2;
  render();
}

function resizeCanvas() {
  const ratio = window.devicePixelRatio || 1;
  const width = Math.max(1, Math.round(wrap.clientWidth * ratio));
  const height = Math.max(1, Math.round(wrap.clientHeight * ratio));
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
    render();
  }
}

function recomputeMask() {
  if (!rembgImage) return;
  const width = rembgCanvas.width;
  const height = rembgCanvas.height;
  const base = rembgCanvas.getContext('2d').getImageData(0, 0, width, height).data;
  const edits = editsCanvas.getContext('2d').getImageData(0, 0, width, height).data;
  const maskCtx = maskCanvas.getContext('2d');
  const overlayCtx = overlayCanvas.getContext('2d');
  const mask = maskCtx.createImageData(width, height);
  const overlay = overlayCtx.createImageData(width, height);
  maskForeground = new Uint8Array(width * height);
  const threshold = Number($('#threshold').value);

  for (let i = 0; i < base.length; i += 4) {
    let isForeground = base[i + 3] >= threshold;
    if (edits[i + 3] > 0) isForeground = edits[i] >= 128;
    if (isForeground) {
      const pixel = i / 4;
      maskForeground[pixel] = 1;
      mask.data[i] = mask.data[i + 1] = mask.data[i + 2] = mask.data[i + 3] = 255;
    } else {
      overlay.data[i] = 0;
      overlay.data[i + 1] = 0;
      overlay.data[i + 2] = 0;
      overlay.data[i + 3] = 255;
    }
  }
  maskCtx.putImageData(mask, 0, 0);
  overlayCtx.putImageData(overlay, 0, 0);
  recomputeEdgeRegion(0, 0, width, height);
  render();
}

function recomputeEdgeRegion(left, top, right, bottom) {
  const width = maskCanvas.width;
  const height = maskCanvas.height;
  left = Math.max(0, left);
  top = Math.max(0, top);
  right = Math.min(width, right);
  bottom = Math.min(height, bottom);
  if (left >= right || top >= bottom) return;

  const edgeCtx = edgeCanvas.getContext('2d');
  const edgeImage = edgeCtx.createImageData(right - left, bottom - top);
  const isForeground = (x, y) => maskForeground[y * width + x];
  const candidateLeft = Math.max(0, left - 2);
  const candidateTop = Math.max(0, top - 2);
  const candidateRight = Math.min(width, right + 2);
  const candidateBottom = Math.min(height, bottom + 2);

  for (let y = candidateTop; y < candidateBottom; y++) {
    for (let x = candidateLeft; x < candidateRight; x++) {
      if (!isForeground(x, y)) continue;
      if (x !== 0 && y !== 0 && x !== width - 1 && y !== height - 1 &&
          isForeground(x - 1, y) && isForeground(x + 1, y) &&
          isForeground(x, y - 1) && isForeground(x, y + 1)) continue;
      for (let dy = -2; dy <= 2; dy++) {
        for (let dx = -2; dx <= 2; dx++) {
          if (dx * dx + dy * dy > 4) continue;
          const px = x + dx;
          const py = y + dy;
          if (px < left || py < top || px >= right || py >= bottom) continue;
          edgeImage.data[((py - top) * (right - left) + px - left) * 4 + 3] = 255;
        }
      }
      if (x >= left && y >= top && x < right && y < bottom) {
        const index = ((y - top) * (right - left) + x - left) * 4;
        edgeImage.data[index] = edgeImage.data[index + 1] = edgeImage.data[index + 2] = 255;
        edgeImage.data[index + 3] = 255;
      }
    }
  }
  edgeCtx.putImageData(edgeImage, left, top);
}

function recomputeMaskSegment(from, to, size) {
  const width = rembgCanvas.width;
  const height = rembgCanvas.height;
  const padding = size / 2 + 1;
  const left = Math.max(0, Math.floor(Math.min(from[0], to[0]) - padding));
  const top = Math.max(0, Math.floor(Math.min(from[1], to[1]) - padding));
  const right = Math.min(width, Math.ceil(Math.max(from[0], to[0]) + padding));
  const bottom = Math.min(height, Math.ceil(Math.max(from[1], to[1]) + padding));
  if (left >= right || top >= bottom) return;

  const regionWidth = right - left;
  const regionHeight = bottom - top;
  const base = rembgCanvas.getContext('2d').getImageData(left, top, regionWidth, regionHeight).data;
  const edits = editsCanvas.getContext('2d').getImageData(left, top, regionWidth, regionHeight).data;
  const maskCtx = maskCanvas.getContext('2d');
  const overlayCtx = overlayCanvas.getContext('2d');
  const mask = maskCtx.createImageData(regionWidth, regionHeight);
  const overlay = overlayCtx.createImageData(regionWidth, regionHeight);
  const threshold = Number($('#threshold').value);

  for (let i = 0; i < base.length; i += 4) {
    let isForeground = base[i + 3] >= threshold;
    if (edits[i + 3] > 0) isForeground = edits[i] >= 128;
    const pixel = i / 4;
    const x = pixel % regionWidth;
    const y = Math.floor(pixel / regionWidth);
    maskForeground[(top + y) * width + left + x] = Number(isForeground);
    if (isForeground) {
      mask.data[i] = mask.data[i + 1] = mask.data[i + 2] = mask.data[i + 3] = 255;
    } else {
      overlay.data[i + 3] = 255;
    }
  }
  maskCtx.putImageData(mask, left, top);
  overlayCtx.putImageData(overlay, left, top);
  recomputeEdgeRegion(left - 3, top - 3, right + 3, bottom + 3);
}

function render() {
  const ratio = window.devicePixelRatio || 1;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (!sourceImage) return;
  ctx.setTransform(ratio * view.zoom, 0, 0, ratio * view.zoom, ratio * view.x, ratio * view.y);
  ctx.imageSmoothingEnabled = true;
  if (current?.read_only && !rembgImage) ctx.filter = 'invert(1)';
  ctx.drawImage(sourceImage, 0, 0);
  ctx.filter = 'none';

  if (current?.read_only) return;
  if (viewMode === 'overlay') {
    ctx.globalAlpha = Number($('#opacity').value) / 100;
    ctx.drawImage(overlayCanvas, 0, 0);
    ctx.globalAlpha = 1;
    ctx.drawImage(edgeCanvas, 0, 0);
  } else if (viewMode === 'cutout') {
    ctx.globalCompositeOperation = 'destination-in';
    ctx.drawImage(maskCanvas, 0, 0);
    ctx.globalCompositeOperation = 'source-over';
  }
  drawLength(ctx);
  if (hoverPoint && (tool === 'add' || tool === 'erase')) {
    ctx.save();
    ctx.strokeStyle = '#000';
    ctx.lineWidth = 4 / view.zoom;
    ctx.beginPath();
    ctx.arc(hoverPoint[0], hoverPoint[1], Number($('#brush').value) / 2, 0, Math.PI * 2);
    ctx.stroke();
    ctx.strokeStyle = tool === 'add' ? '#4ade80' : '#fb7185';
    ctx.lineWidth = 2 / view.zoom;
    ctx.beginPath();
    ctx.arc(hoverPoint[0], hoverPoint[1], Number($('#brush').value) / 2, 0, Math.PI * 2);
    ctx.stroke();
    ctx.restore();
  }
}

function drawLength(target) {
  const line = activeLength || state?.main_length;
  if (!line) return;
  const start = line.start;
  const end = line.end;
  target.save();
  target.strokeStyle = '#fbbf24';
  target.fillStyle = '#fbbf24';
  target.lineWidth = 3 / view.zoom;
  target.beginPath();
  target.arc(start[0], start[1], 5 / view.zoom, 0, Math.PI * 2);
  target.fill();
  target.beginPath();
  target.moveTo(start[0], start[1]);
  target.lineTo(end[0], end[1]);
  target.stroke();
  const angle = Math.atan2(end[1] - start[1], end[0] - start[0]);
  const size = 14 / view.zoom;
  target.beginPath();
  target.moveTo(end[0], end[1]);
  target.lineTo(end[0] - size * Math.cos(angle - Math.PI / 6), end[1] - size * Math.sin(angle - Math.PI / 6));
  target.lineTo(end[0] - size * Math.cos(angle + Math.PI / 6), end[1] - size * Math.sin(angle + Math.PI / 6));
  target.closePath();
  target.fill();
  target.restore();
}

function imagePoint(event) {
  const rect = canvas.getBoundingClientRect();
  return [
    (event.clientX - rect.left - view.x) / view.zoom,
    (event.clientY - rect.top - view.y) / view.zoom,
  ];
}

function inside(point) {
  return sourceImage && point[0] >= 0 && point[1] >= 0 &&
    point[0] <= sourceImage.naturalWidth && point[1] <= sourceImage.naturalHeight;
}

function paintSegment(from, to, mode, size) {
  const targetCtx = editsCanvas.getContext('2d');
  targetCtx.save();
  targetCtx.lineCap = 'round';
  targetCtx.lineJoin = 'round';
  targetCtx.lineWidth = size;
  targetCtx.globalCompositeOperation = 'source-over';
  targetCtx.strokeStyle = mode === 'add' ? '#fff' : '#000';
  targetCtx.fillStyle = targetCtx.strokeStyle;
  targetCtx.beginPath();
  if (from[0] === to[0] && from[1] === to[1]) {
    targetCtx.arc(from[0], from[1], size / 2, 0, Math.PI * 2);
    targetCtx.fill();
  } else {
    targetCtx.moveTo(from[0], from[1]);
    targetCtx.lineTo(to[0], to[1]);
    targetCtx.stroke();
  }
  targetCtx.restore();
}

function drawStroke(stroke, live = true) {
  for (let i = 1; i < stroke.points.length; i++) {
    const from = stroke.points[i - 1];
    const to = stroke.points[i];
    paintSegment(from, to, stroke.mode, stroke.size);
    if (live) recomputeMaskSegment(from, to, stroke.size);
  }
}

function updateCanvasCursor() {
  canvas.style.cursor = panning ? 'grabbing' :
    current?.read_only ? 'grab' :
      tool === 'length' ? 'crosshair' : hoverPoint ? 'none' : 'default';
}

function setTool(nextTool) {
  tool = nextTool;
  $$('.tool').forEach(button => button.classList.toggle('active', button.dataset.tool === tool));
  updateCanvasCursor();
  render();
}

function markDirty(paint = false) {
  if (!state) return;
  state.status = 'pending';
  metadataDirty = true;
  if (paint) editsDirty = true;
  if (current) current.status = 'pending';
  clearTimeout(autosaveTimer);
  if (!hostedMode) autosaveTimer = setTimeout(() => save('pending'), 700);
  refreshProgressText();
}

function refreshProgressText() {
  const matching = visibleItems();
  if (hostedMode) {
    const catalog = items.filter(item => item.workflow_status === 'in_catalog').length;
    $('#progress').textContent = `${matching.length} matching · ${catalog} in catalog`;
    return;
  }
  const done = matching.filter(item => item.status === 'done').length;
  $('#progress').textContent = `${done} of ${matching.length} matching completed`;
}

function canvasBlob(target) {
  return new Promise(resolve => target.toBlob(resolve, 'image/png'));
}

async function performSave(status) {
  if (!current || !state) return;
  const itemId = current.id;
  state.alpha_threshold = Number($('#threshold').value);
  state.status = status;
  const form = new FormData();
  form.append('state_json', JSON.stringify(state));
  if (editsDirty) form.append('edits', await canvasBlob(editsCanvas), 'edits.png');
  const result = await api(`/api/items/${encodeURIComponent(itemId)}/save`, { method: 'POST', body: form });
  editsDirty = false;
  metadataDirty = false;
  const listed = items.find(item => item.id === itemId);
  if (listed) Object.assign(listed, result);
  refreshProgressText();
  setStatus(status === 'done'
    ? (hostedMode ? 'Submitted for review' : 'Saved and exported')
    : 'Draft saved');
  if (status === 'done') window.refreshModerationCount?.();
  return result;
}

function save(status) {
  clearTimeout(autosaveTimer);
  const operation = saveChain.then(
    () => performSave(status),
    () => performSave(status),
  );
  saveChain = operation.catch(() => {});
  return operation.catch(error => {
    setStatus(error.message, true);
    throw error;
  });
}

async function navigate(direction) {
  if (!current) return;
  if (!hostedMode && (metadataDirty || editsDirty)) {
    try { await save('pending'); } catch (_) { return; }
  }
  const visible = visibleItems();
  if (!visible.length) return;
  let index = visible.findIndex(item => item.id === current.id);
  if (index < 0) index = direction > 0 ? -1 : 0;
  index = (index + direction + visible.length) % visible.length;
  await loadItem(visible[index].id);
}

async function saveAndNext() {
  if (!state?.rating) return setStatus('Choose a rating first', true);
  if (state.rating !== 'unusable' && !state.main_length) {
    return setStatus('Usable items require a base-to-tip line', true);
  }
  const oldIndex = items.findIndex(item => item.id === current.id);
  try {
    await save('done');
    clearInterval(claimHeartbeat);
    await refreshItems();
  } catch (_) { return; }
  const candidates = visibleItems();
  if (!candidates.length) {
    await syncPrefetch([], null);
    showEmptyState();
    return setStatus(hostedMode ? 'Submitted for review' : 'Queue complete');
  }
  const next = items.slice(oldIndex + 1).concat(items.slice(0, oldIndex + 1))
    .find(item => candidates.some(candidate => candidate.id === item.id));
  if (next) await loadItem(next.id);
}

async function downloadCurrent() {
  if (!current || !state) return;
  if (!state.rating) return setStatus('Choose a rating first', true);
  if (state.rating === 'unusable') {
    return setStatus('An unusable item has no silhouette to download', true);
  }
  if (!state.main_length) {
    return setStatus('Usable items require a base-to-tip line', true);
  }
  const button = $('#download-current');
  button.disabled = true;
  state.alpha_threshold = Number($('#threshold').value);
  state.status = 'done';
  const form = new FormData();
  form.append('state_json', JSON.stringify(state));
  form.append('download_only', 'true');
  if (editsDirty) form.append('edits', await canvasBlob(editsCanvas), 'edits.png');
  try {
    const response = await fetch(
      `/api/items/${encodeURIComponent(current.id)}/save`,
      {method: 'POST', body: form},
    );
    if (!response.ok) throw await responseError(response);
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement('a');
    link.href = url;
    link.download = `${current.id}.zip`;
    link.click();
    URL.revokeObjectURL(url);
    setStatus('Archive downloaded; the product remains claimed and can still be submitted.');
  } catch (error) {
    setStatus(error.message, true);
  } finally {
    button.disabled = false;
  }
}

function updateLineInfo() {
  if (activeLength) {
    const length = Math.hypot(
      activeLength.end[0] - activeLength.start[0],
      activeLength.end[1] - activeLength.start[1],
    );
    $('#line-info').textContent = `Usable length: ${length.toFixed(1)} px`;
    return;
  }
  const line = state?.main_length;
  if (!line) {
    $('#line-info').textContent = 'Select Length, then mark the usable portion toward the tip.';
    return;
  }
  const length = Math.hypot(line.end[0] - line.start[0], line.end[1] - line.start[1]);
  $('#line-info').textContent = `Usable length: ${length.toFixed(1)} px`;
}

canvas.addEventListener('pointerdown', event => {
  if (event.button === 1) {
    event.preventDefault();
    suppressPasteUntil = performance.now() + 1000;
  }
  if (!sourceImage) return;
  canvas.setPointerCapture(event.pointerId);
  if (current?.read_only || event.button === 1) {
    panning = { x: event.clientX, y: event.clientY, ox: view.x, oy: view.y };
    canvas.style.cursor = 'grabbing';
    return;
  }
  const point = imagePoint(event);
  if (!inside(point)) return;
  if (tool === 'length') {
    activeLength = { start: point, end: point };
    updateLineInfo();
    render();
    return;
  }
  if (tool === 'add' || tool === 'erase') {
    const size = Number($('#brush').value);
    activeStroke = { mode: tool, size, points: [point, point] };
    drawStroke(activeStroke);
    render();
  }
});
canvas.addEventListener('auxclick', event => {
  if (event.button === 1) event.preventDefault();
});

canvas.addEventListener('pointermove', event => {
  const pointer = imagePoint(event);
  hoverPoint = inside(pointer) ? pointer : null;
  updateCanvasCursor();
  if (panning) {
    view.x = panning.ox + event.clientX - panning.x;
    view.y = panning.oy + event.clientY - panning.y;
    render();
    return;
  }
  if (activeLength) {
    activeLength.end = [
      Math.max(0, Math.min(sourceImage.naturalWidth, pointer[0])),
      Math.max(0, Math.min(sourceImage.naturalHeight, pointer[1])),
    ];
    updateLineInfo();
    render();
    return;
  }
  if (!activeStroke) { render(); return; }
  const point = pointer;
  const previous = activeStroke.points.at(-1);
  activeStroke.points.push(point);
  const segment = { ...activeStroke, points: [previous, point] };
  drawStroke(segment);
  render();
});

function finishPointer(cancelled = false) {
  if (panning) {
    panning = null;
    updateCanvasCursor();
  }
  if (activeStroke) {
    strokes.push(activeStroke);
    activeStroke = null;
    render();
    markDirty(true);
  }
  if (activeLength) {
    const length = Math.hypot(
      activeLength.end[0] - activeLength.start[0],
      activeLength.end[1] - activeLength.start[1],
    );
    if (!cancelled && length >= 1) {
      state.main_length = activeLength;
      markDirty();
    }
    activeLength = null;
    updateLineInfo();
    render();
  }
}
canvas.addEventListener('pointerup', () => finishPointer(false));
canvas.addEventListener('pointercancel', () => finishPointer(true));
canvas.addEventListener('pointerleave', () => {
  hoverPoint = null;
  updateCanvasCursor();
  render();
});

canvas.addEventListener('wheel', event => {
  if (!sourceImage) return;
  event.preventDefault();
  const rect = canvas.getBoundingClientRect();
  const sx = event.clientX - rect.left;
  const sy = event.clientY - rect.top;
  const ix = (sx - view.x) / view.zoom;
  const iy = (sy - view.y) / view.zoom;
  const factor = Math.exp(-event.deltaY * 0.0015);
  view.zoom = Math.max(view.fit * 0.25, Math.min(16, view.zoom * factor));
  view.x = sx - ix * view.zoom;
  view.y = sy - iy * view.zoom;
  render();
}, { passive: false });

$('#threshold').addEventListener('input', event => {
  $('#threshold-value').textContent = event.target.value;
  state.alpha_threshold = Number(event.target.value);
  recomputeMask();
  markDirty();
});
$('#opacity').addEventListener('input', event => {
  $('#opacity-value').textContent = `${event.target.value}%`;
  render();
});
$('#brush').addEventListener('input', event => {
  $('#brush-value').textContent = `${event.target.value} px`;
  render();
});
$$('.tool').forEach(button => button.addEventListener('click', () => setTool(button.dataset.tool)));
$$('.view').forEach(button => button.addEventListener('click', () => {
  viewMode = button.dataset.view;
  $$('.view').forEach(item => item.classList.toggle('active', item === button));
  render();
}));
$$('.rating button').forEach(button => button.addEventListener('click', () => {
  state.rating = button.dataset.rating;
  $$('.rating button').forEach(item => item.classList.toggle('active', item === button));
  markDirty();
}));

$('#undo').addEventListener('click', () => {
  if (!strokes.length) return;
  strokes.pop();
  const editsCtx = editsCanvas.getContext('2d');
  editsCtx.clearRect(0, 0, editsCanvas.width, editsCanvas.height);
  editsCtx.drawImage(initialEditsCanvas, 0, 0);
  strokes.forEach(stroke => drawStroke(stroke, false));
  recomputeMask();
  markDirty(true);
});
$('#reset-edits').addEventListener('click', () => {
  editsCanvas.getContext('2d').clearRect(0, 0, editsCanvas.width, editsCanvas.height);
  initialEditsCanvas.getContext('2d').clearRect(0, 0, editsCanvas.width, editsCanvas.height);
  strokes = [];
  recomputeMask();
  markDirty(true);
});
$('#reverse-line').addEventListener('click', () => {
  if (!state?.main_length) return;
  [state.main_length.start, state.main_length.end] = [state.main_length.end, state.main_length.start];
  updateLineInfo(); render(); markDirty();
});
$('#clear-line').addEventListener('click', () => {
  if (!state) return;
  state.main_length = null; activeLength = null;
  updateLineInfo(); render(); markDirty();
});
$('#fit').addEventListener('click', fitView);
$('#search-images').addEventListener('click', () => {
  const product = current?.products?.[0];
  if (!product) return setStatus('No catalog product is selected', true);
  const query = `${product.n} ${product.vn}`.trim();
  window.open(`https://www.google.com/search?tbm=isch&q=${encodeURIComponent(query)}`, '_blank', 'noopener');
});
$('#open-product').addEventListener('click', event => {
  window.open(event.currentTarget.dataset.url, '_blank', 'noopener');
});
$('#reset-catalog').addEventListener('click', resetToCatalog);
$('#rereview').addEventListener('click', rereview);
$('#edit-metadata').addEventListener('click', editIndependentMetadata);
wrap.addEventListener('dragover', event => {
  event.preventDefault();
  if (current) wrap.classList.add('dragging');
});
wrap.addEventListener('dragleave', () => wrap.classList.remove('dragging'));
wrap.addEventListener('drop', event => {
  event.preventDefault();
  wrap.classList.remove('dragging');
  const file = [...event.dataTransfer.files].find(item => item.type.startsWith('image/'));
  if (file) uploadAlternative(file);
  else setStatus('Drop an image file here', true);
});
$('#previous').addEventListener('click', () => navigate(-1));
$('#next').addEventListener('click', () => navigate(1));
$('#download-current').addEventListener('click', downloadCurrent);
$('#save-next').addEventListener('click', saveAndNext);
$('#filter').addEventListener('change', applyFilters);
$('#type-filter').addEventListener('change', applyFilters);
let filterTimer = null;
for (const input of $$('#catalog-filters input')) {
  input.addEventListener('input', () => {
    clearTimeout(filterTimer);
    filterTimer = setTimeout(applyFilters, 180);
  });
}
$('#clear-filters').addEventListener('click', () => {
  $('#filter').value = 'all';
  $('#name-filter').value = $('#vendor-filter').value = $('#type-filter').value = '';
  applyFilters();
});
$('#show-all').addEventListener('click', () => {
  $('#filter').value = 'all';
  $('#name-filter').value = $('#vendor-filter').value = $('#type-filter').value = '';
  applyFilters();
});
window.beforeAppLogout = releaseCurrentClaim;

window.addEventListener('keydown', event => {
  if (event.target.matches('input, select')) return;
  if (event.ctrlKey && event.key.toLowerCase() === 'z') { $('#undo').click(); event.preventDefault(); return; }
  if (event.code === 'Space') {
    if (!event.repeat) current?.read_only ? navigate(1) : saveAndNext();
    event.preventDefault();
    return;
  }
  if (event.key === 'a') setTool('add');
  else if (event.key === 'e') setTool('erase');
  else if (event.key === 'w') setTool('length');
  else if (event.key === '1') $$('.rating button')[0].click();
  else if (event.key === '2') $$('.rating button')[1].click();
  else if (event.key === '3') $$('.rating button')[2].click();
  else if (event.key === 'ArrowLeft') navigate(-1);
  else if (event.key === 'ArrowRight') navigate(1);
});
window.addEventListener('paste', event => {
  if (performance.now() < suppressPasteUntil) {
    event.preventDefault();
    return;
  }
  const file = [...event.clipboardData.items]
    .find(item => item.kind === 'file' && item.type.startsWith('image/'))?.getAsFile();
  if (!file) return;
  event.preventDefault();
  uploadAlternative(file);
});
window.addEventListener('resize', resizeCanvas);
window.addEventListener('pagehide', () => {
  if (hostedMode && current && !current.read_only) {
    fetch(`/api/items/${encodeURIComponent(current.id)}/release`, {
      method:'POST', keepalive:true,
    });
  }
});
new ResizeObserver(resizeCanvas).observe(wrap);

$('#help').addEventListener('click', () => $('#instructions').showModal());
try {
  if (!localStorage.getItem('batch-outliner-instructions-v1')) {
    $('#instructions').showModal();
    localStorage.setItem('batch-outliner-instructions-v1', 'shown');
  }
} catch (_) {}

(async () => {
  try {
    const session = await window.appNavigationReady;
    hostedMode = session.hosted;
    if (hostedMode) {
      $('#save-label').textContent = 'Submit';
      $('#source-info').textContent = 'Moving to another product discards unfinished work.';
    }
    const options = await api('/api/public/metadata-options');
    for (const type of options.product_types) {
      const option = document.createElement('option');
      option.value = option.textContent = type;
      $('#type-filter').append(option);
    }
    await refreshItems();
    const visible = visibleItems();
    const first = visible[0];
    if (first) await loadItem(first.id);
    else showEmptyState();
  } catch (error) {
    setStatus(error.message, true);
  }
})();
