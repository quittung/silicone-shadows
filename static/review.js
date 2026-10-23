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
let activeCrop = null;
let remaskBusy = false;
let hoverPoint = null;
let activeLength = null;
let panning = null;
let suppressPasteUntil = 0;
let editSession = null;
let editorBusy = false;
let loadGeneration = 0;
let saveChain = Promise.resolve();
let hostedMode = false;
let claimHeartbeat = null;
let navigationIds = null;
let prioritizeLeastRecent = true;
const view = { zoom: 1, x: 0, y: 0, fit: 1 };
let activeVariant = '';
let variantCollection = {outlines: [], sizes: [], general: true};
let savedOutlineMode = false;
let variantDialogMode = 'add';
let variantBusy = false;
let variantDialogTarget = '';
// Outline labels belong to this editor session, not the published dataset.
const outlineNumbers = new Map();
let outlineNumberItem = null;
let nextOutlineNumber = 1;

function outlineLabel(id) {
  if (!outlineNumbers.has(id)) outlineNumbers.set(id, nextOutlineNumber++);
  return `Outline ${outlineNumbers.get(id)}`;
}

function outlineIconButton(label, path) {
  const button = document.createElement('button');
  button.className = 'variant-icon';
  button.setAttribute('aria-label', label);
  button.title = label;
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('aria-hidden', 'true');
  const shape = document.createElementNS(svg.namespaceURI, 'path');
  shape.setAttribute('d', path);
  svg.append(shape); button.append(svg);
  return button;
}

function renderVariantControls() {
  const controls = $('#variant-controls');
  controls.hidden = !current;
  $('#variant-add').hidden = !!current?.read_only;
  $('#variant-add').disabled = variantBusy || editorBusy || remaskBusy || !variantCollection.sizes.length;
  const list = $('#variant-list');
  list.replaceChildren();
  for (const entry of variantCollection.outlines) {
    const row = document.createElement('section'); row.className = 'variant-row';
    const heading = document.createElement('div'); heading.className = 'variant-row-heading';
    const name = outlineLabel(entry.id);
    const radio = document.createElement('input');
    radio.type = 'radio'; radio.name = 'outline-selection';
    radio.className = 'variant-radio';
    radio.id = `outline-selection-${outlineNumbers.get(entry.id)}`;
    radio.checked = entry.id === activeVariant;
    radio.disabled = variantBusy || editorBusy || remaskBusy;
    radio.setAttribute('aria-label', name);
    radio.addEventListener('change', () => switchVariant(entry.id));
    const title = document.createElement('h3');
    title.textContent = name;
    heading.append(title);
    if (!current?.read_only) {
      const remove = outlineIconButton(`Delete ${outlineLabel(entry.id)}`,
        'M3 6h18 M9 6V3h6v3 M5 6l1 15h12l1-15 M10 10v7 M14 10v7');
      remove.classList.add('variant-delete');
      remove.disabled = variantBusy || editorBusy || remaskBusy || variantCollection.outlines.length <= 1;
      if (variantCollection.outlines.length <= 1) remove.title = 'Keep at least one outline';
      remove.addEventListener('click', () => {
        if (confirm(`Remove ${outlineLabel(entry.id)} and its editing work?`)) {
          $('#variants-panel').close();
          mutateVariant({action:'remove', id:entry.id});
        }
      });
      heading.append(remove);
    }
    const sizes = document.createElement('p');
    sizes.append(document.createTextNode(`Sizes: ${entry.id === '' ? 'All sizes without a specific outline' : entry.sizes.join(', ')}`));
    if (!current?.read_only) {
      const change = document.createElement('button'); change.textContent = 'Change';
      change.className = 'variant-change';
      change.setAttribute('aria-label', `Change sizes for ${outlineLabel(entry.id)}`);
      change.disabled = variantBusy || editorBusy || remaskBusy || !variantCollection.sizes.length;
      change.addEventListener('click', () => openVariantDialog('assign', entry.id));
      sizes.append(document.createTextNode(' '), change);
    }
    row.append(radio, heading, sizes);
    list.append(row);
  }
  const coverage = $('#variant-coverage');
  coverage.replaceChildren();
  coverage.hidden = !variantCollection.sizes.length;
  const table = document.createElement('table'); table.setAttribute('aria-label', 'Size coverage');
  const headers = document.createElement('tr');
  const values = document.createElement('tr');
  const suggestions = document.createElement('tr');
  const groups = suggestedSizeGroups();
  const wrongValues = wrongValueLabels(current);
  const groupsByOutline = new Map();
  if (groups.size) {
    headers.append(document.createElement('td'));
    for (const [row, text] of [[values, 'Outline'], [suggestions, 'Suggested']]) {
      const header = document.createElement('th'); header.scope = 'row'; header.textContent = text;
      row.append(header);
    }
  }
  const fallback = variantCollection.outlines.find(entry => entry.id === '');
  let hasFallback = false;
  let hasMissing = false;
  for (const size of variantCollection.sizes) {
    const specific = variantCollection.outlines.find(entry => entry.id !== '' && entry.sizes.includes(size));
    const outline = specific || fallback;
    const label = document.createElement('th'); label.scope = 'col';
    label.textContent = variantCollection.size_labels?.[size] || size;
    label.title = size;
    const value = document.createElement('td');
    value.textContent = outline ? `${outlineNumbers.get(outline.id)}${specific ? '' : '*'}` : '—';
    value.title = outline ? `${size}: ${outlineLabel(outline.id)}${specific ? '' : ' (fallback)'}` : `${size}: No outline`;
    value.setAttribute('aria-label', value.title);
    if (!outline) value.className = 'variant-uncovered';
    hasFallback ||= !!outline && !specific;
    hasMissing ||= !outline;
    headers.append(label); values.append(value);
    if (groups.size) {
      const group = groups.get(size) ?? null;
      const wrongValue = wrongValues.has(variantCollection.size_labels?.[size] || size);
      const suggestion = document.createElement('td'); suggestion.className = 'variant-suggested';
      suggestion.textContent = group !== null ? groupLetter(group) : wrongValue ? '?' : '–';
      suggestion.title = group !== null ? `${size}: Suggested group ${groupLetter(group)}`
        : wrongValue ? `${size}: Catalog measurements look wrong, so this size is left out`
        : `${size}: Not enough measurements to compare`;
      suggestion.setAttribute('aria-label', suggestion.title);
      suggestions.append(suggestion);
      if (outline && group !== null) {
        if (!groupsByOutline.has(outline.id)) groupsByOutline.set(outline.id, new Set());
        groupsByOutline.get(outline.id).add(group);
      }
    }
  }
  const head = document.createElement('thead'); head.append(headers);
  const body = document.createElement('tbody'); body.append(values);
  if (groups.size) body.append(suggestions);
  table.append(head, body); coverage.append(table);
  const legend = [hasFallback ? '* Fallback' : '', hasMissing ? '— No outline' : '',
    groups.size ? 'Same letter: similar proportions' : '',
    groups.size && wrongValues.size ? '? Catalog value looks wrong' : ''].filter(Boolean);
  if (legend.length) {
    const note = document.createElement('small');
    note.textContent = legend.join(' · ');
    coverage.append(note);
  }
  for (const [id, outlineGroups] of groupsByOutline) {
    if (outlineGroups.size < 2) continue;
    const warning = document.createElement('small'); warning.className = 'variant-mixed';
    const letters = [...outlineGroups].sort((left, right) => left - right).map(groupLetter);
    warning.textContent = `${outlineLabel(id)} covers groups ${new Intl.ListFormat('en').format(letters)}`;
    coverage.append(warning);
  }
  renderScalingNotice();
}

