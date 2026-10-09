# Dataset format

Each product in the dataset has a `metadata.json` file and SVG outlines for size and shape comparisons. This guide explains how to find the product information, display an outline at the right size, and choose between outlines when a product's proportions vary by size. The full field definitions are in [`record.schema.json`](../schemas/record.schema.json).

## One outline for a product

A typical record lives in `dataset/<vendor>/<product-type>/<product-name>/` and contains two files:

```text
metadata.json
outline.svg
```

For a product in the Fantasy Toybox catalog, `metadata.json` looks like this:

```json
{
  "schema_version": 2,
  "catalog_id": 1234,
  "quality": "good",
  "source": "catalog"
}
```

The `catalog_id` identifies the product in the pinned catalog listed in [`catalog_source.json`](../catalog_source.json). Look up its name, sizes, and measurements there. Products outside the catalog supply that information in their own metadata, as shown under [Independent products](#independent-products).

The `quality` field describes the outline, and `source` tells you whether it came from a catalog photograph (`catalog`) or a contributor-supplied photograph (`alternative`). The source photographs themselves are not included in the dataset. The examples on this page use fictional products and IDs.

## Displaying an outline at the right size

Each SVG is cropped and rotated so the marked base-to-tip line points upward. That line represents the product's usable length and is exactly one SVG unit long. Scale the whole outline uniformly by the chosen size's usable length.

For example, to display a size with a usable length of 15 cm, one SVG unit should represent 15 cm. The marked line may be shorter than the outline's full height, so setting the SVG's total height to 15 cm would give the wrong scale.

All published measurements are in inches. Catalog sizes provide usable length in `len`; independent sizes use `length` with `unit: "in"`. Convert to your preferred display unit when needed. A size needs a usable-length measurement to be displayed at scale.

An outline describes the silhouette seen in the photograph; it does not provide circumference or depth. Its quality rating helps you judge how useful that silhouette is:

| `quality` | Meaning |
| --- | --- |
| `good` | Suitable for silhouette comparison. |
| `bad_perspective` | The shape is recognizable, but perspective distortion makes measurements unreliable. |
| `unusable` | No usable outline is available. The corresponding SVG is absent. |

## When sizes have different proportions

One outline can represent several sizes if they share the same proportions. Some products change shape between sizes - for example, a larger size may get longer without getting wider. These products need separate outlines, assigned through `variants`. Each outline is still scaled uniformly to the selected size's usable length.

Here, Small uses the general `outline.svg`, while Medium and Large share a second outline:

```json
{
  "schema_version": 2,
  "catalog_id": 1234,
  "quality": "good",
  "source": "catalog",
  "variants": {
    "outline-b": {
      "sizes": ["Medium", "Large"],
      "quality": "good",
      "source": "alternative",
      "file": "variants/outline-b.svg"
    }
  }
}
```

The product directory contains:

```text
metadata.json
outline.svg
variants/
└── outline-b.svg
```

The top-level `quality` and `source` describe the general outline. Each variant has its own rating and source, and its `file` path is relative to the product directory.

To choose an outline for a size:

1. If a variant lists that size in `sizes`, use the variant.
2. Otherwise, use the general `outline.svg`.
3. If the selected outline is rated `unusable`, that size has no outline for comparison. An unusable variant does not fall back to the general outline.

A size can belong to only one variant. Assignments use the full size label: `sl` in the catalog, or `label` for independent products. If that is missing, use `ShortLabel` or `short_label`, respectively. For example, a catalog size with `sl: "Medium"` and `ShortLabel: "M"` is assigned as `"Medium"`.

### Missing outlines

A product can have size-specific outlines without a general outline. In that case, its top-level `quality` is `unusable` and there is no root `outline.svg`. Only sizes assigned to usable variants have an outline; unassigned sizes remain unavailable.

Likewise, a variant rated `unusable` keeps its `sizes` and `file` fields, but the SVG named by `file` is absent. This records which sizes lack a usable outline even when the product has a general fallback.

The top-level `source` field is required even when there is no general outline. Use each variant's own `source` for its provenance.

## Independent products

Products outside the catalog use `catalog_id: null` and a stable `record_id`. Their metadata includes the product information and size measurements:

```json
{
  "schema_version": 2,
  "record_id": "community-example",
  "catalog_id": null,
  "vendor": "Example maker",
  "product_type": "Example type",
  "name": "Example product",
  "quality": "good",
  "source": "alternative",
  "product_url": null,
  "species": null,
  "tags": [],
  "features": [],
  "sizes": [
    {
      "label": "Medium",
      "short_label": "M",
      "unit": "in",
      "length": 6
    }
  ],
  "notes": null
}
```

This product's outline is scaled to a usable length of 6 inches. Its `record_id` stays the same if the product is renamed. Independent products can also have variants, with assignments to their own size labels.

## Versions and compatibility

The dataset uses schema version 2, which adds optional size-specific outlines through `variants` and standardizes independent measurements on inches. Apart from the version number and any converted measurements, records without variants have the same structure as version 1. Older readers that accept the new version number and ignore additional fields may still work, but won't use size-specific outlines.
