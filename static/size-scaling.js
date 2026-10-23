// Flags products whose catalog girth does not grow with length between sizes,
// so one outline scaled by length would misrepresent some of them.
const SCALING_LEVELS = [
  { id: 'likely', minimum: 0.25, label: 'Likely non-uniform sizing' },
  { id: 'possible', minimum: 0.15, label: 'Possibly non-uniform sizing' },
];
// Catalog sizes use abbreviated keys; independent records use full names.
const SIZE_FIELDS = {
  length: ['len', 'length'],
  girths: [
    { name: 'circumference', keys: ['circ', 'circumference'] },
    { name: 'widest circumference', keys: ['wcirc', 'widest_circumference'] },
  ],
  label: ['ShortLabel', 'short_label', 'sl', 'label'],
};
// Independent sizes may each be entered in their own unit; catalog sizes are inches.
const UNIT_INCHES = { in: 1, cm: 1 / 2.54, mm: 1 / 25.4 };

function sizeValue(size, keys) {
  const scale = UNIT_INCHES[size?.unit] ?? 1;
  for (const key of keys) {
    const value = size?.[key];
    if (typeof value === 'number' && Number.isFinite(value) && value > 0) return value * scale;
  }
  return null;
}

function sizeLabel(size, index) {
  return SIZE_FIELDS.label.map(key => size?.[key]).find(Boolean) || `Size ${index + 1}`;
}

// Sizes with a usable length, in length order, keeping their input position.
function measuredSizes(sizes) {
  return (Array.isArray(sizes) ? sizes : [])
    .map((size, index) => ({ size, index, label: sizeLabel(size, index), length: sizeValue(size, SIZE_FIELDS.length) }))
    .filter(entry => entry.length)
    .sort((left, right) => left.length - right.length);
}

// The girth measure whose growth differs most from length growth between two
// sizes, or null when they share no girth measure.
function worstMismatch(smaller, larger) {
  const lengthRatio = larger.length / smaller.length;
  let worst = null;
  for (const girth of SIZE_FIELDS.girths) {
    const from = sizeValue(smaller.size, girth.keys), to = sizeValue(larger.size, girth.keys);
    if (!from || !to) continue;
    const girthRatio = to / from;
    // Signed: positive when the larger size is thicker than uniform scaling predicts.
    const deviation = girthRatio / lengthRatio - 1;
    if (!worst || Math.abs(deviation) > Math.abs(worst.deviation)) {
      worst = { measure: girth.name, girthRatio, lengthRatio, deviation };
    }
  }
  return worst;
}

// Returns the worst mismatch between any two sizes of different length, or
// null when fewer than two sizes have a length and a comparable girth. Every
// pair counts, so small steps that add up across the range are caught too.
function scalingCheck(sizes) {
  const measured = measuredSizes(sizes);
  let worst = null;
  measured.forEach((smaller, index) => {
    for (const larger of measured.slice(index + 1)) {
      if (larger.length <= smaller.length) continue;
      const mismatch = worstMismatch(smaller, larger);
      if (mismatch && (!worst || Math.abs(mismatch.deviation) > Math.abs(worst.deviation))) {
        worst = { from: smaller.label, to: larger.label, ...mismatch };
      }
    }
  });
  if (!worst) return null;
  const level = SCALING_LEVELS.find(candidate => Math.abs(worst.deviation) >= candidate.minimum) || null;
  return { ...worst, level: level?.id || null, label: level?.label || null };
}

// Suggests which sizes can share an outline. In length order, each size joins
// the group whose smallest size has the closest proportions within the
// "possible" tolerance, or starts a new group. Comparing against each group's
// smallest size keeps small steps from drifting, and an odd size in between
// does not split the sizes around it. Returns a group number per input size,
// numbered in length order, and null for sizes that cannot be compared.
function suggestOutlineGroups(sizes) {
  const tolerance = SCALING_LEVELS.find(level => level.id === 'possible').minimum;
  const groups = (Array.isArray(sizes) ? sizes : []).map(() => null);
  const anchors = [];
  for (const entry of measuredSizes(sizes)) {
    if (!SIZE_FIELDS.girths.some(girth => sizeValue(entry.size, girth.keys))) continue;
    let best = null;
    anchors.forEach((anchor, group) => {
      const mismatch = worstMismatch(anchor, entry);
      const distance = mismatch && Math.abs(mismatch.deviation);
      if (mismatch && distance < tolerance && (!best || distance < best.distance)) best = { group, distance };
    });
    if (!best) best = { group: anchors.push(entry) - 1 };
    groups[entry.index] = best.group;
  }
  return groups;
}

// Leaves out sizes that the catalog checks (server/catalog_checks.py) found to
// carry a single wrong value, so one typo does not decide the comparison.
function withoutWrongValues(sizes, issues) {
  const wrong = new Set((issues || []).filter(issue => issue.wrong_value).flatMap(issue => issue.sizes));
  return (Array.isArray(sizes) ? sizes : []).filter((size, index) => !wrong.has(sizeLabel(size, index)));
}

// The worst check across size lists, e.g. products that share one catalog image.
function worstScalingCheck(sizeLists) {
  return (sizeLists || [])
    .map(scalingCheck)
    .filter(Boolean)
    .reduce((worst, check) =>
      !worst || Math.abs(check.deviation) > Math.abs(worst.deviation) ? check : worst, null);
}

function meetsScalingLevel(check, levelId) {
  const level = SCALING_LEVELS.find(candidate => candidate.id === levelId);
  return Boolean(level && check && Math.abs(check.deviation) >= level.minimum);
}

function describeScalingStep(check) {
  const percent = Math.round(Math.abs(check.deviation) * 100);
  return `${check.from} → ${check.to}: ${check.measure} ×${check.girthRatio.toFixed(2)}, ` +
    `length ×${check.lengthRatio.toFixed(2)} (${percent}% ${check.deviation > 0 ? 'thicker' : 'slimmer'} than uniform)`;
}

if (typeof module !== 'undefined') {
  module.exports = {
    SCALING_LEVELS, scalingCheck, worstScalingCheck, meetsScalingLevel, describeScalingStep, suggestOutlineGroups,
    withoutWrongValues,
  };
}
