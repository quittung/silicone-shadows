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

// Returns the worst step between consecutive sizes, ordered by length, or null
// when fewer than two sizes have a length and a comparable girth.
function scalingCheck(sizes) {
  const measured = (Array.isArray(sizes) ? sizes : [])
    .map((size, index) => ({ size, label: sizeLabel(size, index), length: sizeValue(size, SIZE_FIELDS.length) }))
    .filter(entry => entry.length)
    .sort((left, right) => left.length - right.length);
  let worst = null;
  for (let index = 1; index < measured.length; index++) {
    const smaller = measured[index - 1], larger = measured[index];
    if (larger.length <= smaller.length) continue;
    const lengthRatio = larger.length / smaller.length;
    for (const girth of SIZE_FIELDS.girths) {
      const from = sizeValue(smaller.size, girth.keys), to = sizeValue(larger.size, girth.keys);
      if (!from || !to) continue;
      const girthRatio = to / from;
      // Signed: positive when the larger size is thicker than uniform scaling predicts.
      const deviation = girthRatio / lengthRatio - 1;
      if (!worst || Math.abs(deviation) > Math.abs(worst.deviation)) {
        worst = { from: smaller.label, to: larger.label, measure: girth.name, girthRatio, lengthRatio, deviation };
      }
    }
  }
  if (!worst) return null;
  const level = SCALING_LEVELS.find(candidate => Math.abs(worst.deviation) >= candidate.minimum) || null;
  return { ...worst, level: level?.id || null, label: level?.label || null };
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
  module.exports = { SCALING_LEVELS, scalingCheck, worstScalingCheck, meetsScalingLevel, describeScalingStep };
}