// Matches the server's size keys: full labels, falling back to short labels.
function sizeKey(size) {
  return String(size?.sl || size?.label || size?.ShortLabel || size?.short_label || '');
}

function groupLetter(group) {
  return String.fromCharCode(65 + group);
}

// Suggested outline groups by size key. Products sharing one catalog image can
// differ in proportions, so suggestions need exactly one measured size list.
function suggestedSizeGroups() {
  const lists = itemSizeLists(current).filter(sizes => sizes?.length);
  if (lists.length !== 1) return new Map();
  const groups = suggestOutlineGroups(lists[0]);
  if (groups.filter(group => group !== null).length < 2) return new Map();
  return new Map(lists[0].map((size, index) => [sizeKey(size), groups[index]]));
}

// Size lists for comparisons, without sizes whose catalog values look wrong.
function itemSizeLists(item) {
  // An independent working copy may have edited sizes that are not listed yet.
  if (item?.independent && item.metadata?.sizes) return [item.metadata.sizes];
  return (item?.products || []).map(product => withoutWrongValues(product.sz?.s, product.data_issues));
}

function itemDataIssues(item) {
  return (item?.products || []).flatMap(product => product.data_issues || []);
}

function wrongValueLabels(item) {
  return new Set(itemDataIssues(item).filter(issue => issue.wrong_value).flatMap(issue => issue.sizes));
}

function currentScalingWarning() {
  const check = current && worstScalingCheck(itemSizeLists(current));
  return check?.level && variantCollection.outlines.length <= 1 ? check : null;
}

function renderScalingNotice() {
  const notice = $('#scaling-notice');
  const check = current && worstScalingCheck(itemSizeLists(current));
  const issues = current ? itemDataIssues(current) : [];
  const flagged = !!check?.level;
  notice.hidden = !flagged && !issues.length;
  if (notice.hidden) return;
  const outlines = variantCollection.outlines.length;
  const covered = flagged && outlines > 1;
  notice.className = !flagged ? 'data' : covered ? 'covered' : check.level;
  $('#scaling-notice-title').textContent = flagged ? check.label : 'Catalog data may be wrong';
  $('#scaling-notice-step').hidden = $('#scaling-notice-advice').hidden = !flagged;
  if (flagged) {
    $('#scaling-notice-step').textContent = describeScalingStep(check);
    $('#scaling-notice-advice').textContent = covered
      ? `Sizes are split across ${outlines} outlines.`
      : `Every size uses one outline. ${current.read_only ? 'Edit to add' : 'Consider adding'} outlines for sizes with different proportions.`;
  }
  $('#scaling-notice-issues-lead').hidden = !flagged || !issues.length;
  $('#scaling-notice-issues').replaceChildren(...issues.map(issue => {
    const item = document.createElement('li');
    item.textContent = `${issue.detail[0].toUpperCase()}${issue.detail.slice(1)}.`;
    return item;
  }));
  const wrongValues = [...wrongValueLabels(current)];
  $('#scaling-notice-excluded').hidden = !wrongValues.length;
  $('#scaling-notice-excluded').textContent =
    `Left out of the size check: ${new Intl.ListFormat('en').format(wrongValues)}.`;
}

async function refreshVariants(itemId) {
  if (outlineNumberItem !== itemId) {
    outlineNumberItem = itemId;
    outlineNumbers.clear();
    nextOutlineNumber = 1;
  }
  variantCollection = await api(`/api/items/${encodeURIComponent(itemId)}/variants`);
  if (!variantCollection.outlines.some(entry => entry.id === activeVariant)) activeVariant = variantCollection.outlines[0]?.id || '';
  renderVariantControls();
}

function captureCurrentOutline() {
  if (!editSession || !state) return;
  state.alpha_threshold = Number($('#threshold').value);
  editSession.capture(activeVariant, {state, paint:editsCanvas, initialPaint:initialEditsCanvas, strokes, editsDirty});
}

async function switchVariant(id) {
  if (variantBusy || editorBusy || remaskBusy) return;
  variantBusy = true;
  try {
    if (editSession) {
      captureCurrentOutline(); activeVariant = id;
      await showDraftOutline(); updateEditorUrl();
    } else { activeVariant = id; await loadItem(current.id); }
  } catch (error) { setStatus(error.message, true); }
  finally {
    variantBusy = false; renderVariantControls();
    if ($('#variants-panel').open) $('#variant-list input:checked')?.focus();
  }
}

async function mutateVariant(change, selectId) {
  if (variantBusy || editorBusy || remaskBusy || !editSession) return;
  variantBusy = true; $('#variant-confirm').disabled = true;
  try {
    captureCurrentOutline(); editSession.changeVariant(change);
    variantCollection = editSession.collection();
    if (change.action === 'remove') outlineNumbers.delete(change.id);
    if (change.move_from != null && selectId != null && outlineNumbers.has(change.move_from)) {
      const number = outlineNumbers.get(change.move_from);
      outlineNumbers.delete(change.move_from); outlineNumbers.set(selectId, number);
    }
    activeVariant = selectId ?? (variantCollection.outlines.some(entry => entry.id === activeVariant)
      ? activeVariant : variantCollection.outlines[0]?.id) ?? '';
    $('#variant-dialog').close(); await showDraftOutline();
    metadataDirty = true; setStatus('Unsaved changes'); updateEditorUrl();
  } catch (error) { $('#variant-error').textContent = error.message; setStatus(error.message, true); }
  finally { variantBusy = false; $('#variant-confirm').disabled = false; renderVariantControls(); }
}

