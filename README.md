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

## Quick start

If you have a coding agent available, you can simply ask it to **set up this
repository and start the local review app**. It does not need to inspect any of
the product images to do that.

For manual installation and platform-specific prerequisites, see
[Contributing and local setup](docs/contributing.md).

1. Clone and set up the project using the linked guide.
2. Review as many entries as you like; progress is retained locally.
3. Commit the changed files under `dataset/` and open a pull request.

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
about the accuracy, attribution, provenance, or inclusion of material, please
[open an issue](https://github.com/quittung/silicone-shadows/issues).
