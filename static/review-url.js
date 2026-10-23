const EDITOR_STATES = new Set([
  'available', 'pending_review', 'in_catalog', 'all',
  'good', 'bad_perspective', 'unusable',
]);
const EDITOR_ORDERS = new Set(['least-recent', 'catalog']);
const EDITOR_SCALING = new Set(['possible', 'likely']);

function parseEditorUrl(search) {
  const params = new URLSearchParams(search);
  const state = params.get('state');
  const order = params.get('order');
  const item = params.get('item') || '';
  const measurements = params.get('measurements') === 'all' ? 'all' : 'measured';
  const scaling = EDITOR_SCALING.has(params.get('scaling')) ? params.get('scaling') : 'all';
  const hasProductFilter = ['name', 'vendor', 'type', 'item']
    .some(name => params.has(name)) || measurements === 'all' || scaling !== 'all';
  return {
    state: EDITOR_STATES.has(state) ? state : hasProductFilter ? 'all' : 'available',
    name: params.get('name') || '',
    vendor: params.get('vendor') || '',
    type: params.get('type') || '',
    measurements,
    scaling,
    order: EDITOR_ORDERS.has(order) ? order : null,
    item,
    directItem: Boolean(item) &&
      !['state', 'name', 'vendor', 'type'].some(name => params.has(name)),
  };
}

function buildEditorUrl(pathname, view) {
  const params = new URLSearchParams();
  const hasProductFilter = ['name', 'vendor', 'type', 'item'].some(name => view[name]) ||
    view.measurements === 'all' || EDITOR_SCALING.has(view.scaling);
  const implicitState = hasProductFilter ? 'all' : 'available';
  if (view.state !== implicitState) params.set('state', view.state);
  for (const name of ['name', 'vendor', 'type']) {
    if (view[name]) params.set(name, view[name]);
  }
  if (view.measurements === 'all') params.set('measurements', 'all');
  if (EDITOR_SCALING.has(view.scaling)) params.set('scaling', view.scaling);
  if (view.state === 'available' && view.order && view.order !== 'least-recent') {
    params.set('order', view.order);
  }
  if (view.item) params.set('item', view.item);
  const query = params.toString();
  return `${pathname}${query ? `?${query}` : ''}`;
}

if (typeof module !== 'undefined') module.exports = { parseEditorUrl, buildEditorUrl };
