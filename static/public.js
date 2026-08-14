const $ = selector => document.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];
const uploadScreen = $('#upload-screen');
const editorScreen = $('#editor-screen');
const uploadCard = $('#upload-card');
const picker = $('#image');
const uploadButton = $('#upload');
const canvas = $('#canvas');
const wrap = $('#canvas-wrap');
const ctx = canvas.getContext('2d');
const offscreen = () => document.createElement('canvas');
const sourceCanvas = offscreen();
const rembgCanvas = offscreen();
const editsCanvas = offscreen();
const maskCanvas = offscreen();
const overlayCanvas = offscreen();
const edgeCanvas = offscreen();

let viewMode = 'overlay';
let tool = 'add';
let strokes = [];
let activeStroke = null;
let mainLength = null;
let activeLength = null;
let hoverPoint = null;
let panning = null;
let imageReady = false;
let currentFile = null;
let sessionUser = null;
let productMetadata = null;
let archiveProof = null;
const view = {zoom: 1, x: 0, y: 0, fit: 1};

function setStatus(target, message, error = false) {
  target.textContent = message;
  target.classList.toggle('error', error);
}

async function responseError(response) {
  let message = `${response.status} ${response.statusText}`;
  try { message = (await response.json()).detail || message; } catch (_) {}
  return new Error(message);
}

async function json(response) {
  if (!response.ok) throw await responseError(response);
  return response.json();
}

function loadImage(url) {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error('Could not decode the image'));
    image.src = url;
  });
}

async function waitUntilReady(ticket) {
  while (true) {
    const state = await json(await fetch('/api/public/queue/status', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ticket}),
    }));
    if (state.status === 'ready') return;
    setStatus(
      $('#upload-status'),
      `Waiting in queue · position ${state.position} · expires in ${state.expires_in}s`,
    );
    await new Promise(resolve => setTimeout(resolve, 2000));
  }
}

function configureCanvases(width, height) {
  for (const target of [
    sourceCanvas, rembgCanvas, editsCanvas, maskCanvas, overlayCanvas, edgeCanvas,
  ]) {
    target.width = width;
    target.height = height;
  }
}

async function processImage(file) {
  if (!file || uploadButton.disabled) return;
  uploadButton.disabled = true;
  setStatus($('#upload-status'), 'Joining queue…');
  try {
    const sourceUrl = URL.createObjectURL(file);
    let source;
    try {
      source = await loadImage(sourceUrl);
    } finally {
      URL.revokeObjectURL(sourceUrl);
    }
    const queued = await json(await fetch('/api/public/queue', {method: 'POST'}));
    await waitUntilReady(queued.ticket);
    setStatus($('#upload-status'), 'Removing background…');
    const body = new FormData();
    body.append('ticket', queued.ticket);
    body.append('image', file, file.name);
    const response = await fetch('/api/public/rembg', {method: 'POST', body});
    if (!response.ok) throw await responseError(response);
    archiveProof = response.headers.get('X-Archive-Token');
    if (!archiveProof) throw new Error('The server did not return an archive token.');
    const resultUrl = URL.createObjectURL(await response.blob());
    let rembg;
    try {
      rembg = await loadImage(resultUrl);
    } finally {
      URL.revokeObjectURL(resultUrl);
    }

    configureCanvases(rembg.naturalWidth, rembg.naturalHeight);
    sourceCanvas.getContext('2d').drawImage(
      source, 0, 0, sourceCanvas.width, sourceCanvas.height,
    );
    rembgCanvas.getContext('2d').drawImage(rembg, 0, 0);
    strokes = [];
    activeStroke = null;
    mainLength = null;
    activeLength = null;
    imageReady = true;
    currentFile = file;
    $('#filename').textContent = file.name || 'Guest contribution';
    picker.value = '';
    uploadScreen.hidden = true;
    editorScreen.hidden = false;
    recomputeMask();
    resizeCanvas();
    fitView();
    updateLineInfo();
    setStatus($('#editor-status'), '');
  } catch (error) {
    setStatus($('#upload-status'), error.message, true);
  } finally {
    uploadButton.disabled = false;
  }
}

