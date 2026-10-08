# Silicone Shadows

This is a community project to create a clean silhouette and main-length
alignment data for every product in the
[Fantasy Toybox](https://fantasytoybox.net/) catalog. The resulting dataset is
intended for size and shape comparison tools.

The review app removes backgrounds from product photos and provides
a quick workflow for correcting the mask, rating the result, and marking the
product's usable length. The catalog contains adult products, so contributors
should expect adult imagery in the review interface.

## The dataset

The publishable dataset is in [`dataset/`](dataset/):

```text
dataset/<vendor>/<product-type>/<product-name>/
├── metadata.json
└── outline.svg       # omitted when rated unusable
```

Each SVG is tightly cropped, rotated so its directed base-to-tip vector points
upward, and scaled so that vector is exactly one SVG unit long. Simply scale
by the products usable length to show it at that size.

Toybox-backed `metadata.json` files contain only the Toybox catalog ID, quality
rating, and source provenance; names, vendors, types, sizes, tags, and features
are resolved from the pinned Toybox JSON instead of duplicated. Independent
records contain their full entered metadata and a community record ID. The exact
formats are defined by [`schemas/record.schema.json`](schemas/record.schema.json).

Catalog JSON, source images, masks, cutouts, alternative images, and editable
work state are downloaded or generated locally and excluded from Git. A clone
therefore contains the finished dataset without redistributing the source
catalog or its images.

## Using the dataset

Attribution is not required under CC0. If Silicone Shadows is useful to you, a
link back to [this repo](https://github.com/quittung/silicone-shadows) would be
greatly appreciated. If you publish a project that uses the dataset, I'd love
to see it—send a link to
[shadows@qtng.dev](mailto:shadows@qtng.dev).

## Quick start

The quickest way to contribute or create an outline for your own product is the
[hosted version of this app](https://shadows.qtng.dev/). Guests can create and
download their own entries, and contributors can submit their work to be included in
catalog. Contribution requires an invitation; contact `qtng` on Discord or
email [shadows@qtng.dev](mailto:shadows@qtng.dev).

You can instead run the app locally and contribute through Git. With a coding
agent, ask it to **set up this repository and start the local review app**; it
does not need to inspect any product images. For manual installation and
platform-specific prerequisites, see
[Contributing and local setup](docs/contributing.md). Then:

1. Review as many entries as you like; progress is retained locally.
2. Commit the changed files under `dataset/` and open a pull request.

Pull before starting a large batch where practical, and avoid changing another
contributor's record unless you are deliberately improving it. Source-code
improvements are welcome too.

By contributing material under `dataset/`, you apply CC0 1.0 to any copyright,
related rights, or database rights you hold in that contribution.

## Licensing and independence

The software is available under the [MIT No Attribution license](LICENSE). To
the extent that maintainers and contributors hold rights in the dataset and its
silhouettes, those rights are waived under
[CC0 1.0 Universal](dataset/LICENSE). This does not claim ownership of or grant
rights in underlying product designs, source photographs, catalog content,
names, or trademarks. See the dataset's [rights notice](dataset/NOTICE.md) for
details.

Silicone Shadows is independent and is not affiliated with, endorsed by, or
sponsored by Fantasy Toybox or any represented vendor. If you have concerns
about the accuracy, attribution, provenance, or inclusion of material—or want
something removed—please [open an issue](https://github.com/quittung/silicone-shadows/issues)
or email [shadows@qtng.dev](mailto:shadows@qtng.dev).

## Outline variants

Version 1 records remain supported without changes. Records with size-specific
outlines use `schema_version: 2` and an optional `variants` object:

```json
{
  "schema_version": 2,
  "catalog_id": 4794,
  "quality": "good",
  "source": "catalog",
  "variants": {
    "medium-large": {
      "sizes": ["Medium", "Large"],
      "quality": "good",
      "source": "alternative",
      "file": "variants/medium-large.svg"
    }
  }
}
```

The top-level quality and source describe the general `outline.svg`. Specific
outlines live in `variants/<variant-id>.svg`. Sizes use exact full catalog size
labels (`sl`, falling back to `ShortLabel`), or `label` for independent records;
these assignments do not depend on array order. A size may belong to at most
one variant. Each outline is uniformly scaled to the chosen size's usable length.

A specific assignment takes precedence over the general fallback. Without an
assignment, the general outline is used; without either usable outline, that
size is unavailable in Compare. A product containing only specific outlines has
no root `outline.svg` and top-level `quality: "unusable"`. The root source remains
a required legacy field; each variant supplies its own authoritative source.
Old readers that ignore unfamiliar fields can still use a general fallback;
old strict version-1 validators reject version-2 records.

The catalog editor's bottom-bar **Variants** button opens an outline panel.
Radio controls select the outline to edit, and a compact size table shows
coverage, fallback use, and missing assignments. Outline numbers are local UI
labels, not part of the dataset. **Add outline** creates a blank canvas for a
new size assignment; paste or drop its photo to start editing. **Change** beside
an outline's sizes can assign selected sizes or use it as the general fallback. Draft
images, masks, and length markers are stored separately. Save/download covers
the entire product, and every outline must be rated and usable outlines marked
with a length line. Hosted moderation accepts or rejects the whole product.
Published specific outlines without local source images are retained during
re-review; paste or drop a new photo to edit their shapes.
