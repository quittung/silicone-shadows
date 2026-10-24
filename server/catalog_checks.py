"""Consistency checks for size measurements in the Fantasy Toybox catalog."""

import math
import statistics
from collections.abc import Callable
from dataclasses import dataclass


MEASUREMENTS = {"len": "length", "circ": "circumference", "wcirc": "widest"}
GIRTHS = ("circ", "wcirc")
# Matches the "possibly non-uniform" tolerance in static/size-scaling.js.
PROPORTION_TOLERANCE = 0.15
# Decreases up to the catalog's quarter-inch rounding are not reported.
ROUNDING = 0.25
# Typical toys have a circumference near their length; eggs and plugs are
# naturally wider.
IMPLAUSIBLE_GIRTH_RATIO = 2.5
WIDE_PRODUCT_TYPES = {"OviAndEgg", "Plug"}


@dataclass
class Finding:
    check: str
    product_id: int
    vendor: str
    name: str
    sizes: list[str]
    detail: str
    # The listed sizes probably carry one wrong value, so size comparisons
    # should leave them out.
    wrong_value: bool = False


@dataclass(frozen=True)
class Check:
    key: str
    title: str
    explanation: str
    run: Callable
    wrong_value: bool = False
    # Whether the finding makes measurements less trustworthy for contributors;
    # the others only concern catalog upkeep.
    affects_measurements: bool = True