function fitView() {
  if (!imageReady) return;
  view.fit = Math.min(
    wrap.clientWidth / sourceCanvas.width,
    wrap.clientHeight / sourceCanvas.height,
  ) * .94;
  view.zoom = view.fit;
  view.x = (wrap.clientWidth - sourceCanvas.width * view.zoom) / 2;
  view.y = (wrap.clientHeight - sourceCanvas.height * view.zoom) / 2;
  render();
}

function resizeCanvas() {
  if (editorScreen.hidden) return;
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
  if (!imageReady) return;
  const width = rembgCanvas.width;
  const height = rembgCanvas.height;
  const base = rembgCanvas.getContext('2d').getImageData(0, 0, width, height).data;
  const edits = editsCanvas.getContext('2d').getImageData(0, 0, width, height).data;
  const maskCtx = maskCanvas.getContext('2d');
  const overlayCtx = overlayCanvas.getContext('2d');
  const edgeCtx = edgeCanvas.getContext('2d');
  const mask = maskCtx.createImageData(width, height);
  const overlay = overlayCtx.createImageData(width, height);
  const edge = edgeCtx.createImageData(width, height);
  const foreground = new Uint8Array(width * height);
  const threshold = Number($('#threshold').value);

  for (let index = 0; index < base.length; index += 4) {
    let included = base[index + 3] >= threshold;
    if (edits[index + 3] > 0) included = edits[index] >= 128;
    if (included) {
      foreground[index / 4] = 1;
      mask.data[index] = mask.data[index + 1] = mask.data[index + 2] = 255;
      mask.data[index + 3] = 255;
    } else {
      overlay.data[index + 3] = 255;
    }
  }

  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      const pixel = y * width + x;
      if (!foreground[pixel]) continue;
      if (
        x === 0 || y === 0 || x === width - 1 || y === height - 1 ||
        !foreground[pixel - 1] || !foreground[pixel + 1] ||
        !foreground[pixel - width] || !foreground[pixel + width]
      ) {
        const index = pixel * 4;
        edge.data[index] = edge.data[index + 1] = edge.data[index + 2] = 255;
        edge.data[index + 3] = 255;
      }
    }
  }
  maskCtx.putImageData(mask, 0, 0);
  overlayCtx.putImageData(overlay, 0, 0);
  edgeCtx.putImageData(edge, 0, 0);
  render();
}

function render() {
  const ratio = window.devicePixelRatio || 1;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (!imageReady) return;
  ctx.setTransform(
    ratio * view.zoom, 0, 0, ratio * view.zoom, ratio * view.x, ratio * view.y,
  );
  ctx.drawImage(sourceCanvas, 0, 0);
  if (viewMode === 'overlay') {
    ctx.globalAlpha = Number($('#opacity').value) / 100;
    ctx.drawImage(overlayCanvas, 0, 0);
    ctx.globalAlpha = 1;
    if (!activeStroke) ctx.drawImage(edgeCanvas, 0, 0);
  } else if (viewMode === 'cutout') {
    ctx.globalCompositeOperation = 'destination-in';
    ctx.drawImage(maskCanvas, 0, 0);
    ctx.globalCompositeOperation = 'source-over';
  }
  drawLength(ctx);
  if (hoverPoint && ['add', 'erase'].includes(tool)) {
    ctx.save();
    ctx.strokeStyle = tool === 'add' ? '#4ade80' : '#fb7185';
    ctx.lineWidth = 2 / view.zoom;
    ctx.beginPath();
    ctx.arc(hoverPoint[0], hoverPoint[1], Number($('#brush').value) / 2, 0, Math.PI * 2);
    ctx.stroke();
    ctx.restore();
  }
}

function drawLength(target) {
  const line = activeLength || mainLength;
  if (!line) return;
  const [start, end] = [line.start, line.end];
  target.save();
  target.strokeStyle = target.fillStyle = '#fbbf24';
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
  target.lineTo(
    end[0] - size * Math.cos(angle - Math.PI / 6),
    end[1] - size * Math.sin(angle - Math.PI / 6),
  );
  target.lineTo(
    end[0] - size * Math.cos(angle + Math.PI / 6),
    end[1] - size * Math.sin(angle + Math.PI / 6),
  );
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
  return imageReady && point[0] >= 0 && point[1] >= 0 &&
    point[0] <= sourceCanvas.width && point[1] <= sourceCanvas.height;
}

