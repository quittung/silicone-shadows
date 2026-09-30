const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('static/compare.html', 'utf8');
const section = (from, to) => html.slice(html.indexOf(`    function ${from}(`), html.indexOf(`    function ${to}(`));
vm.runInNewContext(`
const view = {zoom:40, x:17, y:-23};
const wrap = {clientWidth:390, clientHeight:760};
let measurement = [[2,4],[5,8]];
${section('screenToWorld', 'objectMatrix')}
${section('measurementLength', 'renderMeasurement')}
assert.equal(measurementLength(), 5);
assert.deepEqual(measurementHandle(0), [63,185]);
assert.deepEqual(measurementHandle(1), [251,345]);
// Changing the camera does not change the world-space length.
view.zoom = 80; view.x = -10;
assert.equal(measurementLength(), 5);
moveMeasurementPoint(0, 150, 220);
assert.deepEqual(measurement[0], [2,243/80]);
// Handles stay within reach at all canvas edges, without altering the camera.
for (const i of [0,1]) {
  for (const point of [[-100,-100],[1000,1000]]) {
    moveMeasurementPoint(i, ...point);
    const [x,y] = measurementHandle(i);
    assert.ok(x >= 24 && x <= 366 && y >= 24 && y <= 736);
  }
}
measurement[1] = [...measurement[0]];
assert.equal(measurementLength(), 0);
`, {assert});
console.log('Measurement distance, camera invariance, and endpoint bounds passed');
const events = html.slice(html.indexOf("    canvas.addEventListener('pointerdown'"), html.indexOf("    addEventListener('keydown', event => {"));
vm.runInNewContext(`
const handlers = {};
const canvas = {style:{}, setPointerCapture(){}, addEventListener(name, fn){handlers[name] = fn;}};
const view = {zoom:40, x:0, y:0};
const measurement = [[2,4],[5,8]];
const selected = {x:7,y:9};
let rotationDrag=null, measureDrag=null, drag=null, pan=null, pinch=null, scaleLocked=false;
const touches = new Map();
const MIN_ZOOM=1, MAX_ZOOM=400;
const pointerPosition = e => [e.clientX,e.clientY];
const render = () => {}, queueWorkspaceSave = () => {};
const hitObject = () => {throw Error('Measuring must not select outlines');};
const clearSelection = () => {throw Error('Measuring must preserve selection');};
${section('screenToWorld', 'objectMatrix')}
${section('setZoom', 'adjustScale')}
${html.slice(html.indexOf('    function startPinch('), html.indexOf("    canvas.addEventListener('pointerdown'"))}
${events}
const event = (x,y,id=1,type='mouse') => ({clientX:x,clientY:y,button:0,pointerId:id,pointerType:type,preventDefault(){}});
handlers.pointerdown(event(100,100));
handlers.pointermove(event(125,140));
assert.deepEqual(view, {zoom:40,x:25,y:40});
handlers.pointerup(event(125,140));
handlers.wheel({...event(125,140),deltaY:-100});
assert.ok(view.zoom > 40);
const beforePinch = view.zoom;
handlers.pointerdown(event(100,100,1,'touch'));
handlers.pointerdown(event(200,100,2,'touch'));
handlers.pointermove(event(250,100,2,'touch'));
assert.ok(view.zoom > beforePinch);
assert.deepEqual(selected, {x:7,y:9});
assert.deepEqual(measurement, [[2,4],[5,8]]);
`, {assert});
console.log('Measurement panning, wheel zoom, and pinch zoom passed');