function openVariantDialog(mode, target = activeVariant) {
  variantDialogTarget = target;
  variantDialogMode = mode;
  $('#variants-panel').close();
  $('#variant-dialog-title').textContent = mode === 'add' ? 'Add outline' : `Change sizes · ${outlineLabel(target)}`;
  $('#variant-confirm').textContent = mode === 'add' ? 'Add outline' : 'Apply';
  $('#variant-error').textContent = '';
  const generalUnavailable = variantCollection.general && (mode === 'add' || target !== '');
  $('#variant-general').disabled = generalUnavailable;
  $('#variant-general-help').hidden = !generalUnavailable;
  $('#variant-general').checked = mode === 'assign' && target === '';
  $('#variant-specific').checked = !$('#variant-general').checked;
  const selected = variantCollection.outlines.find(entry => entry.id === target);
  const list = $('#variant-sizes'); list.replaceChildren();
  const groups = suggestedSizeGroups();
  for (const size of variantCollection.sizes) {
    const owner = variantCollection.outlines.find(entry => entry.sizes.includes(size));
    const label = document.createElement('label');
    const checkbox = document.createElement('input'); checkbox.type = 'checkbox'; checkbox.value = size;
    checkbox.checked = mode === 'assign' && !!selected?.sizes.includes(size);
    checkbox.disabled = !!owner && (mode === 'add' || owner.id !== target);
    label.append(checkbox, document.createTextNode(`${size}${checkbox.disabled ? ` (assigned to ${outlineLabel(owner.id)})` : ''}`));
    const group = groups.get(size) ?? null;
    if (group !== null) {
      const tag = document.createElement('span'); tag.className = 'variant-group-tag';
      tag.textContent = groupLetter(group); tag.title = `Suggested group ${groupLetter(group)}`;
      tag.setAttribute('aria-label', tag.title);
      label.append(tag);
    }
    list.append(label);
  }
  $('#variant-sizes').hidden = $('#variant-general').checked;
  $('#variant-dialog').showModal();
}

$('#variants-open').addEventListener('click', () => {
  renderVariantControls();
  $('#variants-panel').showModal();
});
$('#variants-close').addEventListener('click', () => $('#variants-panel').close());
$('#variant-add').addEventListener('click', () => openVariantDialog('add'));
$('#variant-scope').addEventListener('change', () => {
  $('#variant-sizes').hidden = $('#variant-general').checked;
});
$('#variant-cancel').addEventListener('click', () => {
  $('#variant-dialog').close();
  $('#variants-panel').showModal();
});
$('#variant-form').addEventListener('submit', event => {
  event.preventDefault();
  const sizes = [...$('#variant-sizes').querySelectorAll('input:checked')].map(input => input.value);
  const target = variantDialogTarget;
  if ($('#variant-general').checked) {
    if (variantDialogMode === 'assign' && target === '') {
      $('#variant-dialog').close();
      $('#variants-panel').showModal();
      return;
    }
    return mutateVariant({action:'general', move_from:variantDialogMode === 'assign' ? target : null}, '');
  }
  if (!sizes.length) { $('#variant-error').textContent = 'Select at least one size.'; return; }
  const id = variantDialogMode === 'add' || !target ? `v-${crypto.randomUUID()}` : target;
  mutateVariant({action:variantDialogMode === 'add' || !target ? 'add' : 'assign', id, sizes,
    move_from: variantDialogMode === 'assign' && !target ? '' : null}, id);
});
function editorUrlView(itemId = current?.id) {
  return {
    state: $('#filter').value,
    name: $('#name-filter').value,
    vendor: $('#vendor-filter').value,
    type: $('#type-filter').value,
    measurements: $('#measurements-filter').value,
    scaling: $('#scaling-filter').value,
    outlines: $('#outlines-filter').value,
    order: hostedMode ? $('#navigation-order').value : null,
    item: itemId || '',
  };
}

function updateEditorUrl(mode = 'replace', itemId = current?.id) {
  if (mode === 'none') return;
  let url = buildEditorUrl(location.pathname, editorUrlView(itemId));
  if (activeVariant && itemId) url += `${url.includes('?') ? '&' : '?'}variant=${encodeURIComponent(activeVariant)}`;
  if (url === `${location.pathname}${location.search}`) return;
  history[mode === 'push' ? 'pushState' : 'replaceState'](null, '', url);
}

function restoreEditorControls(urlState) {
  $('#filter').value = urlState.state;
  $('#name-filter').value = urlState.name;
  $('#vendor-filter').value = urlState.vendor;
  $('#type-filter').value = urlState.type;
  $('#measurements-filter').value = urlState.measurements;
  $('#scaling-filter').value = urlState.scaling;
  $('#outlines-filter').value = urlState.outlines;
  if ($('#type-filter').value !== urlState.type) $('#type-filter').value = '';
  if (hostedMode) {
    prioritizeLeastRecent = urlState.order !== 'catalog';
    $('#navigation-order').value = prioritizeLeastRecent ? 'least-recent' : 'catalog';
  }
}

function setStatus(message, error = false) {
  $('#status').textContent = message;
  $('#status').classList.toggle('error', error);
}

async function responseError(response) {
  let message = `${response.status} ${response.statusText}`;
  let detail;
  try {
    detail = (await response.json()).detail;
    message = detail?.message || detail || message;
  } catch (_) {}
  const error = new Error(message);
  error.status = response.status;
  error.currentItemId = detail?.current_item_id;
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
    image.src = cacheBust ? `${url}${url.includes('?') ? '&' : '?'}v=${Date.now()}` : url;
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
  if (!navigationIds) rebuildNavigation();
  refreshProgressText();
  return response;
}

function matchesFilters(item) {
  const selectedState = $('#filter').value;
  const workflowState = ['available', 'pending_review', 'in_catalog']
    .includes(selectedState);
  const workflowStatus = selectedState === 'available' ? 'never_worked' : selectedState;
  const stateMatches = selectedState === 'all' ||
    (workflowState ? item.workflow_status === workflowStatus : item.rating === selectedState);
  const claimMatches = selectedState !== 'available' || !item.claimed_by;
  return stateMatches && claimMatches && matchesCatalogFilters(item);
}

function orderedView(candidates) {
  if (!hostedMode || !prioritizeLeastRecent || $('#filter').value !== 'available') {
    return candidates;
  }
  return [...candidates].sort((left, right) =>
    (left.last_opened_at ?? -Infinity) - (right.last_opened_at ?? -Infinity));
}

function rebuildNavigation() {
  navigationIds = orderedView(items.filter(matchesFilters)).map(item => item.id);
}

function visibleItems() {
  const matching = new Map(
    items.filter(matchesFilters).map(item => [item.id, item]),
  );
  return (navigationIds || [...matching.keys()])
    .map(itemId => matching.get(itemId))
    .filter(Boolean);
}



function matchesCatalogFilters(item) {
  const name = $('#name-filter').value.trim().toLowerCase();
  const vendor = $('#vendor-filter').value;
  const type = $('#type-filter').value.trim().toLowerCase();
  const measuredOnly = $('#measurements-filter').value === 'measured';
  const scaling = $('#scaling-filter').value;
  const outlines = $('#outlines-filter').value;
  if (outlines !== 'all' && Boolean(item.variant_count) !== (outlines === 'multiple')) return false;
  if (scaling !== 'all' &&
      !meetsScalingLevel(worstScalingCheck(itemSizeLists(item)), scaling)) return false;
  const products = item.products?.length
    ? item.products
    : [{ n: item.id, vn: '', pt: '' }];
  return products.some(product =>
    (!measuredOnly || product.sz?.s?.some(size =>
      ['len', 'circ', 'wcirc', 'length', 'circumference', 'widest_circumference']
        .some(field => Number.isFinite(size[field]) && size[field] > 0))) &&
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
      await api(`/api/editor/items/${encodeURIComponent(itemId)}/claim`, {method:'POST'});
      if ($('#status').textContent === 'Connection interrupted; claim check will retry') {
        setStatus('Claim active');
      }
    } catch (error) {
      if (!error.status || error.status >= 500) {
        setStatus('Connection interrupted; claim check will retry', true);
        return;
      }
      clearInterval(claimHeartbeat);
      captureCurrentOutline();
      current.read_only = true;
      setEditorDisabled(true);
      setSidebarMode('read-only');
      $('#read-only-summary').textContent = 'Editing stopped';
      $('#read-only-info').textContent = `${error.message} Your working copy is still in this browser.`;
      $('#rereview').textContent = 'Resume editing';
      $('#rereview').hidden = false;
      $('#edit-metadata').hidden = true;
      setStatus(error.message, true);
      render();
    }
  }, 60_000);
}