function paintLayer(target, from, to, mode, size, displayLayer = false) {
  const layer = target.getContext('2d');
  layer.save();
  layer.lineCap = layer.lineJoin = 'round';
  layer.lineWidth = size;
  if (!displayLayer) {
    layer.globalCompositeOperation = 'source-over';
    layer.strokeStyle = mode === 'add' ? '#fff' : '#000';
  } else if (target === maskCanvas) {
    layer.globalCompositeOperation = mode === 'add' ? 'source-over' : 'destination-out';
    layer.strokeStyle = '#fff';
  } else {
    layer.globalCompositeOperation = mode === 'add' ? 'destination-out' : 'source-over';
    layer.strokeStyle = '#000';
  }
  layer.fillStyle = layer.strokeStyle;
  layer.beginPath();
  if (from[0] === to[0] && from[1] === to[1]) {
    layer.arc(from[0], from[1], size / 2, 0, Math.PI * 2);
    layer.fill();
  } else {
    layer.moveTo(from[0], from[1]);
    layer.lineTo(to[0], to[1]);
    layer.stroke();
  }
  layer.restore();
}

function paintSegment(from, to, mode, size, live = true) {
  paintLayer(editsCanvas, from, to, mode, size);
  if (live) {
    paintLayer(maskCanvas, from, to, mode, size, true);
    paintLayer(overlayCanvas, from, to, mode, size, true);
  }
}

function drawStroke(stroke, live = true) {
  for (let index = 1; index < stroke.points.length; index++) {
    paintSegment(
      stroke.points[index - 1], stroke.points[index], stroke.mode, stroke.size, live,
    );
  }
}

function redrawStrokes() {
  editsCanvas.getContext('2d').clearRect(0, 0, editsCanvas.width, editsCanvas.height);
  strokes.forEach(stroke => drawStroke(stroke, false));
  recomputeMask();
}

function updateCanvasCursor() {
  canvas.style.cursor = panning ? 'grabbing' : tool === 'length' ? 'crosshair' :
    hoverPoint ? 'none' : 'default';
}

function setTool(nextTool) {
  tool = nextTool;
  $$('.tool').forEach(button => {
    button.classList.toggle('active', button.dataset.tool === tool);
  });
  updateCanvasCursor();
  render();
}

function updateLineInfo() {
  const line = activeLength || mainLength;
  $('#line-info').textContent = line
    ? `Usable length: ${Math.hypot(
      line.end[0] - line.start[0], line.end[1] - line.start[1],
    ).toFixed(1)} px`
    : 'Select Length, then mark the usable portion toward the tip.';
}

canvas.addEventListener('pointerdown', event => {
  if (!imageReady) return;
  canvas.setPointerCapture(event.pointerId);
  if (event.button === 1) {
    event.preventDefault();
    panning = {x: event.clientX, y: event.clientY, ox: view.x, oy: view.y};
    canvas.style.cursor = 'grabbing';
    return;
  }
  const point = imagePoint(event);
  if (!inside(point)) return;
  if (tool === 'length') {
    activeLength = {start: point, end: point};
    updateLineInfo();
    return render();
  }
  activeStroke = {
    mode: tool,
    size: Number($('#brush').value),
    points: [point, point],
  };
  drawStroke(activeStroke);
  render();
});

canvas.addEventListener('pointermove', event => {
  const point = imagePoint(event);
  hoverPoint = inside(point) ? point : null;
  updateCanvasCursor();
  if (panning) {
    view.x = panning.ox + event.clientX - panning.x;
    view.y = panning.oy + event.clientY - panning.y;
    return render();
  }
  if (activeLength) {
    activeLength.end = [
      Math.max(0, Math.min(sourceCanvas.width, point[0])),
      Math.max(0, Math.min(sourceCanvas.height, point[1])),
    ];
    updateLineInfo();
    return render();
  }
  if (!activeStroke) return render();
  const previous = activeStroke.points.at(-1);
  activeStroke.points.push(point);
  paintSegment(previous, point, activeStroke.mode, activeStroke.size);
  render();
});

function finishPointer(cancelled = false) {
  if (panning) {
    panning = null;
    setTool(tool);
  }
  if (activeStroke) {
    strokes.push(activeStroke);
    activeStroke = null;
    recomputeMask();
  }
  if (activeLength) {
    const length = Math.hypot(
      activeLength.end[0] - activeLength.start[0],
      activeLength.end[1] - activeLength.start[1],
    );
    if (!cancelled && length >= 1) mainLength = activeLength;
    activeLength = null;
    updateLineInfo();
    render();
  }
}

