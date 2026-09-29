const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('static/compare.html', 'utf8');
const section = (from, to) => html.slice(html.indexOf(`    function ${from}(`), html.indexOf(`    function ${to}(`));
const context = vm.createContext({assert});
vm.runInContext(`
const view = {zoom:37, x:113, y:-41};
let probes = 0;
const hitCtx = {isPointInPath: (_,x,y) => {
  probes++; return x >= 0 && x <= 13 && y >= 0 && y <= 12;
}};
${section('objectScale', 'drawGrid')}
${section('screenToWorld', 'renderOrientation')}
${section('objectBounds', 'fitAll')}
const near = (a,b) => assert.ok(Math.abs(a-b)<1e-9, a+' != '+b);
// An oblique source vector catches reflection in the wrong (SVG) axis.
const object = {product:{main_length:{start:[2,9],end:[8,1]}},
  size:{length_in:7}, midpoint:[5,5], width:13, height:12, x:4,y:-3,
  defaultRotation:-Math.PI/2-Math.atan2(-8,6)};
const ends = outlineAxisEnds(object);
assert.ok(Math.abs(ends[0][0]) < 1e-6);
assert.ok(Math.abs(ends[0][1] - 35/3) < 1e-6);
assert.ok(Math.abs(ends[1][0] - 8.75) < 1e-6);
assert.ok(Math.abs(ends[1][1]) < 1e-6);
const initialProbes = probes;
assert.equal(outlineAxisEnds(object), ends);
assert.equal(probes, initialProbes, 'outline intersection is cached');
for (const angle of [0, .7, -2, Math.PI]) {
  object.rotation=object.defaultRotation+angle;
  object.mirrored=false;
  const tip=worldPoint(object,8,1), base=worldPoint(object,2,9);
  near(Math.hypot(tip[0]-base[0],tip[1]-base[1]),7);
  for (const mirrored of [false,true]) {
    object.mirrored=mirrored;
    worldPoint(object,8,1).forEach((v,i)=>near(v,tip[i]));
    worldPoint(object,2,9).forEach((v,i)=>near(v,base[i]));
    for (const point of [[0,0],[13,12],[3,8]]) {
      const world=worldPoint(object,...point);
      const local=localPoint(object,world[0]*view.zoom+view.x,world[1]*view.zoom+view.y);
      local.forEach((v,i)=>near(v,point[i]));
    }
    const bounds=objectBounds(object);
    near(Math.hypot(bounds[1][0]-bounds[0][0],bounds[1][1]-bounds[0][1]),13*.7);
  }
  const reflected=worldPoint(object,0,0);
  object.mirrored=false;
  const original=worldPoint(object,0,0);
  // The midpoint of a reflected pair lies on the length axis.
  const dx=(reflected[0]+original[0])/2-object.x;
  const dy=(reflected[1]+original[1])/2-object.y;
  near(dx*(tip[1]-base[1])-dy*(tip[0]-base[0]),0);
}
`, context);
new vm.Script(html.match(/<script>([\s\S]*?)<\/script>/)[1]);
console.log('Orientation geometry and script syntax checks passed');
