const assert = require('node:assert/strict');
const { parseEditorUrl, buildEditorUrl } = require('../static/review-url.js');

const view = parseEditorUrl(
  '?state=pending_review&name=Echo+2&vendor=Acme+-Labs&type=Dildo&measurements=all&order=catalog&item=a%2Fb',
);
assert.deepEqual(view, {
  state: 'pending_review',
  name: 'Echo 2',
  vendor: 'Acme -Labs',
  type: 'Dildo',
  measurements: 'all',
  scaling: 'all',
  order: 'catalog',
  item: 'a/b',
  directItem: false,
});
assert.equal(
  buildEditorUrl('/editor', view),
  '/editor?state=pending_review&name=Echo+2&vendor=Acme+-Labs&type=Dildo&measurements=all&item=a%2Fb',
);
assert.deepEqual(parseEditorUrl('?state=nope&measurements=random&order=random'), {
  state: 'available', name: '', vendor: '', type: '', measurements: 'measured', scaling: 'all', order: null, item: '',
  directItem: false,
});
assert.equal(
  buildEditorUrl('/editor', {
    state: 'available', name: '', vendor: '', type: '', order: 'least-recent', item: '',
  }),
  '/editor',
);
assert.deepEqual(parseEditorUrl('?item=a%2Fb'), {
  state: 'all', name: '', vendor: '', type: '', measurements: 'measured', scaling: 'all', order: null, item: 'a/b',
  directItem: true,
});
assert.equal(parseEditorUrl('?item=a%2Fb&measurements=all').directItem, true);
assert.equal(parseEditorUrl('?measurements=all').state, 'all');
assert.equal(
  buildEditorUrl('/editor', {
    state: 'all', name: '', vendor: '', type: '', measurements: 'all', order: null, item: '',
  }),
  '/editor?measurements=all',
);
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

assert.equal(parseEditorUrl('?scaling=likely').scaling, 'likely');
assert.equal(parseEditorUrl('?scaling=likely').state, 'all');
assert.equal(parseEditorUrl('?scaling=bogus').scaling, 'all');
assert.equal(
  buildEditorUrl('/editor', {
    state: 'in_catalog', name: '', vendor: '', type: '', scaling: 'possible', order: null, item: '',
  }),
  '/editor?state=in_catalog&scaling=possible',
);
assert.equal(
  buildEditorUrl('/editor', {
    state: 'all', name: '', vendor: '', type: '', scaling: 'likely', order: null, item: '',
  }),
  '/editor?scaling=likely',
);
