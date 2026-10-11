/* The browser owns unfinished product edits. Only submit/export serializes them. */
class EditorSession {
  constructor(document) {
    this.document = document;
    this.outlines = document.outlines.map(outline => ({...outline, state: {status:'pending', rating:null, alpha_threshold:128, main_length:null, ...structuredClone(outline.state)}, shape_changed: false, strokes: []}));
    this.metadata = document.metadata;
  }

  collection() {
    return {
      outlines: this.outlines.map(outline => ({id: outline.id, sizes: outline.sizes, label: outline.id ? outline.sizes.join(' + ') : 'General'})),
      sizes: this.metadata ? this.metadata.sizes.map(size => size.label) : this.document.sizes,
      size_labels: this.document.size_labels,
      general: this.outlines.some(outline => outline.id === ''),
    };
  }

  outline(id) {
    const outline = this.outlines.find(entry => entry.id === id);
    if (!outline) throw new Error('Outline not found.');
    return outline;
  }

  changeVariant(change) {
    const existing = this.outlines.find(outline => outline.id === change.id);
    if (change.action === 'remove') {
      if (this.outlines.length <= 1) throw new Error('Keep at least one outline.');
      this.outline(change.id);
      this.outlines = this.outlines.filter(outline => outline.id !== change.id);
      return;
    }
    const general = change.action === 'general';
    const id = general ? '' : change.id;
    if ((change.action === 'add' || general) && this.outlines.some(outline => outline.id === id)) throw new Error('That outline already exists.');
    if (change.action === 'assign' && !existing) throw new Error('Outline not found.');
    const sizes = general ? [] : change.sizes;
    if (!general && (!sizes?.length || new Set(sizes).size !== sizes.length)) throw new Error('Select at least one distinct size.');
    const known = new Set(this.collection().sizes);
    if (sizes.some(size => !known.has(size))) throw new Error('Unknown size.');
    const remaining = this.outlines.filter(outline => outline.id !== id && outline.id !== change.move_from);
    if (sizes.some(size => remaining.some(outline => outline.sizes.includes(size)))) throw new Error('That size already belongs to another outline.');
    let entry;
    if (change.move_from != null) entry = {...this.outline(change.move_from), id, sizes};
    else entry = existing ? {...existing, sizes} : {id, original_id: null, sizes, mode: 'blank', source: 'alternative', state: {status:'pending', rating:null, alpha_threshold:128, main_length:null}, strokes:[], shape_changed:false};
    this.outlines = [...remaining, entry];
  }

  capture(id, editing) {
    const entry = this.outline(id);
    if (!editing.state) return;
    entry.state = structuredClone(editing.state);
    entry.paint = EditorSession.copyCanvas(editing.paint);
    entry.initialPaint = EditorSession.copyCanvas(editing.initialPaint);
    entry.strokes = structuredClone(editing.strokes);
    entry.shape_changed ||= editing.editsDirty;
  }

  static copyCanvas(source) {
    const canvas = document.createElement('canvas');
    canvas.width = source.width; canvas.height = source.height;
    canvas.getContext('2d').drawImage(source, 0, 0);
    return canvas;
  }

  static image(url) {
    return new Promise((resolve, reject) => {
      const image = new Image();
      image.onload = () => resolve(image);
      image.onerror = () => reject(new Error('Could not load the editing image.'));
      image.src = url;
    });
  }

  static async blobImage(blob) {
    const url = URL.createObjectURL(blob);
    try { return await EditorSession.image(url); }
    finally { URL.revokeObjectURL(url); }
  }

  async images(id) {
    const entry = this.outline(id);
    if (entry.mode === 'blank') return null;
    if (entry.images) return entry.images;
    let source, mask;
    if (entry.mode === 'svg') {
      mask = await EditorSession.blobImage(new Blob([entry.mask_svg], {type:'image/svg+xml'}));
      source = document.createElement('canvas'); source.width = entry.width; source.height = entry.height;
      const context = source.getContext('2d');
      context.fillStyle = '#ecece8'; context.fillRect(0, 0, source.width, source.height);
      const silhouette = document.createElement('canvas'); silhouette.width = entry.width; silhouette.height = entry.height;
      const fill = silhouette.getContext('2d'); fill.drawImage(mask, 0, 0); fill.globalCompositeOperation = 'source-in';
      fill.fillStyle = '#343b45'; fill.fillRect(0, 0, entry.width, entry.height);
      context.drawImage(silhouette, 0, 0);
    } else {
      [source, mask] = await Promise.all([EditorSession.image(entry.source_url), EditorSession.image(entry.mask_url)]);
    }
    entry.images = {source, mask};
    return entry.images;
  }

  async replacement(file, source = 'alternative') {
    const image = await EditorSession.blobImage(file);
    const form = new FormData(); form.append('image', file, file.name || 'image.png');
    const response = await fetch('/api/editor/process', {method:'POST', body:form});
    if (!response.ok) throw await responseError(response);
    const mask = await EditorSession.blobImage(await response.blob());
    const width = mask.naturalWidth, height = mask.naturalHeight;
    const normalized = document.createElement('canvas'); normalized.width = width; normalized.height = height;
    normalized.getContext('2d').drawImage(image, 0, 0, width, height);
    return {mode:'photo', source, width, height, images:{source:normalized, mask},
      state:{status:'pending',rating:null,alpha_threshold:128,main_length:null},
      paint:null, strokes:[], shape_changed:false, reference_url:null};
  }

  async body() {
    const form = new FormData();
    const outlines = [];
    for (const entry of this.outlines) {
      const {id, original_id, sizes, state, mode, shape_changed, source} = entry;
      if (!state.rating) throw new Error(`Choose an image quality for ${outlineLabel(id)}.`);
      if (state.rating !== 'unusable' && !state.main_length) throw new Error(`Add a usable-length line for ${outlineLabel(id)}.`);
      outlines.push({id, original_id, sizes, state, mode, shape_changed, source});
      if (mode === 'photo' || (mode === 'svg' && shape_changed)) {
        const images = await this.images(id);
        const mask = document.createElement('canvas'); mask.width = entry.width; mask.height = entry.height;
        mask.getContext('2d').drawImage(images.mask, 0, 0);
        form.append(`mask:${id}`, await canvasBlob(mask), 'mask.png');
        if (mode === 'photo') {
          const sourceCanvas = EditorSession.copyCanvas(images.source);
          form.append(`source:${id}`, await canvasBlob(sourceCanvas), 'source.png');
        }
        if (entry.paint) form.append(`edits:${id}`, await canvasBlob(entry.paint), 'edits.png');
      }
    }
    form.append('document', JSON.stringify({baseline:this.document.baseline, outlines, metadata:this.metadata}));
    return form;
  }
}
if (typeof module !== 'undefined') module.exports = {EditorSession};
