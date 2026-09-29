const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('static/compare.html', 'utf8');
const code = html.slice(html.indexOf('    function objectBox('), html.indexOf('    async function addProductSize('));
const context = vm.createContext({assert, console, performance});
vm.runInContext(`
let objects = [], placementArea = null, scaleLocked = false;
const wrap = {clientWidth:1000, clientHeight:700};
const view = {zoom:30, x:0, y:0};
const MIN_ZOOM = 1;
function screenToWorld(x,y) { return [(x-view.x)/view.zoom,(y-view.y)/view.zoom]; }
function objectBounds(o) { return [[o.x-o.w/2,o.y-o.h/2],[o.x+o.w/2,o.y+o.h/2]]; }
function renderLayers() {} function render() {} function queueWorkspaceSave() {}
${code}
const layout = bulkLayout();
const center = [placementArea.centerX, placementArea.centerY];
const ratio = placementArea.width / placementArea.height;
const started = performance.now();
for (let i=0;i<1000;i++) {
  const o = {x:0,y:0,w:1+(i%7)*.25,h:3+(i%13)*.5};
  const previous = objects.map(o=>[o.x,o.y]);
  placeBulkObject(o,layout);
  assert.deepEqual(objects.map(o=>[o.x,o.y]), previous);
  objects.push(o);
}
console.log('Placed 1000 mixed outlines in', Math.round(performance.now()-started), 'ms');
for (let i=0;i<objects.length;i++) {
  const a=objectBox(objects[i]);
  for (let j=0;j<i;j++) {
    const b=objectBox(objects[j]);
    assert.ok(!(a.minX<b.maxX+.649 && a.maxX>b.minX-.649 && a.minY<b.maxY+.649 && a.maxY>b.minY-.649), 'overlap or missing breathing room');
  }
}
assert.ok(Math.abs(placementArea.width/placementArea.height-ratio)<1e-10);
const bounds = objects.map(objectBox);
const clusterRatio = (Math.max(...bounds.map(b=>b.maxX))-Math.min(...bounds.map(b=>b.minX))) /
  (Math.max(...bounds.map(b=>b.maxY))-Math.min(...bounds.map(b=>b.minY)));
assert.ok(clusterRatio > ratio*.8 && clusterRatio < ratio*1.2, 'cluster follows viewport proportions');
view.x = 4000; view.y = -2000;
objects[0].x = -500; // A manual move and a pan do not move the anchor.
const before = objects.map(o=>[o.x,o.y]);
const extra = {x:0,y:0,w:3,h:10}; objects.push(extra);
placeObjects([extra]);
assert.deepEqual(objects.slice(0,-1).map(o=>[o.x,o.y]),before);
assert.deepEqual([placementArea.centerX,placementArea.centerY],center);
const oldZoom = view.zoom;
zoomOutForAdditions([extra]);
assert.ok(view.zoom<=oldZoom);
scaleLocked=true;
const oldView={...view};
zoomOutForAdditions([{x:10000,y:10000,w:20,h:20}]);
assert.deepEqual(view,oldView);
// An oversized object grows the area without breaking the aspect ratio.
const huge={x:0,y:0,w:1000,h:2000};
placeBulkObject(huge,bulkLayout());
assert.ok(Math.abs(placementArea.width/placementArea.height-ratio)<1e-10);
console.log('Layout checks passed; cluster aspect ratio:',clusterRatio.toFixed(2));
`, context);
// Parse the whole inline script too, so integration syntax is checked.
new vm.Script(html.match(/<script>([\s\S]*?)<\/script>/)[1]);