async function releaseCurrentClaim() {
  clearInterval(claimHeartbeat);
  const itemId = editSession?.document.id;
  editSession = null; metadataDirty = editsDirty = false;
  if (hostedMode && itemId) {
    try { await fetch(`/api/editor/items/${encodeURIComponent(itemId)}/release`, {method:'POST'}); }
    catch (_) {}
  }
}

function showEmptyState(title, message) {
  clearInterval(claimHeartbeat);
  $('#claim-recovery').hidden = true;
  $('#show-all').hidden = false;
  const matchingCatalog = items.filter(matchesCatalogFilters);
  const complete = matchingCatalog.length > 0 &&
    $('#filter').value === 'available' &&
    matchingCatalog.every(item => item.workflow_status !== 'never_worked');
  const defaultTitle = complete ? 'All matching products reviewed' : 'No matching products';
  $('#empty-title').textContent = title || defaultTitle;
  $('#empty-message').textContent = message || (complete
    ? 'All products matching these catalog filters have been reviewed.'
    : 'Change or clear the filters to continue.');
  $('#empty-state').classList.add('visible');
  $('#source-unavailable').classList.remove('visible');
  $('#filename').textContent = title || defaultTitle;
  sourceImage = null;
  rembgImage = null;
  state = null;
  current = null;
  $('#variant-controls').hidden = true;
  $('#variant-blank').hidden = true;
  $('#scaling-notice').hidden = true;
  $('#published-state').classList.remove('visible');
  setSidebarMode('empty');
  setEditorDisabled(false);
  $('#reset-catalog').hidden = true;
  $('#open-product').hidden = true;
  $('#rereview').hidden = true;
  $('#edit-metadata').hidden = true;
  render();
}

function showClaimRecovery(error, retry) {
  showEmptyState('Another product is still open', 'You already have a product claimed for review.');
  $('#show-all').hidden = true;
  $('#claim-recovery').hidden = false;
  $('#claim-return').href = `/editor?item=${encodeURIComponent(error.currentItemId)}&measurements=all`;
  $('#claim-return').focus();
  $('#claim-release').onclick = async () => {
    const button = $('#claim-release');
    button.disabled = true;
    try {
      const response = await fetch(`/api/editor/items/${encodeURIComponent(error.currentItemId)}/release`, {method:'POST'});
      if (!response.ok) throw await responseError(response);
      $('#claim-recovery').hidden = true;
      await retry();
    } catch (releaseError) {
      setStatus(`Could not release product: ${releaseError.message}`, true);
    } finally {
      button.disabled = false;
    }
  };
}

function setEditorDisabled(disabled) {
  $$('.editor button:not(#cancel-revision), .editor input, button.editor').forEach(control => control.disabled = disabled);
}

function setSidebarMode(mode) {
  $('#edit-panel').hidden = mode !== 'edit';
  $('#read-only-panel').hidden = mode !== 'read-only';
}

function setItemTitle(item) {
  const names = item.products?.map(product => product.n).join(' / ');
  const vendors = [...new Set(item.products?.map(product => product.vn) || [])].join(' / ');
  $('#filename').textContent = names ? `${names} · ${vendors}` : item.filename;
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
  $('#source-unavailable').classList.remove('visible');
  const source = item.provenance === 'alternative' ? 'alternative image' :
    item.provenance === 'mixed' ? 'mixed image sources' : 'catalog image';
  const stage = item.pending_review ? 'Pending review' : 'In catalog';
  $('#published-note').textContent = `${stage} · ${item.rating?.replace('_', ' ') || 'mixed rating'} · ${source}`;
  $('#read-only-summary').textContent = `${stage} · ${item.rating?.replace('_', ' ') || 'mixed rating'} · ${source}`;
  $('#view-hint').textContent = 'Wheel zoom · drag pans';
  $('#read-only-info').textContent = item.pending_review
    ? 'This submission is waiting for moderation. Reviewers can edit a working copy.'
    : 'Edit a working copy. The saved result stays intact until you explicitly save the revision.';
  $('#reset-catalog').hidden = hostedMode || !item.has_alternative;
  $('#rereview').textContent = 'Edit';
  $('#rereview').hidden = !item.can_edit;
  $('#cancel-revision').hidden = true;
  $('#revision-note').hidden = true;
  $('#revision-metadata').hidden = true;
  $('#edit-metadata').hidden = !item.independent || item.pending_review;
  $$('.rating button').forEach(button =>
    button.classList.toggle('active', button.dataset.rating === item.rating));
  setStatus(`${stage} (read-only)`);
  canvas.style.cursor = 'grab';
  renderVariantControls();
  const previewUrl = variantCollection.outlines.find(entry => entry.id === activeVariant)?.preview_url || item.svg_url;
  if (!previewUrl) return render();
  $('#loading').textContent = 'Loading published silhouette…';
  $('#loading').classList.add('visible');
  try {
    sourceImage = await loadPublishedSvg(previewUrl);
    fitView();
  } catch (error) {
    sourceImage = null;
    render();
    setStatus(error.message, true);
  } finally {
    $('#loading').classList.remove('visible');
  }
}

async function applyFilters(urlMode = 'push', preferredItemId, requirePreferred = false) {
  if (editorBusy || remaskBusy || variantBusy) return;
  rebuildNavigation();
  const visible = visibleItems();
  refreshProgressText();
  const preferred = preferredItemId === undefined
    ? visible.find(item => item.id === current?.id)
    : visible.find(item => item.id === preferredItemId);
  if (requirePreferred && !preferred) {
    await releaseCurrentClaim();
    showEmptyState('Product not found', 'The linked product no longer exists.');
    return updateEditorUrl(urlMode, preferredItemId);
  }
  if (!visible.length) {
    await releaseCurrentClaim();
    showEmptyState();
    return updateEditorUrl(urlMode, null);
  }
  $('#empty-state').classList.remove('visible');
  const target = preferred || visible[0];
  if (!current || target.id !== current.id) {
    await loadItem(target.id, urlMode);
  } else {
    updateEditorUrl(urlMode, current.id);
  }
}

