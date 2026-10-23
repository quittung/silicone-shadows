const assert = require('node:assert/strict');
const {deflateSync, inflateSync} = require('node:zlib');
require('../static/compare-share.js');
const {capture, encode, decode} = globalThis.CompareShare;
const pack = data => '1.' + deflateSync(Buffer.from(JSON.stringify(data))).toString('base64url');

(async () => {
  // Stable independent producer/consumer checks, including string IDs and duplicates.
  const data = [1, [[1711, 2, -1407, 953, -1457, 1, 7],
    ['reference:credit-card', 0, 0, 0, 0, 0, 0], [1711, 2, -1407, 953, -1457, 1, 7]]];
  assert.deepEqual(await decode(pack(data)), data);
  const encoded = await encode(data);
  assert.deepEqual(JSON.parse(inflateSync(Buffer.from(encoded.slice(2), 'base64url'))), data);
  assert.match(encoded, /^1\.[A-Za-z0-9_-]+$/);
  assert.deepEqual(await decode(encoded), data);

  const palette = ['red', 'blue'];
  const object = {product:{id:'reference:credit-card'}, size:{index:0}, x:-12.34567,
    y:7.89123, rotation:0.56, defaultRotation:0.12, mirrored:true, color:'blue'};
  const captured = capture([object], false, palette);
  assert.equal(captured[0], 0);
  assert.deepEqual(captured[1][0], ['reference:credit-card', 0, -1235, 789, 252, 1, 1]);
  for (const angle of [-100 * Math.PI, -Math.PI, Math.PI, 100 * Math.PI]) {
    const row = capture([{...object, rotation:object.defaultRotation + angle}], true, palette)[1][0];
    assert.ok(row[4] >= -1800 && row[4] < 1800);
  }
  for (const bad of ['', '2.abc', '1.', '1.%%%', '1.abcd', '1.' + 'a'.repeat(7500)]) {
    await assert.rejects(decode(bad));
  }
  for (const bad of [null, [1, []], [true, data[1]], [1, [[1,0,0,0,0,0,8]]],
    [1, [[1,0,NaN,0,0,0,0]]], [1, [['',0,0,0,0,0,0]]],
    [1, [[1,-1,0,0,0,0,0]]], [1, [[1,0,0,0,1800,0,0]]]]) {
    await assert.rejects(decode(pack(bad)));
    await assert.rejects(encode(bad));
  }
  // Reject a tiny compressed input that would expand beyond the decode budget.
  await assert.rejects(decode('1.' + deflateSync(Buffer.alloc(300000, 32)).toString('base64url')));
  await checkImports();
  console.log('Comparison codec: compatibility, precision, duplicates, validation, and decompression limits passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });

async function checkImports() {
  const fs = require('node:fs');
  const vm = require('node:vm');
  const html = fs.readFileSync('static/compare.html', 'utf8');
  const code = html.slice(html.indexOf('    function isDefaultCanvas('), html.indexOf('    function saveWorkspace('));
  const context = vm.createContext({assert, console, URL, CompareShare});
  await vm.runInContext(`(async () => {
    const palette = ['red', 'blue', 'green', 'purple', 'orange', 'cyan', 'pink', 'indigo'];
    const DEFAULT_PRODUCT_ID = 'reference:credit-card', LOAD_BATCH_SIZE = 1;
    const products = [{id:7,sizes:[{index:0},{index:1}]}];
    let objects, selected, placementArea, layerPage, activeLoad, metric;
    let choice = 'cancel', fail = false, stop = false, saves = 0;
    const nodes = new Map();
    function $(id) {
      if (!nodes.has(id)) nodes.set(id, {hidden:true, showModal(){}, addEventListener(_, cb){this.returnValue=choice;cb();}});
      return nodes.get(id);
    }
    let status = '';
    function setStatus(text){status=text;}
    function objectBox(o){return {minX:o.x-1,maxX:o.x+1,minY:o.y-1,maxY:o.y+1};}
    function bulkLayout(){return {};}
    function placeBulkObject(offset){offset.x=100;offset.y=200;}
    function beginLoading(){activeLoad={done:0,stopped:false,stopPromise:new Promise(()=>{})};return activeLoad;}
    function finishLoading(){activeLoad=null;}
    async function loadOutline(){if(fail)throw Error('Network failure');return {};}
    async function paintLoadingBatch(op){if(stop)op.stopped=true;return !op.stopped;}
    function stopMeasurement(){} function setUnits(value){metric=value;}
    function addObject(product,size,loaded,color){const o={product,size,color,defaultRotation:0};objects.push(o);return o;}
    function fitAll(){} function renderLayers(){} function render(){}
    function saveWorkspace(){saves++;}
    ${code}
    const original = {product:{id:9},size:{index:0},x:3,y:4};
    const data = [1, [[7,0,111,-222,350,1,2],[7,1,333,444,-370,0,3],[7,0,111,-222,350,1,2]]];
    function reset(){objects=[original];choice='cancel';fail=stop=false;metric=false;saves=0;}
    reset();await importSharedComparison(data);
    assert.equal(objects.length,1);assert.equal(objects[0],original);assert.equal(saves,0);
    reset();choice='replace';fail=true;
    await assert.rejects(importSharedComparison(data));
    assert.equal(objects[0],original);assert.equal(activeLoad,null);assert.equal(saves,0);
    reset();choice='replace';stop=true;await importSharedComparison(data);
    assert.equal(objects[0],original);assert.equal(saves,0);
    reset();choice='replace';await importSharedComparison(data);
    assert.equal(objects.length,3);assert.equal(metric,true);
    assert.equal(objects[0].mirrored,true);assert.equal(objects[0].x,1.11);
    assert.equal(objects[0].size.index,0);assert.equal(objects[1].size.index,1);assert.equal(objects[2].size.index,0);
    reset();choice='add';await importSharedComparison(data);
    assert.equal(objects.length,4);assert.equal(objects[0],original);assert.equal(metric,false);
    assert.equal(objects[1].x,101.11);assert.equal(objects[1].y,197.78);
    assert.equal(objects[2].x,103.33);assert.equal(saves,1);
    reset();objects=[];await importSharedComparison(data);assert.equal(objects.length,3);
    reset();await assert.rejects(importSharedComparison([1,[[999,0,0,0,0,0,0]]]));assert.equal(objects[0],original);
    const card = {product:{id:DEFAULT_PRODUCT_ID,sizes:[{index:0}]},size:{index:0},x:0,y:0,rotation:0,defaultRotation:0,color:'red',mirrored:false};
    objects=[card];placementArea={centerX:0,centerY:0};assert.equal(isDefaultCanvas(),true);
    card.x=1;assert.equal(isDefaultCanvas(),false);card.x=0;
    card.rotation=.1;assert.equal(isDefaultCanvas(),false);card.rotation=0;
    card.mirrored=true;assert.equal(isDefaultCanvas(),false);card.mirrored=false;
    choice='cancel';await importSharedComparison(data);assert.equal(objects.length,3);
    console.log('Comparison import: add, replace, cancel, default detection, duplicates, failure, and stop checks passed.');
  })()`, context);
}