canvas.addEventListener('pointerup', () => finishPointer());
canvas.addEventListener('pointercancel', () => finishPointer(true));
canvas.addEventListener('pointerleave', () => {
  hoverPoint = null;
  updateCanvasCursor();
  render();
});
canvas.addEventListener('auxclick', event => {
  if (event.button === 1) event.preventDefault();
});
canvas.addEventListener('wheel', event => {
  if (!imageReady) return;
  event.preventDefault();
  const rect = canvas.getBoundingClientRect();
  const screenX = event.clientX - rect.left;
  const screenY = event.clientY - rect.top;
  const imageX = (screenX - view.x) / view.zoom;
  const imageY = (screenY - view.y) / view.zoom;
  view.zoom = Math.max(
    view.fit * .25,
    Math.min(16, view.zoom * Math.exp(-event.deltaY * .0015)),
  );
  view.x = screenX - imageX * view.zoom;
  view.y = screenY - imageY * view.zoom;
  render();
}, {passive: false});

$('#threshold').addEventListener('input', event => {
  $('#threshold-value').textContent = event.target.value;
  recomputeMask();
});
$('#opacity').addEventListener('input', event => {
  $('#opacity-value').textContent = `${event.target.value}%`;
  render();
});
$('#brush').addEventListener('input', event => {
  $('#brush-value').textContent = `${event.target.value} px`;
  render();
});
$$('.tool').forEach(button => {
  button.addEventListener('click', () => setTool(button.dataset.tool));
});
$$('.view').forEach(button => button.addEventListener('click', () => {
  viewMode = button.dataset.view;
  $$('.view').forEach(item => item.classList.toggle('active', item === button));
  render();
}));
$('#undo').addEventListener('click', () => {
  if (strokes.length) {
    strokes.pop();
    redrawStrokes();
  }
});
$('#reset-edits').addEventListener('click', () => {
  strokes = [];
  redrawStrokes();
});
$('#reverse-line').addEventListener('click', () => {
  if (!mainLength) return;
  [mainLength.start, mainLength.end] = [mainLength.end, mainLength.start];
  updateLineInfo();
  render();
});
$('#clear-line').addEventListener('click', () => {
  mainLength = activeLength = null;
  updateLineInfo();
  render();
});
$('#fit').addEventListener('click', fitView);

function canvasBlob(target) {
  return new Promise(resolve => target.toBlob(resolve, 'image/png'));
}

function slug(value) {
  return value.toLowerCase().normalize('NFKD').replace(/[^a-z0-9]+/g, '-')
    .replace(/^-|-$/g, '') || 'submission';
}

async function editMetadata(confirmLabel) {
  const metadata = await MetadataDialog.open({
    ...productMetadata,
    quality: $('#quality').value,
    source: 'contributor_photo',
  }, confirmLabel);
  if (metadata) {
    productMetadata = metadata;
    if (!mainLength && metadata.sizes.some(size => size.length !== null)) {
      setStatus(
        $('#editor-status'),
        'Mark the usable length in the editor before adding a size length.',
        true,
      );
      return null;
    }
  }
  return metadata;
}

async function submissionBody(metadata, includeSource = false) {
  const body = new FormData();
  body.append('metadata_json', JSON.stringify(metadata));
  if (mainLength) body.append('main_length_json', JSON.stringify(mainLength));
  body.append('mask', await canvasBlob(maskCanvas), 'mask.png');
  if (archiveProof) body.append('proof', archiveProof);
  if (includeSource) body.append('source', currentFile, currentFile.name || 'source.png');
  return body;
}