function configureOffscreen(width, height) {
  for (const target of [rembgCanvas, editsCanvas, initialEditsCanvas, maskCanvas, overlayCanvas, edgeCanvas]) {
    target.width = width;
    target.height = height;
  }
}

async function showDraftOutline() {
  const session = editSession, variant = activeVariant;
  const entry = session.outline(variant);
  const images = await session.images(variant);
  if (editSession !== session || activeVariant !== variant) return;
  state = structuredClone(entry.state); sourceImage = images?.source || null; rembgImage = images?.mask || null;
  savedOutlineMode = entry.mode === 'svg';
  strokes = structuredClone(entry.strokes || []); activeStroke = activeCrop = activeLength = null;
  editsDirty = false;
  setItemTitle(current); setSidebarMode('edit'); setEditorDisabled(!images);
  $('#published-state').classList.remove('visible');
  $('#source-unavailable').classList.toggle('visible', !!session.document.source_unavailable && !images);
  $('#source-unavailable-message').textContent = session.document.certificate_error
    ? "The source site's certificate couldn't be verified." : 'The image could not be retrieved from the source site.';
  $('#load-insecure').hidden = !session.document.certificate_error || !!images;
  $('#variant-blank').hidden = !!images || !!session.document.source_unavailable;
  $('#cancel-revision').hidden = false; $('#cancel-revision').disabled = false;
  $('#download-current').hidden = false;
  $('#save-next').disabled = false; $('#download-current').disabled = false;
  $('#revision-note').hidden = !current.revision;
  $('#revision-metadata').hidden = !current.independent;
  $('#rereview').hidden = true; $('#edit-metadata').hidden = true;
  $('#save-label').textContent = hostedMode ? 'Submit' : 'Save';
  $('#save-next').setAttribute('aria-label', hostedMode ? 'Submit product' : 'Save product');
  $('#save-next').title = `${$('#save-next').getAttribute('aria-label')} (Space)`;
  $('#use-saved-photo').hidden = !entry.reference_url;
  $('#use-saved-photo').dataset.url = entry.reference_url || '';
  $('#reset-catalog').hidden = entry.source !== 'alternative' || current.independent;
  $('#threshold').value = state.alpha_threshold || 128;
  $('#threshold-value').textContent = $('#threshold').value;
  $('#threshold').disabled = savedOutlineMode || !images;
  $('[data-tool=remask]').disabled = savedOutlineMode || !images;
  $('#source-info').textContent = savedOutlineMode
    ? 'Editing saved outline. Paste or drop a photo to use the image tools.'
    : entry.source === 'alternative' && images ? 'Alternative image active. Changes stay here until Save.'
    : 'Paste or drop an image onto the canvas. Changes stay here until Save.';
  $('#view-hint').textContent = images ? 'Wheel zoom · middle drag pans' : 'Paste or drop an image to begin';
  $$('.rating button').forEach(button => { button.disabled = false; button.classList.toggle('active', button.dataset.rating === state.rating); });
  if (images) {
    // Canvas sources from local processing use the same dimensions as image elements.
    if (!sourceImage.naturalWidth) { sourceImage.naturalWidth = sourceImage.width; sourceImage.naturalHeight = sourceImage.height; }
    configureOffscreen(entry.width, entry.height);
    rembgCanvas.getContext('2d').drawImage(rembgImage, 0, 0);
    if (entry.paint) editsCanvas.getContext('2d').drawImage(entry.paint, 0, 0);
    if (entry.initialPaint) initialEditsCanvas.getContext('2d').drawImage(entry.initialPaint, 0, 0);
    recomputeMask(); fitView(); setTool(tool);
  } else render();
  variantCollection = session.collection(); renderVariantControls(); updateLineInfo();
}

async function loadItem(itemId, urlMode = 'replace', allowInvalidCertificate = false, edit = false) {
  if (editorBusy) return;
  editorBusy = true;
  try {
    const generation = ++loadGeneration;
    if (editSession) await releaseCurrentClaim();
    const listed = items.find(item => item.id === itemId);
    if (!listed) return showEmptyState();
    if (current?.id !== itemId) {
      const linked = new URLSearchParams(location.search);
      activeVariant = linked.get('item') === itemId ? linked.get('variant') || '' : '';
    }
    $('#empty-state').classList.remove('visible');
    if (listed.read_only && !edit) {
      await refreshVariants(itemId); await showPublishedItem(listed); updateEditorUrl(urlMode, itemId); return;
    }
    $('#loading').textContent = 'Opening product…'; $('#loading').classList.add('visible');
    try {
      const query = allowInvalidCertificate ? '?allow_invalid_certificate=true' : '';
      const descriptor = await api(`/api/editor/items/${encodeURIComponent(itemId)}/open${query}`, {method:'POST'});
      if (generation !== loadGeneration) return;
      editSession = new EditorSession(descriptor);
      current = {...listed, read_only:false, revision:descriptor.revision, metadata:descriptor.metadata || listed.metadata};
      if (outlineNumberItem !== itemId) { outlineNumberItem = itemId; outlineNumbers.clear(); nextOutlineNumber = 1; }
      if (!descriptor.outlines.some(entry => entry.id === activeVariant)) activeVariant = descriptor.outlines[0].id;
      metadataDirty = editsDirty = false;
      await showDraftOutline();
      if (hostedMode) startClaimHeartbeat(itemId);
      setStatus(hostedMode ? 'Claimed for up to 15 minutes · Changes stay in this browser until Save' : 'Changes stay in this browser until Save');
      updateEditorUrl(urlMode, itemId);
    } catch (error) {
      editSession = null; showEmptyState('Could not open product', error.message);
      if (error.currentItemId) showClaimRecovery(error, () => loadItem(itemId, urlMode, allowInvalidCertificate, edit));
      setStatus(error.message, true);
    } finally { $('#loading').classList.remove('visible'); }
  } finally { editorBusy = false; }
}

async function uploadAlternative(file, source = 'alternative', ask = true) {
  if (!current || !file || editorBusy || remaskBusy) return;
  if (current.read_only) { await rereview(); if (!editSession) return; }
  if (ask && (state?.rating || state?.main_length || editsDirty) &&
      !confirm('Replace this source image and clear its current mask edits, rating, and length line?')) return;
  captureCurrentOutline();
  const session = editSession, variant = activeVariant;
  editorBusy = true; setEditorDisabled(true);
  $('#loading').textContent = 'Processing image…'; $('#loading').classList.add('visible');
  try {
    const replacement = await session.replacement(file, source);
    if (editSession !== session || activeVariant !== variant) return;
    Object.assign(session.outline(variant), replacement);
    await showDraftOutline(); metadataDirty = true;
    setStatus('Image ready · Unsaved changes');
  } catch (error) { setStatus(error.message, true); }
  finally { editorBusy = false; $('#loading').classList.remove('visible'); if (editSession === session) await showDraftOutline(); }
}

