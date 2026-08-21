const assert = require('node:assert/strict');
const { parseEditorUrl, buildEditorUrl } = require('../static/review-url.js');

const view = parseEditorUrl(
  '?state=pending_review&name=Echo+2&vendor=Acme+-Labs&type=Dildo&order=catalog&item=a%2Fb',
);
assert.deepEqual(view, {
  state: 'pending_review',
  name: 'Echo 2',
  vendor: 'Acme -Labs',
  type: 'Dildo',
  order: 'catalog',
  item: 'a/b',
  directItem: false,
});
assert.equal(
  buildEditorUrl('/editor', view),
  '/editor?state=pending_review&name=Echo+2&vendor=Acme+-Labs&type=Dildo&item=a%2Fb',
);
assert.deepEqual(parseEditorUrl('?state=nope&order=random'), {
  state: 'available', name: '', vendor: '', type: '', order: null, item: '',
  directItem: false,
});
assert.equal(
  buildEditorUrl('/editor', {
    state: 'available', name: '', vendor: '', type: '', order: 'least-recent', item: '',
  }),
  '/editor',
);
assert.deepEqual(parseEditorUrl('?item=a%2Fb'), {
  state: 'all', name: '', vendor: '', type: '', order: null, item: 'a/b',
  directItem: true,
});
assert.equal(
  buildEditorUrl('/editor', {
    state: 'all', name: '', vendor: '', type: '', order: 'least-recent', item: 'a/b',
  }),
  '/editor?item=a%2Fb',
);
assert.equal(
  buildEditorUrl('/editor', {
    state: 'available', name: '', vendor: '', type: '', order: 'least-recent', item: 'a/b',
  }),
  '/editor?state=available&item=a%2Fb',
);
assert.equal(parseEditorUrl('?vendor=Acme').state, 'all');
assert.equal(
  buildEditorUrl('/editor', {
    state: 'all', name: '', vendor: 'Acme', type: '', order: 'catalog', item: '',
  }),
  '/editor?vendor=Acme',
);