$('#download').addEventListener('click', async () => {
  if (!archiveProof) {
    setStatus($('#editor-status'), 'Process the image again before downloading another archive.', true);
    return;
  }
  const metadata = await editMetadata('Download archive');
  if (!metadata) return;
  const button = $('#download');
  button.disabled = true;
  setStatus($('#editor-status'), 'Building archive…');
  try {
    const body = await submissionBody(metadata);
    const response = await fetch('/api/public/archive', {method: 'POST', body});
    archiveProof = null;
    if (!response.ok) throw await responseError(response);
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement('a');
    link.href = url;
    link.download = `${slug(metadata.vendor)}-${slug(metadata.name)}.zip`;
    link.click();
    URL.revokeObjectURL(url);
    setStatus($('#editor-status'), 'Archive downloaded. Send the ZIP to the project maintainer.');
  } catch (error) {
    setStatus($('#editor-status'), error.message, true);
  } finally {
    button.disabled = false;
  }
});

$('#submit-review').addEventListener('click', async () => {
  if (!sessionUser || !currentFile) return;
  const metadata = await editMetadata('Submit for review');
  if (!metadata) return;
  const button = $('#submit-review');
  button.disabled = true;
  setStatus($('#editor-status'), 'Submitting for review…');
  try {
    const response = await fetch('/api/public/submit', {
      method: 'POST',
      body: await submissionBody(metadata, true),
    });
    if (!response.ok) throw await responseError(response);
    setStatus(
      $('#editor-status'),
      'Submitted for review. You can still download your own copy.',
    );
  } catch (error) {
    setStatus($('#editor-status'), error.message, true);
    button.disabled = false;
  }
});

$('#start-over').addEventListener('click', () => {
  if (!confirm('Discard this unsaved contribution and choose another image?')) return;
  imageReady = false;
  currentFile = null;
  productMetadata = null;
  archiveProof = null;
  editorScreen.hidden = true;
  uploadScreen.hidden = false;
  setStatus($('#upload-status'), '');
});

$('#upload-form').addEventListener('submit', event => {
  event.preventDefault();
  processImage(picker.files[0]);
});
uploadCard.addEventListener('dragover', event => {
  event.preventDefault();
  uploadCard.classList.add('dragging');
});
uploadCard.addEventListener('dragleave', event => {
  if (!uploadCard.contains(event.relatedTarget)) uploadCard.classList.remove('dragging');
});
uploadCard.addEventListener('drop', event => {
  event.preventDefault();
  uploadCard.classList.remove('dragging');
  const file = [...event.dataTransfer.files].find(item => item.type.startsWith('image/'));
  if (file) processImage(file);
  else setStatus($('#upload-status'), 'Drop an image file here.', true);
});
window.addEventListener('paste', event => {
  if (uploadScreen.hidden) return;
  const file = [...event.clipboardData.items]
    .find(item => item.kind === 'file' && item.type.startsWith('image/'))?.getAsFile();
  if (!file) return;
  event.preventDefault();
  processImage(file);
});
window.addEventListener('keydown', event => {
  if (editorScreen.hidden || event.target.matches('input, select, textarea')) return;
  if (event.ctrlKey && event.key.toLowerCase() === 'z') {
    $('#undo').click();
    event.preventDefault();
  } else if (event.key === 'a') setTool('add');
  else if (event.key === 'e') setTool('erase');
  else if (event.key === 'w') setTool('length');
});
window.addEventListener('resize', resizeCanvas);
new ResizeObserver(resizeCanvas).observe(wrap);

fetch('/api/session').then(json).then(session => {
  sessionUser = session.user;
  $('#catalog-choice').hidden = !sessionUser;
  $('#landing-logout').hidden = !sessionUser;
  $('#submit-review').hidden = !sessionUser;
  $('#guest-contact').hidden = Boolean(sessionUser);
  $('#session-status').textContent = sessionUser
    ? `Signed in as ${sessionUser.name}. Choose a starting point.`
    : 'Continue as a guest to turn a product photo into a downloadable outline and metadata. Your work stays separate from the catalog and won’t be submitted for review.';
  $('#independent-title').textContent = sessionUser
    ? 'Create a new product entry'
    : 'Choose a photo';
  $('#independent-description').textContent = sessionUser
    ? 'Start from your own photo instead of Toybox data, then download the result or submit it for review.'
    : 'Select, drop, or paste an image to get started.';
  $('#retention-hint').textContent = sessionUser
    ? 'Download your work or submit it for review.'
    : 'Nothing is saved on the server; download before leaving.';
}).catch(error => {
  $('#session-status').textContent = error.message;
});

$('#landing-logout').addEventListener('click', async () => {
  await fetch('/api/logout', {method: 'POST'});
  location.reload();
});