async function resetToCatalog() {
  if ($('#reset-catalog').hidden || editorBusy) return;
  if (!confirm('Reset to the catalog image and clear the current mask edits, rating, and length line?')) return;
  const session = editSession, variant = activeVariant;
  try {
    const url = `/api/editor/items/${encodeURIComponent(current.id)}/asset/catalog?baseline=${session.document.baseline}`;
    const response = await fetch(url);
    if (!response.ok) throw await responseError(response);
    const blob = await response.blob();
    if (editSession === session && activeVariant === variant) await uploadAlternative(blob, 'catalog', false);
  } catch (error) { setStatus(error.message, true); }
}

async function rereview() {
  if (!current?.can_edit || editorBusy || remaskBusy) return;
  if (editSession) {
    const session = editSession;
    editorBusy = true;
    try {
      const descriptor = await api(`/api/editor/items/${encodeURIComponent(current.id)}/open`, {method:'POST'});
      if (descriptor.baseline !== session.document.baseline) {
        await fetch(`/api/editor/items/${encodeURIComponent(current.id)}/release`, {method:'POST'});
        throw new Error('The saved product changed. Reload to open the new result. Your working copy remains here.');
      }
      current.read_only = false;
      await showDraftOutline(); startClaimHeartbeat(current.id);
      setStatus('Claim restored · Your working copy was retained');
    } catch (error) { setStatus(error.message, true); }
    finally { editorBusy = false; }
    return;
  }
  await loadItem(current.edit_item_id || current.id, 'replace', false, true);
}

async function cancelRevision() {
  if (!editSession || editorBusy || !confirm('Discard this working copy and return to the saved result?')) return;
  const itemId = current.id;
  await releaseCurrentClaim();
  if (items.find(item => item.id === itemId)?.read_only) await loadItem(itemId);
  else { showEmptyState('Editing cancelled', 'Select a product to start another working copy.'); updateEditorUrl('replace', null); }
  setStatus('Working copy discarded; saved result retained');
}

