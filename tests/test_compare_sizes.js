const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('static/compare.html', 'utf8');
const code = html.slice(html.indexOf('    // Apparel-style labels'), html.indexOf('    function refreshFilterOptions('));
const context = vm.createContext({});
vm.runInContext(code, context);
const labels = ['M', 'ID-1', 'XL', '3XL', 'S', 'XXS', '5XL', 'L', 'Size 10', 'XS', '3XS', 'XXL', 'Size 2', '4XL'];
assert.deepEqual([...labels].sort(context.compareSizeLabels),
  ['3XS', 'XXS', 'XS', 'S', 'M', 'L', 'XL', 'XXL', '3XL', '4XL', '5XL', 'ID-1', 'Size 2', 'Size 10']);
assert.equal(context.compareSizeLabels('xl', 'XL'), 0);
console.log('Size labels sort by size');
