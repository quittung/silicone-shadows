const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('static/compare.html', 'utf8');
const code = html.slice(html.indexOf('    // Upright every outline'), html.indexOf('    function openAlignDialog('));
const context = vm.createContext({assert});
vm.runInContext(`
const OUTLINE_GAP = .65, scaleLocked = true;
let renders = 0;
const view = {zoom:10, y:0};
function render() { renders++; } function queueWorkspaceSave() {} function fitAll() {}
// Upright rectangles centred on (x, y); length endpoints are offsets from that centre.
function objectBox(o) { return {minX:o.x-o.w/2, maxX:o.x+o.w/2, minY:o.y-o.h/2, maxY:o.y+o.h/2}; }
function worldPoint(o, x, y) { return [o.x + x, o.y + y]; }
const outline = (x, y, w, h, start, end) => ({x, y, w, h, rotation:1, defaultRotation:0,
  product:{}, size:{main_length:{start, end}}});
${code}
const objects = [
  outline(5, 0, 2, 6, [0, 2], [0, -3]),
  outline(0, 4, 3, 10, [0, 3], [0, -5]),
  outline(5.5, -2, 2, 4, [0, 1], [0, -2]),
  outline(20, 9, 1, 3, [0, 1], [0, -1.5]),
];
const order = [...objects].sort((a, b) => a.x - b.x);
const span = () => { const boxes = objects.map(objectBox);
  return [Math.min(...boxes.map(b => b.minX)), Math.max(...boxes.map(b => b.maxX))]; };
const [left, right] = span();
for (const [anchor, y] of [['base', o => objectBox(o).maxY], ['tip', o => o.y + o.size.main_length.end[1]],
  ['depth', o => o.y + o.size.main_length.start[1]]]) {
  alignObjects(anchor);
  const lines = objects.map(y);
  for (const line of lines) assert.ok(Math.abs(line - lines[0]) < 1e-9, anchor + ' anchors share one line');
  assert.ok(Math.abs(lines[0]) < 1e-9, anchor + ' line sits on y = 0, a major grid line at every zoom');
  assert.ok(objects.every(o => o.rotation === o.defaultRotation), 'outlines stand upright');
  const sorted = [...objects].sort((a, b) => a.x - b.x);
  assert.deepEqual(sorted, order, 'left-to-right order is kept');
  for (let i = 1; i < sorted.length; i++) {
    assert.ok(objectBox(sorted[i]).minX >= objectBox(sorted[i - 1]).maxX + OUTLINE_GAP - 1e-9, 'no overlap');
  }
  const [newLeft, newRight] = span();
  assert.ok(Math.abs((newLeft + newRight) / 2 - (left + right) / 2) < 1e-9, 'group stays centred');
}
assert.ok(Math.abs(objects[3].x - objects[0].x - 15) < 1e-9, 'already spaced outlines keep their gap');
assert.equal(renders, 3);
const viewBefore = view.y;
const baseLine = objects.reduce((sum, o) => sum + objectBox(o).maxY, 0) / objects.length;
alignObjects('base');
assert.ok(Math.abs(view.y - (viewBefore + baseLine * view.zoom)) < 1e-9, 'a locked view pans with the row so it stays on screen');
`, context);
console.log('Align keeps order, clears overlaps, and puts anchors on y = 0');