async function editIndependentMetadata() {
  if (!current?.independent || editorBusy) return;
  if (!editSession) { await rereview(); if (!editSession) return; }
  captureCurrentOutline();
  const session = editSession;
  const metadata = await MetadataDialog.open(session.metadata, 'Apply to working copy');
  if (!metadata || editSession !== session) return;
  const sizes = new Set(metadata.sizes.map(size => size.label));
  if (session.outlines.some(outline => outline.sizes.some(size => !sizes.has(size)))) return setStatus('Reassign outline sizes before removing them from the product.', true);
  session.metadata = metadata; current.metadata = metadata; metadataDirty = true;
  variantCollection = session.collection(); renderVariantControls();
  setStatus('Metadata updated locally · Unsaved changes');
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
    ctx.globalCompositeOperation = 'source-atop';
    ctx.drawImage(overlayCanvas, 0, 0);
    ctx.globalCompositeOperation = 'source-over';
    ctx.globalAlpha = 1;
    ctx.drawImage(edgeCanvas, 0, 0);
  } else if (viewMode === 'cutout') {
    ctx.globalCompositeOperation = 'destination-in';
    ctx.drawImage(maskCanvas, 0, 0);
    ctx.globalCompositeOperation = 'source-over';
  }
  // Display-only matte: keep black cutouts visible without flattening their alpha.
  ctx.globalCompositeOperation = 'destination-over';
  ctx.fillStyle = '#ecece8';
  ctx.fillRect(0, 0, sourceImage.naturalWidth, sourceImage.naturalHeight);
  ctx.globalCompositeOperation = 'source-over';
  if (activeCrop) {
    const left = Math.min(activeCrop.start[0], activeCrop.end[0]);
    const top = Math.min(activeCrop.start[1], activeCrop.end[1]);
    const width = Math.abs(activeCrop.end[0] - activeCrop.start[0]);
    const height = Math.abs(activeCrop.end[1] - activeCrop.start[1]);
    ctx.save();
    ctx.fillStyle = '#22d3ee33';
    ctx.strokeStyle = '#22d3ee';
    ctx.lineWidth = 2 / view.zoom;
    ctx.setLineDash([8 / view.zoom, 5 / view.zoom]);
    ctx.fillRect(left, top, width, height);
    ctx.strokeRect(left, top, width, height);
    ctx.restore();
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

function paintRemaskCrop(stroke) {
  const target = editsCanvas.getContext('2d');
  const edits = target.getImageData(stroke.left, stroke.top, stroke.width, stroke.height);
  for (let pixel = 0; pixel < stroke.mask.length; pixel++) {
    if (!stroke.mask[pixel]) continue;
    const index = pixel * 4;
    edits.data[index] = edits.data[index + 1] = edits.data[index + 2] = 255;
    edits.data[index + 3] = 255;
  }
  target.putImageData(edits, stroke.left, stroke.top);
}

function drawStroke(stroke, live = true) {
  if (stroke.mask) {
    paintRemaskCrop(stroke);
    if (live) recomputeMask();
    return;
  }
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
      tool === 'length' || tool === 'remask' ? 'crosshair' : hoverPoint ? 'none' : 'default';
}

function setTool(nextTool) {
  if (savedOutlineMode && nextTool === 'remask') nextTool = 'add';
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

  setStatus('Unsaved changes');
  refreshProgressText();
}

function refreshProgressText() {
  $('#progress').textContent = `${visibleItems().length} matching filters`;
}

function canvasBlob(target) {
  return new Promise(resolve => target.toBlob(resolve, 'image/png'));
}

async function remaskCrop(box) {
  if (remaskBusy || !current) return;
  const itemId = current.id;
  remaskBusy = true;
  setStatus('Remasking crop…');
  const form = new FormData();
  const session = editSession, variant = activeVariant;
  const crop = offscreen(); crop.width = box.right - box.left; crop.height = box.bottom - box.top;
  const context = crop.getContext('2d'); context.fillStyle = '#ecece8'; context.fillRect(0, 0, crop.width, crop.height);
  context.drawImage(sourceImage, box.left, box.top, crop.width, crop.height, 0, 0, crop.width, crop.height);
  form.append('image', await canvasBlob(crop), 'crop.png');
  try {
    const response = await fetch(
      '/api/editor/process',
      {method: 'POST', body: form},
    );
    if (!response.ok) throw await responseError(response);
    const prediction = await createImageBitmap(await response.blob());
    if (current?.id !== itemId || editSession !== session || activeVariant !== variant) return prediction.close();
    const width = box.right - box.left;
    const height = box.bottom - box.top;
    if (prediction.width !== width || prediction.height !== height) {
      prediction.close();
      throw new Error('Remasked crop dimensions do not match');
    }
    const cropped = offscreen();
    cropped.width = width;
    cropped.height = height;
    const croppedContext = cropped.getContext('2d');
    croppedContext.drawImage(prediction, 0, 0);
    prediction.close();
    const alpha = croppedContext.getImageData(0, 0, width, height).data;
    const threshold = Number($('#threshold').value);
    const mask = new Uint8Array(width * height);
    for (let pixel = 0; pixel < mask.length; pixel++) {
      mask[pixel] = Number(alpha[pixel * 4 + 3] >= threshold);
    }
    if (!mask.some(Boolean)) return setStatus('rembg found no foreground in that crop', true);
    const stroke = {left: box.left, top: box.top, width, height, mask};
    strokes.push(stroke);
    drawStroke(stroke);
    render();
    markDirty(true);
    setStatus('Crop mask added');
  } catch (error) {
    setStatus(error.message, true);
  } finally {
    remaskBusy = false;
  }
}

async function performSave() {
  if (!current || !editSession || editorBusy || remaskBusy) return;
  const itemId = current.id, session = editSession;
  captureCurrentOutline(); editorBusy = true; setEditorDisabled(true);
  try {
    const form = await session.body();
    const result = await api(`/api/editor/items/${encodeURIComponent(itemId)}/commit`, {method:'POST', body:form});
    Object.assign(items.find(item => item.id === itemId), result);
    editsDirty = metadataDirty = false; editSession = null;
    clearInterval(claimHeartbeat); refreshProgressText();
    setStatus(hostedMode ? 'Submitted for review' : 'Saved and exported');
    window.refreshModerationCount?.(); return result;
  } finally { editorBusy = false; if (editSession === session) await showDraftOutline(); }
}

function save() {
  const operation = saveChain.then(() => performSave());
  saveChain = operation.catch(() => {});
  return operation.catch(error => { setStatus(error.message, true); throw error; });
}

async function navigate(direction) {
  if (!current) return;
  if (editorBusy || remaskBusy || variantBusy) return;
  const visible = visibleItems();
  if (!visible.length) return;
  let index = visible.findIndex(item => item.id === current.id);
  if (index < 0) index = direction > 0 ? -1 : 0;
  index = (index + direction + visible.length) % visible.length;
  await loadItem(visible[index].id, 'push');
}

async function saveAndNext() {
  if (editorBusy || remaskBusy || variantBusy) return;
  const revising = current?.revision;
  const itemId = current?.id;
  if (!state?.rating) return setStatus('Choose a rating first', true);
  if (state.rating !== 'unusable' && !state.main_length) {
    return setStatus('Usable items require a base-to-tip line', true);
  }
  const scalingWarning = state.rating !== 'unusable' && currentScalingWarning();
  if (scalingWarning && !confirm(
    `${scalingWarning.label}\n${describeScalingStep(scalingWarning)}.\n\n` +
    `Every size will use this one outline. ${hostedMode ? 'Submit' : 'Save'} anyway?`,
  )) return setStatus('Not saved · Use Variants to add outlines for other sizes');
  const orderedIds = navigationIds || items.map(item => item.id);
  const oldIndex = orderedIds.indexOf(current.id);
  try {
    await save();
    clearInterval(claimHeartbeat);
    if (revising) {
      await loadItem(itemId);
      return setStatus(hostedMode ? 'Revision saved for moderation' : 'Revision saved');
    }
  } catch (_) { return; }
  const candidates = visibleItems();
  if (!candidates.length) {
    showEmptyState();
    updateEditorUrl('push', null);
    return setStatus(hostedMode ? 'Submitted for review' : 'All matching products reviewed');
  }
  const candidateIds = new Set(candidates.map(item => item.id));
  const followingIds = oldIndex < 0
    ? orderedIds
    : orderedIds.slice(oldIndex + 1).concat(orderedIds.slice(0, oldIndex));
  const nextId = followingIds.find(itemId => candidateIds.has(itemId));
  if (nextId) await loadItem(nextId, 'push');
}

async function downloadCurrent() {
  if (!editSession || editorBusy || remaskBusy) return;
  captureCurrentOutline(); const itemId = current.id;
  editorBusy = true; $('#download-current').disabled = true;
  try {
    const form = await editSession.body();
    const response = await fetch(`/api/editor/items/${encodeURIComponent(itemId)}/commit?download_only=true`, {method:'POST', body:form});
    if (!response.ok) throw await responseError(response);
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement('a'); link.href = url; link.download = `${itemId}.zip`; link.click();
    URL.revokeObjectURL(url); setStatus('Archive downloaded; changes remain local until Save.');
  } catch (error) { setStatus(error.message, true); }
  finally { editorBusy = false; $('#download-current').disabled = false; }
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
  if (editorBusy || remaskBusy || !sourceImage) return;
  canvas.setPointerCapture(event.pointerId);
  if (current?.read_only || event.button === 1) {
    panning = { x: event.clientX, y: event.clientY, ox: view.x, oy: view.y };
    canvas.style.cursor = 'grabbing';
    return;
  }
  const point = imagePoint(event);
  if (tool === 'remask') {
    if (!inside(point) || remaskBusy) return;
    activeCrop = {start: point, end: point};
    render();
    return;
  }
  if (tool === 'length') {
    if (!inside(point)) return;
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
  hoverPoint = sourceImage ? pointer : null;
  updateCanvasCursor();
  if (panning) {
    view.x = panning.ox + event.clientX - panning.x;
    view.y = panning.oy + event.clientY - panning.y;
    render();
    return;
  }
  if (activeCrop) {
    activeCrop.end = [
      Math.max(0, Math.min(sourceImage.naturalWidth, pointer[0])),
      Math.max(0, Math.min(sourceImage.naturalHeight, pointer[1])),
    ];
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
  if (activeCrop) {
    const box = {
      left: Math.max(0, Math.floor(Math.min(activeCrop.start[0], activeCrop.end[0]))),
      top: Math.max(0, Math.floor(Math.min(activeCrop.start[1], activeCrop.end[1]))),
      right: Math.min(sourceImage.naturalWidth, Math.ceil(Math.max(activeCrop.start[0], activeCrop.end[0]))),
      bottom: Math.min(sourceImage.naturalHeight, Math.ceil(Math.max(activeCrop.start[1], activeCrop.end[1]))),
    };
    activeCrop = null;
    render();
    if (!cancelled && box.right - box.left >= 4 && box.bottom - box.top >= 4) {
      remaskCrop(box);
    }
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
  if (!state || current?.read_only || editorBusy) return;
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
function searchImages() {
  const product = current?.products?.[0];
  if (!product) return setStatus('No catalog product is selected', true);
  const query = `${product.n} ${product.vn}`.trim();
  window.open(`https://www.google.com/search?tbm=isch&q=${encodeURIComponent(query)}`, '_blank', 'noopener');
}
$('#search-images').addEventListener('click', searchImages);
$('#unavailable-search-images').addEventListener('click', searchImages);
$('#open-product').addEventListener('click', event => {
  window.open(event.currentTarget.dataset.url, '_blank', 'noopener');
});
$('#load-insecure').addEventListener('click', async () => {
  if (!current) return;
  await loadItem(current.id, 'none', true, true);
});
$('#reset-catalog').addEventListener('click', resetToCatalog);
$('#rereview').addEventListener('click', rereview);
$('#edit-metadata').addEventListener('click', editIndependentMetadata);
$('#revision-metadata').addEventListener('click', editIndependentMetadata);
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
$('#cancel-revision').addEventListener('click', () => cancelRevision().catch(error => setStatus(error.message, true)));
$('#use-saved-photo').addEventListener('click', async () => {
  try {
    const response = await fetch($('#use-saved-photo').dataset.url);
    if (!response.ok) throw await responseError(response);
    await uploadAlternative(new File([await response.blob()], 'saved-photo.png', {type:'image/png'}));
  } catch (error) { setStatus(error.message, true); }
});
$('#filter').addEventListener('change', () => applyFilters());
$('#type-filter').addEventListener('change', () => applyFilters());
$('#measurements-filter').addEventListener('change', () => applyFilters());
$('#scaling-filter').addEventListener('change', () => applyFilters());
$('#outlines-filter').addEventListener('change', () => applyFilters());
$('#navigation-order').addEventListener('change', () => {
  prioritizeLeastRecent = $('#navigation-order').value === 'least-recent';
  try {
    localStorage.setItem(
      'silicone-shadows-queue-order',
      prioritizeLeastRecent ? 'least-recent' : 'catalog',
    );
  } catch (_) {}
  applyFilters();
});
function setFiltersOpen(open) {
  $('#filters-panel').hidden = !open;
  $('#filters-toggle').setAttribute('aria-expanded', String(open));
}
$('#filters-toggle').addEventListener('click', () =>
  setFiltersOpen($('#filters-panel').hidden));
document.addEventListener('click', event => {
  if (!$('#catalog-filters').contains(event.target)) setFiltersOpen(false);
});
document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && !$('#filters-panel').hidden) {
    setFiltersOpen(false);
    $('#filters-toggle').focus();
  }
});
let filterTimer = null;
let textFilterHistoryMode = 'replace';
for (const input of $$('#catalog-filters input[type="search"]')) {
  input.addEventListener('focus', () => { textFilterHistoryMode = 'push'; });
  input.addEventListener('input', () => {
    clearTimeout(filterTimer);
    filterTimer = setTimeout(() => {
      const mode = textFilterHistoryMode;
      textFilterHistoryMode = 'replace';
      applyFilters(mode);
    }, 180);
  });
}
$('#clear-filters').addEventListener('click', () => {
  $('#filter').value = 'all';
  $('#name-filter').value = $('#vendor-filter').value = $('#type-filter').value = '';
  $('#measurements-filter').value = 'all';
  $('#scaling-filter').value = $('#outlines-filter').value = 'all';
  applyFilters();
});
$('#show-all').addEventListener('click', () => {
  $('#filter').value = 'all';
  $('#name-filter').value = $('#vendor-filter').value = $('#type-filter').value = '';
  $('#measurements-filter').value = 'all';
  $('#scaling-filter').value = $('#outlines-filter').value = 'all';
  applyFilters();
});
window.beforeAppLogout = releaseCurrentClaim;
window.beforeCatalogUpdate = async () => {
  if (remaskBusy || activeStroke || activeLength || activeCrop ||
      editSession || editorBusy) {
    throw new Error('Finish your current review before updating the catalog.');
  }
  await saveChain;
};

window.addEventListener('keydown', event => {
  if ($('#variant-dialog').open || $('#variants-panel').open) return;
  const toolShortcut = {a: 'add', e: 'erase', r: 'remask', w: 'length'}[event.key.toLowerCase()];
  if (toolShortcut && !event.ctrlKey && !event.metaKey && !event.altKey &&
      !event.target.matches('input:not([type="range"]), select, textarea, [contenteditable]')) {
    setTool(toolShortcut);
    event.preventDefault();
    return;
  }
  if (event.target.matches('input, select')) return;
  if (event.ctrlKey && event.key.toLowerCase() === 'z') { $('#undo').click(); event.preventDefault(); return; }
  if (event.code === 'Space') {
    if (!event.repeat) current?.read_only ? navigate(1) : saveAndNext();
    event.preventDefault();
    return;
  }
  if (event.key === '1') $$('.rating button')[0].click();
  else if (event.key === '2') $$('.rating button')[1].click();
  else if (event.key === '3') $$('.rating button')[2].click();
  else if (event.key === 'ArrowLeft') navigate(-1);
  else if (event.key === 'ArrowRight') navigate(1);
});
window.addEventListener('copy', event => {
  if (event.defaultPrevented || !event.clipboardData ||
      event.target.closest('input, textarea, select, [contenteditable], dialog') ||
      document.querySelector('dialog[open]') || !window.getSelection().isCollapsed) return;
  const url = current && $('#open-product').dataset.url;
  if (!url) return;
  event.clipboardData.setData('text/plain', url);
  event.preventDefault();
  setStatus('Product link copied.');
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
let historyNavigation = Promise.resolve();
let historyNavigationVersion = 0;
window.addEventListener('popstate', () => {
  const version = ++historyNavigationVersion;
  const urlState = parseEditorUrl(location.search);
  historyNavigation = historyNavigation.then(async () => {
    restoreEditorControls(urlState);
    activeVariant = new URLSearchParams(location.search).get('variant') || '';
    await applyFilters('none', urlState.item, urlState.directItem);
    if (version === historyNavigationVersion && !urlState.directItem) {
      updateEditorUrl('replace', current?.id);
    }
  }).catch(error => setStatus(error.message, true));
});
window.addEventListener('beforeunload', event => {
  if (editSession && (metadataDirty || editsDirty)) { event.preventDefault(); event.returnValue = ''; }
});
window.addEventListener('pagehide', () => {
  if (hostedMode && current && !current.read_only) {
    fetch(`/api/editor/items/${encodeURIComponent(current.id)}/release`, {
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

setEditorDisabled(true);
(async () => {
  try {
    const initialUrlState = parseEditorUrl(location.search);
    const session = await window.appNavigationReady;
    hostedMode = session.hosted;
    if (hostedMode) {
      if (!initialUrlState.order) {
        try {
          initialUrlState.order = localStorage.getItem('silicone-shadows-queue-order') === 'catalog'
            ? 'catalog' : 'least-recent';
        } catch (_) { initialUrlState.order = 'least-recent'; }
      }
      $('#navigation-order-field').hidden = false;
      $('#save-label').textContent = 'Submit';
      $('#source-info').textContent = 'Moving to another product discards unfinished work.';
    }
    const options = await api('/api/public/metadata-options');
    for (const type of options.product_types) {
      const option = document.createElement('option');
      option.value = option.textContent = type;
      $('#type-filter').append(option);
    }
    restoreEditorControls(initialUrlState);
    await refreshItems();
    const linked = new URLSearchParams(location.search);
    activeVariant = linked.get('variant') || '';
    const linkedEdit = linked.get('edit') === '1';
    await applyFilters('replace', initialUrlState.item, initialUrlState.directItem);
    if (linkedEdit && current?.can_edit && current.read_only) await rereview();
  } catch (error) {
    setStatus(error.message, true);
  }
})();