def number(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return float(value)
    return None


def label(size: dict) -> str:
    return str(size.get("ShortLabel") or size.get("sl") or "?")


def inches(value: float) -> str:
    return f"{value:g}"


def length_inversions(sizes: list[dict]):
    """Neighbouring sizes, by size code, where the larger size is shorter."""
    if not all(number(size.get("sc")) for size in sizes):
        return
    ordered = [
        size
        for size in sorted(sizes, key=lambda size: size["sc"])
        if number(size.get("len"))
    ]
    for smaller, larger in zip(ordered, ordered[1:]):
        if larger["len"] < smaller["len"]:
            yield smaller, larger


def by_length(sizes: list[dict]) -> list[dict]:
    """Sizes in length order, or none when the lengths contradict the size order."""
    if any(length_inversions(sizes)):
        return []
    return sorted(
        (size for size in sizes if number(size.get("len"))),
        key=lambda size: size["len"],
    )


def deviation(smaller: dict, larger: dict, girth: str) -> float | None:
    """How far girth growth differs from length growth between two sizes."""
    lengths = number(smaller.get("len")), number(larger.get("len"))
    girths = number(smaller.get(girth)), number(larger.get(girth))
    if not all(lengths) or not all(girths):
        return None
    return (girths[1] / girths[0]) / (lengths[1] / lengths[0]) - 1


def pi_multiples(values: list[float]) -> bool:
    """Whether every value is pi times a one-decimal number, e.g. 21.99 = pi x 7.0."""
    return bool(values) and all(
        abs(value / math.pi * 10 - round(value / math.pi * 10)) < 0.03
        and abs(value * 4 - round(value * 4)) > 0.02
        for value in values
    )


def missing_size_code(product: dict, sizes: list[dict]):
    for size in sizes:
        if number(size.get("sc")) is None:
            yield [label(size)], f"{label(size)} has no size code"


def invalid_values(product: dict, sizes: list[dict]):
    for size in sizes:
        for key, name in MEASUREMENTS.items():
            value = size.get(key)
            if value is not None and number(value) is None:
                yield [label(size)], f"{label(size)} {name} is {value!r}"


def duplicate_labels(product: dict, sizes: list[dict]):
    named: dict[str, list[str]] = {}
    for size in sizes:
        named.setdefault(str(size.get("sl")), []).append(label(size))
    for name, labels in named.items():
        if len(labels) > 1:
            yield (
                labels,
                (
                    f"the full size name “{name}” is used by {' and '.join(labels)}; "
                    "the short labels differ, so one full name is probably wrong"
                    if len(set(labels)) == len(labels)
                    else f"“{name}” is listed {len(labels)} times"
                ),
            )


def identical_sizes(product: dict, sizes: list[dict]):
    groups: dict[tuple, list[str]] = {}
    for size in sizes:
        values = tuple(number(size.get(key)) for key in MEASUREMENTS)
        # A length alone is not enough: sizes may differ in girth the catalog omits.
        if values[0] and any(values[1:]):
            groups.setdefault(values, []).append(label(size))
    for values, labels in groups.items():
        if len(labels) > 1:
            shown = " / ".join(inches(value) if value else "–" for value in values)
            yield labels, f"{' and '.join(labels)} share every measurement ({shown})"


def length_order(product: dict, sizes: list[dict]):
    for smaller, larger in length_inversions(sizes):
        yield (
            [label(smaller), label(larger)],
            (
                f"length drops from {inches(smaller['len'])} ({label(smaller)}) "
                f"to {inches(larger['len'])} ({label(larger)})"
            ),
        )


def girth_order(product: dict, sizes: list[dict]):
    ordered = by_length(sizes)
    for girth in GIRTHS:
        measured = [size for size in ordered if number(size.get(girth))]
        for smaller, larger in zip(measured, measured[1:]):
            if (
                larger["len"] > smaller["len"]
                and smaller[girth] - larger[girth] > ROUNDING
            ):
                yield (
                    [label(smaller), label(larger)],
                    (
                        f"{MEASUREMENTS[girth]} drops from {inches(smaller[girth])} "
                        f"({label(smaller)}) to {inches(larger[girth])} ({label(larger)}) "
                        "although the length grows"
                    ),
                )


def widest_mismatch(product: dict, sizes: list[dict]):
    both = [
        size for size in sizes if number(size.get("circ")) and number(size.get("wcirc"))
    ]
    exceeding = [size for size in both if size["circ"] > size["wcirc"]]
    # A product-wide pattern means the "widest" point (e.g. Head) is narrower.
    if not exceeding or len(exceeding) == len(both):
        return
    odd = (
        exceeding
        if len(exceeding) <= len(both) / 2
        else [size for size in both if size not in exceeding]
    )
    relation = "exceeds" if odd is exceeding else "is below"
    others = "below" if odd is exceeding else "above"
    for size in odd:
        yield (
            [label(size)],
            (
                f"{label(size)} circumference {inches(size['circ'])} {relation} its widest "
                f"{inches(size['wcirc'])}; in the other sizes it is {others} the widest"
            ),
        )


def implausible_girth(product: dict, sizes: list[dict]):
    if product.get("pt") in WIDE_PRODUCT_TYPES:
        return
    measured = [
        size for size in sizes if number(size.get("len")) and number(size.get("circ"))
    ]
    if not measured:
        return
    ratio = statistics.median(size["circ"] / size["len"] for size in measured)
    if ratio <= IMPLAUSIBLE_GIRTH_RATIO:
        return
    circumferences = [size["circ"] for size in measured]
    if pi_multiples(circumferences):
        factors = ", ".join(
            inches(round(value / math.pi, 1)) for value in circumferences
        )
        explanation = (
            f"every value is π × ({factors}); those may already be circumferences, "
            "or diameters in cm"
        )
    else:
        explanation = f"read as centimetres it would be {ratio / 2.54:.1f}× the length"
    yield (
        [label(size) for size in measured],
        (f"circumference is {ratio:.1f}× the length; {explanation}"),
    )


def proportion_outlier(product: dict, sizes: list[dict]):
    ordered = by_length(sizes)
    for before, size, after in zip(ordered, ordered[1:], ordered[2:]):
        breaks = []
        for girth in GIRTHS:
            into, out_of, around = (
                deviation(before, size, girth),
                deviation(size, after, girth),
                deviation(before, after, girth),
            )
            if (
                None not in (into, out_of, around)
                and abs(into) >= PROPORTION_TOLERANCE
                and abs(out_of) >= PROPORTION_TOLERANCE
                and abs(around) < PROPORTION_TOLERANCE
            ):
                breaks.append(
                    f"{MEASUREMENTS[girth]} {inches(size[girth])} "
                    f"({into:+.0%} then {out_of:+.0%})"
                )
        if breaks:
            yield (
                [label(size)],
                (
                    f"{label(size)} {' and '.join(breaks)} breaks the proportions shared by "
                    f"{label(before)} and {label(after)}"
                ),
            )


def missing_widest_label(product: dict, sizes: list[dict]):
    if any(number(size.get("wcirc")) for size in sizes) and not product["sz"].get("wl"):
        yield [], "widest circumference is given without saying where it is measured"


CHECKS = [
    Check(
        "missing_size_code",
        "Missing size code",
        "Sizes without a size code, so their order relative to the other sizes is unknown.",
        missing_size_code,
        affects_measurements=False,
    ),
    Check(
        "invalid_values",
        "Invalid values",
        "Measurements that are not positive numbers.",
        invalid_values,
    ),
    Check(
        "duplicate_labels",
        "Duplicate size names",
        "A full size name (sl) appears more than once, so the sizes cannot be told "
        "apart by name. The short labels shown on the site may still differ.",
        duplicate_labels,
        affects_measurements=False,
    ),
    Check(
        "identical_sizes",
        "Identical sizes",
        "Differently named sizes with the same length and girth, often a copy-paste "
        "slip. Sizes with only a length are not compared.",
        identical_sizes,
    ),
    Check(
        "length_order",
        "Length out of order",
        "A larger size (by size order) is shorter than the size before it.",
        length_order,
    ),
    Check(
        "girth_order",
        "Girth shrinks as length grows",
        "A longer size has a clearly smaller circumference than a shorter one.",
        girth_order,
    ),
    Check(
        "widest_mismatch",
        "Circumference vs. widest point",
        "In one size the circumference and widest circumference swap order compared "
        "with the product's other sizes, which points to a typo.",
        widest_mismatch,
        wrong_value=True,
    ),
    Check(
        "implausible_girth",
        "Implausibly large circumference",
        "Circumferences far larger than the length, which suggests a unit or "
        "diameter-to-circumference conversion problem. Eggs and plugs are excluded.",
        implausible_girth,
    ),
    Check(
        "proportion_outlier",
        "Size out of proportion",
        "One size's girth does not fit the proportions of the sizes on either side, "
        "which usually means a single wrong value.",
        proportion_outlier,
        wrong_value=True,
    ),
    Check(
        "missing_widest_label",
        "Widest point not named",
        "A widest circumference is given without the location it is measured at.",
        missing_widest_label,
        affects_measurements=False,
    ),
]


def audit(catalog: list[dict]) -> list[Finding]:
    findings = []
    for product in catalog:
        sizes = [
            size
            for size in product.get("sz", {}).get("s") or []
            if isinstance(size, dict)
        ]
        for check in CHECKS:
            for size_labels, detail in check.run(product, sizes):
                findings.append(
                    Finding(
                        check.key,
                        product["id"],
                        str(product.get("vn") or ""),
                        str(product.get("n") or "").strip(),
                        size_labels,
                        detail,
                        check.wrong_value,
                    )
                )
    return findings


def measurement_issues(catalog: list[dict]) -> dict[object, list[dict]]:
    """Findings that affect measurements, by product ID, for contributor views."""
    checks = {check.key: check for check in CHECKS}
    issues: dict[object, list[dict]] = {}
    for finding in audit(catalog):
        if checks[finding.check].affects_measurements:
            issues.setdefault(finding.product_id, []).append(
                {
                    "detail": finding.detail,
                    "sizes": finding.sizes,
                    "wrong_value": finding.wrong_value,
                }
            )
    return issues
