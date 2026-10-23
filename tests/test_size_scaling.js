const assert = require('node:assert/strict');
const {
  scalingCheck, worstScalingCheck, meetsScalingLevel, describeScalingStep, suggestOutlineGroups,
  withoutWrongValues,
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

// Groups follow length order and compare against each group's smallest size,
// so small steps cannot drift into one group.
assert.deepEqual(suggestOutlineGroups([
  { sl: 'Large', len: 10, circ: 7 },
  { sl: 'Small', len: 5, circ: 5 },
  { sl: 'Medium', len: 6, circ: 6 },
  { sl: 'XL', len: 12, circ: 8.4 },
]), [1, 0, 0, 1]);
// Girth grows with length^0.7: each step is about 5% slimmer, but 9 vs 5 is 16%.
const drifting = [5, 6, 7, 8, 9].map(length => ({ len: length, circ: 5 * (length / 5) ** 0.7 }));
assert.deepEqual(suggestOutlineGroups(drifting), [0, 0, 0, 0, 1]);
// An odd size (here a likely catalog typo) does not split the sizes around it.
assert.deepEqual(suggestOutlineGroups([
  { ShortLabel: 'XS', len: 5.5, circ: 3.9, wcirc: 5.4 },
  { ShortLabel: 'S', len: 6.3, circ: 4.5, wcirc: 6.3 },
  { ShortLabel: 'M', len: 7.8, circ: 5.3, wcirc: 7.4 },
  { ShortLabel: 'L', len: 9.5, circ: 9.8, wcirc: 9.5 },
  { ShortLabel: 'XL', len: 11.3, circ: 7.9, wcirc: 11.3 },
]), [0, 0, 0, 1, 0]);
// Unmeasured sizes are left out; one comparable size is its own group.
assert.deepEqual(suggestOutlineGroups([{ len: 5, circ: 5 }, { len: 6 }, { circ: 4 }]), [0, null, null]);
// Sizes without a shared girth measure cannot share a group.
assert.deepEqual(suggestOutlineGroups([{ len: 5, circ: 5 }, { len: 6, wcirc: 7 }]), [0, 1]);
assert.deepEqual(suggestOutlineGroups(undefined), []);

// Every pair counts: no neighbouring step reaches 15%, but S vs L does.
const gradual = scalingCheck([
  { ShortLabel: 'S', len: 5, circ: 5 },
  { ShortLabel: 'M', len: 6, circ: 5.6 },
  { ShortLabel: 'L', len: 7, circ: 5.9 },
]);
assert.equal(gradual.from, 'S');
assert.equal(gradual.to, 'L');
assert.equal(gradual.level, 'possible');

// Sizes with a single wrong catalog value are left out of comparisons.
const austin = [
  { ShortLabel: 'M', len: 7.8, circ: 5.3 },
  { ShortLabel: 'L', len: 9.5, circ: 9.8 },
  { ShortLabel: 'XL', len: 11.3, circ: 7.9 },
];
const issues = [
  { sizes: ['L', 'XL'], wrong_value: false },
  { sizes: ['L'], wrong_value: true },
];
assert.deepEqual(withoutWrongValues(austin, issues).map(size => size.ShortLabel), ['M', 'XL']);
assert.equal(scalingCheck(withoutWrongValues(austin, issues)).level, null);
assert.equal(withoutWrongValues(austin, undefined).length, 3);
assert.deepEqual(withoutWrongValues(undefined, issues), []);
