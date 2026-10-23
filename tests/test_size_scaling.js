const assert = require('node:assert/strict');
const {
  scalingCheck, worstScalingCheck, meetsScalingLevel, describeScalingStep,
} = require('../static/size-scaling.js');

// Uniform within rounding: no level.
const uniform = scalingCheck([
  { ShortLabel: 'S', len: 5, circ: 4.5 },
  { ShortLabel: 'M', len: 6, circ: 5.4 },
]);
assert.equal(uniform.level, null);
assert.ok(Math.abs(uniform.deviation) < 1e-9);

// Sizes are compared in length order and the worst step wins.
const thicker = scalingCheck([
  { ShortLabel: 'L', len: 11, circ: 11.1 },
  { ShortLabel: 'S', len: 7, circ: 5 },
  { ShortLabel: 'M', len: 9, circ: 6 },
]);
assert.equal(thicker.from, 'M');
assert.equal(thicker.to, 'L');
assert.equal(thicker.measure, 'circumference');
assert.equal(thicker.level, 'likely');
assert.ok(thicker.deviation > 0.5);
assert.match(describeScalingStep(thicker), /^M → L: circumference ×1\.85, length ×1\.22 \(51% thicker than uniform\)$/);

// Widest circumference counts, and longer-but-not-wider is flagged as slimmer.
const slimmer = scalingCheck([
  { sl: 'Medium', len: 8, circ: 6, wcirc: 7 },
  { sl: 'Large', len: 10, circ: 7.5, wcirc: 7.4 },
]);
assert.equal(slimmer.from, 'Medium');
assert.equal(slimmer.measure, 'widest circumference');
assert.equal(slimmer.level, 'possible');
assert.ok(slimmer.deviation < 0);
assert.match(describeScalingStep(slimmer), /slimmer than uniform/);

// Independent records use full field names.
const independent = scalingCheck([
  { label: 'Small', short_label: 'S', unit: 'in', length: 5, circumference: 5 },
  { label: 'Large', short_label: 'L', unit: 'in', length: 10, circumference: 5 },
]);
assert.equal(independent.level, 'likely');
assert.equal(independent.from, 'S');

// Mixed units are compared in inches.
const mixedUnits = scalingCheck([
  { short_label: 'S', unit: 'in', length: 5, circumference: 5 },
  { short_label: 'L', unit: 'cm', length: 25.4, circumference: 25.4 },
]);
assert.equal(mixedUnits.level, null);
assert.ok(Math.abs(mixedUnits.lengthRatio - 2) < 1e-9);

// Unmeasured, single, or equal-length sizes cannot be checked.
assert.equal(scalingCheck([{ len: 5, circ: 4 }]), null);
assert.equal(scalingCheck([{ len: 5 }, { len: 6 }]), null);
assert.equal(scalingCheck([{ len: 5, circ: 4 }, { len: 5, circ: 6 }]), null);
assert.equal(scalingCheck(undefined), null);

// Shared catalog images report their worst product.
const shared = worstScalingCheck([
  [{ len: 5, circ: 5 }, { len: 6, circ: 6 }],
  [{ len: 5, circ: 5 }, { len: 10, circ: 5 }],
  [],
]);
assert.equal(shared.level, 'likely');
assert.equal(worstScalingCheck([]), null);

assert.equal(meetsScalingLevel(thicker, 'possible'), true);
assert.equal(meetsScalingLevel(slimmer, 'possible'), true);
assert.equal(meetsScalingLevel(slimmer, 'likely'), false);
assert.equal(meetsScalingLevel(uniform, 'possible'), false);
assert.equal(meetsScalingLevel(null, 'possible'), false);
assert.equal(meetsScalingLevel(thicker, 'all'), false);
