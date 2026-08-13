const MetadataDialog = (() => {
  const featureLabels = {
    sc: 'Suction cup',
    ct: 'Cum tube',
    dd: 'Dual density',
    sf: 'Split firmness',
    vlk: 'Vac-U-Lock',
    inf: 'Inflatable',
  };
  let dialog;
  let options;
  let resolveDialog;

  function option(value, label = value) {
    const item = document.createElement('option');
    item.value = value;
    item.textContent = label;
    return item;
  }

  function fillSelect(select, values, selected = '', blank = false) {
    select.replaceChildren();
    if (blank) select.append(option('', '—'));
    for (const value of values) select.append(option(value));
    select.value = selected || '';
  }

  function checkedValues(name) {
    return [...dialog.querySelectorAll(`input[name="${name}"]:checked`)]
      .map(input => input.value);
  }

  function buildChecks(target, name, values, selected, labels = {}) {
    target.replaceChildren();
    for (const value of values) {
      const label = document.createElement('label');
      label.className = 'metadata-check';
      const input = document.createElement('input');
      input.type = 'checkbox';
      input.name = name;
      input.value = value;
      input.checked = selected.includes(value);
      label.append(input, document.createTextNode(labels[value] || value));
      target.append(label);
    }
  }

  function nullableNumber(input) {
    return input.value === '' ? null : Number(input.value);
  }

  function addSize(size = {}) {
    const row = document.createElement('fieldset');
    row.className = 'metadata-size';
    row.innerHTML = `
      <legend>Size</legend>
      <button class="remove-size" type="button" aria-label="Remove size">×</button>
      <label>Size name<select data-field="size_pair" required></select></label>
      <label>Price <span class="optional">optional</span><input data-field="price" type="number" min="0" step="any"></label>
      <label>Usable/main length <span class="optional">optional</span><input data-field="length" type="number" min="0.01" step="any"></label>
      <label>Circumference <span class="optional">optional</span><input data-field="circumference" type="number" min="0.01" step="any"></label>
      <label>Widest circumference <span class="optional">optional</span><input data-field="widest_circumference" type="number" min="0.01" step="any"></label>
      <label>Widest at<select data-field="widest_label"></select></label>
      <label>Dimension unit<select data-field="unit"><option value="in">in</option><option value="cm">cm</option><option value="mm">mm</option></select></label>`;
    const pairSelect = row.querySelector('[data-field="size_pair"]');
    options.size_labels.forEach((pair, index) => {
      pairSelect.append(option(
        String(index),
        pair.label === pair.short_label
          ? pair.label
          : `${pair.label} (${pair.short_label})`,
      ));
    });
    const selectedPair = options.size_labels.findIndex(pair =>
      pair.label === size.label && pair.short_label === size.short_label);
    pairSelect.value = String(Math.max(0, selectedPair));
    fillSelect(
      row.querySelector('[data-field="widest_label"]'),
      options.width_labels,
      size.widest_label,
      true,
    );
    for (const field of ['price', 'length', 'circumference', 'widest_circumference']) {
      row.querySelector(`[data-field="${field}"]`).value = size[field] ?? '';
    }
    row.querySelector('[data-field="unit"]').value = size.unit || 'in';
    row.querySelector('.remove-size').addEventListener('click', () => row.remove());
    dialog.querySelector('#metadata-sizes').append(row);
  }

  function sizeValue(row) {
    const field = name => row.querySelector(`[data-field="${name}"]`);
    const pair = options.size_labels[Number(field('size_pair').value)];
    return {
      label: pair.label,
      short_label: pair.short_label,
      price: nullableNumber(field('price')),
      length: nullableNumber(field('length')),
      circumference: nullableNumber(field('circumference')),
      widest_circumference: nullableNumber(field('widest_circumference')),
      widest_label: field('widest_label').value || null,
      unit: field('unit').value,
    };
  }

  async function ensureDialog() {
    if (dialog) return;
    const response = await fetch('/api/public/metadata-options');
    if (!response.ok) throw new Error('Could not load metadata options');
    options = await response.json();
    dialog = document.createElement('dialog');
    dialog.id = 'metadata-dialog';
    dialog.innerHTML = `
      <form method="dialog">
        <header><div><h2>Product metadata</h2><p>Add the information that should accompany the silhouette.</p></div></header>
        <div class="metadata-fields">
          <label>Vendor<input id="metadata-vendor" required maxlength="200"></label>
          <label>Product name<input id="metadata-name" required maxlength="200"></label>
          <label>Product type<select id="metadata-type" required></select></label>
          <label>Species<select id="metadata-species"></select></label>
          <label class="metadata-wide">Product page <span class="optional">optional</span><input id="metadata-url" type="url" maxlength="2000" placeholder="https://…"></label>
          <details class="metadata-wide metadata-picker"><summary>Features</summary><div id="metadata-features" class="metadata-checks compact"></div></details>
          <details class="metadata-wide metadata-picker"><summary>Tags</summary><input id="metadata-tag-filter" type="search" placeholder="Filter tags…"><div id="metadata-tags" class="metadata-checks"></div></details>
          <fieldset class="metadata-wide"><legend>Sizes</legend><div id="metadata-sizes"></div><button id="metadata-add-size" type="button">Add size</button></fieldset>
          <label class="metadata-wide">Notes <span class="optional">optional</span><textarea id="metadata-notes" maxlength="2000" rows="3"></textarea></label>
        </div>
        <footer><button value="cancel" formnovalidate>Cancel</button><button id="metadata-confirm" value="default">Download archive</button></footer>
      </form>`;
    document.body.append(dialog);
    dialog.querySelector('#metadata-add-size').addEventListener('click', () => addSize());
    dialog.querySelector('#metadata-tag-filter').addEventListener('input', event => {
      const needle = event.target.value.trim().toLowerCase();
      for (const label of dialog.querySelectorAll('#metadata-tags label')) {
        label.hidden = !label.textContent.toLowerCase().includes(needle);
      }
    });
    dialog.addEventListener('close', () => {
      if (!resolveDialog) return;
      const resolve = resolveDialog;
      resolveDialog = null;
      if (dialog.returnValue !== 'default') return resolve(null);
      const initial = dialog.metadataInitial;
      resolve({
        submission_version: 1,
        catalog_id: initial.catalog_id ?? null,
        vendor: dialog.querySelector('#metadata-vendor').value.trim(),
        product_type: dialog.querySelector('#metadata-type').value,
        name: dialog.querySelector('#metadata-name').value.trim(),
        product_url: dialog.querySelector('#metadata-url').value.trim() || null,
        species: dialog.querySelector('#metadata-species').value || null,
        quality: initial.quality || 'good',
        source: initial.source || 'contributor_photo',
        tags: checkedValues('metadata-tag'),
        features: checkedValues('metadata-feature'),
        sizes: [...dialog.querySelectorAll('.metadata-size')].map(sizeValue),
        notes: dialog.querySelector('#metadata-notes').value.trim() || null,
      });
    });
  }

  async function open(initial = {}, confirmLabel = 'Download archive') {
    await ensureDialog();
    dialog.metadataInitial = initial;
    dialog.querySelector('#metadata-vendor').value = initial.vendor || '';
    dialog.querySelector('#metadata-name').value = initial.name || '';
    dialog.querySelector('#metadata-url').value = initial.product_url || '';
    dialog.querySelector('#metadata-notes').value = initial.notes || '';
    dialog.querySelector('#metadata-confirm').textContent = confirmLabel;
    fillSelect(dialog.querySelector('#metadata-type'), options.product_types, initial.product_type);
    fillSelect(dialog.querySelector('#metadata-species'), options.species, initial.species, true);
    buildChecks(
      dialog.querySelector('#metadata-features'),
      'metadata-feature',
      options.features,
      initial.features || [],
      featureLabels,
    );
    buildChecks(
      dialog.querySelector('#metadata-tags'),
      'metadata-tag',
      options.tags,
      initial.tags || [],
    );
    dialog.querySelector('#metadata-tag-filter').value = '';
    dialog.querySelector('#metadata-sizes').replaceChildren();
    for (const size of initial.sizes || []) addSize(size);
    dialog.returnValue = '';
    dialog.showModal();
    return new Promise(resolve => { resolveDialog = resolve; });
  }

  return {open};
})();
