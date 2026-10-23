# Development

Run the checks with:

```sh
.venv/bin/python -m unittest discover -s tests -q
```

Additional server options are available through
`.venv/bin/python -m server.cli --help`.

## Project structure

```text
server/
├── app.py                # FastAPI application composition
├── cli.py                # command-line arguments and Uvicorn startup
├── catalog.py            # catalog download, trust configuration and slugs
├── artifacts.py          # review-file I/O and mask/SVG helpers
├── workspace.py          # catalog, image cache, review state and prefetching
├── hosted.py             # SQLite state, claims, sessions and guest queue
├── models.py             # validated request and review-state shapes
└── routes/
    ├── pages.py          # authentication, sessions and HTML pages
    ├── public.py         # guest processing and independent submissions
    ├── reviews.py        # catalog editing, saving, stats and comparisons
    └── moderation.py     # hosted submission moderation
static/                   # HTML, CSS and browser-side interaction
tests/                    # local, hosted, outline and release tests
certificates/             # additional verified catalog trust root
admin/                    # deployment, release, and service administration
```

`outline.py` owns standalone mask-to-SVG conversion. `admin/release_dataset.py` owns
dataset validation, packaging and release publication. Changelog generation
reuses the catalog downloader for product names.

## Dataset releases

Preview a standalone product changelog for the latest published release:

```sh
.venv/bin/python admin/dataset_changelog.py --output dist/CHANGELOG.md
```

Pass a release name and optionally `--previous vN` to choose the comparison.
The preview reads both published ZIPs and their pinned catalogs for product names.
It groups changed products by status, product type, and vendor, using the same
classification as the release-note counters. Each product occupies one line;
outline additions, removals, and updates are counted without listing sizes.
This preview command does not modify release archives or upload anything.
Every newly built ZIP includes `CHANGELOG.md` at its top level, generated against
the latest published release using the same product change classification as
the release notes. For the first release, the standalone preview lists all
products as added.

Install the additional plotting dependency once:

```sh
.venv/bin/python -m pip install -r admin/requirements-charts.txt
```

Build and publish a data-only release using the next integer release name from GitHub:

```sh
.venv/bin/python admin/release_dataset.py
```

Pass a name such as `v20` explicitly to override the automatic name. Publishing
requires a clean checkout whose current commit has been pushed. The script uploads
privately, verifies the archive, then publishes it and regenerates both charts.

To build under `dist/` without uploading, use `--build-only`. To leave an uploaded
release as a private draft instead, use:

```sh
.venv/bin/python admin/release_dataset.py --draft
```

Publish the most recently created stable draft without rebuilding its archive:

```sh
.venv/bin/python admin/release_dataset.py --publish-draft
```

Add a release name to select a particular draft. Its uploaded archive and digest
are verified before publication. Drafts and prereleases are excluded from charts.

Both publication paths regenerate `docs/charts/` using published archives and the
current pinned catalog. Commit and push the generated files to update the README.
The chart update is local; the script does not commit or push documentation.
To refresh the charts separately (including after a post-publication chart error):

```sh
.venv/bin/python admin/release_dataset.py --charts-only
```

The charts count each product in the current measured catalog once, matching the
Stats page's measurement filter. Image quality follows each release's main record;
product types come from the current catalog. Variants and independent records do
not add counts. `docs/charts/release-history.json` records the plotted counts, and
both SVG and PNG exports use the same renderer. Early v0.N.0 tags display as vN.

Check the current dataset against the latest published release without changing
anything:

```sh
.venv/bin/python admin/release_dataset.py --check
```

For hosted administration, put its SSH destination in the ignored `.env` file:

```dotenv
SILICONE_SHADOWS_SERVER=example.com
SILICONE_SHADOWS_USER=root
```

Then sync approved records, commit them, push, and publish in one run:

```sh
.venv/bin/python admin/release_dataset.py --sync-hosted --push
```

Use `--sync-hosted --build-only` to sync, commit, and build locally without
pushing or uploading. Add `--draft` to the publishing command to leave a draft. Syncing
requires a clean checkout and mirrors the server dataset, including deletions.
Add `--sync-hosted` to `--check` to also compare the hosted dataset using a
temporary download, without syncing or modifying the checkout or server.

The script validates every record and SVG before upload. It then downloads the
draft archive again and verifies it against GitHub's SHA-256 digest, along with
its manifest version and commit, license, rights notice, and changelog. Publication
is automatic unless `--draft` is supplied.

Check the pinned catalog against the latest upstream version with
`.venv/bin/python admin/update_catalog.py`. Add `--update` to update
`catalog_source.json` and commit that file; pushing and deployment remain
separate steps.

The app checks for catalog updates once per day while its server is running.
Local users and hosted reviewers have a compact **vN** control before the navigation tabs;
a small accent dot marks an available update. It opens **Check now** and **Update to vN** actions.
Updating reloads the catalog without restarting the server. Finish the active
editor review before updating. Updates that would change an unfinished review's
image or product identity are blocked until that work is completed or discarded.
Published records remain associated by catalog ID, even when catalog names change.

The selected source and last check live beside the work directory as
`catalog_source.json` and `catalog-check.json`. With the standard hosted service,
these are under `/var/lib/silicone-shadows/`. The downloaded catalog is cached in
`.local/catalog/` under that same state directory. A newer runtime selection
survives deployment of an older repository pin; a newer repository pin takes
precedence at startup. Explicit `--products` catalogs do not enable these controls.

`release_dataset.py --sync-hosted` retrieves the hosted catalog selection as well
as the dataset, validates the provider, and commits `catalog_source.json` alongside
any dataset changes. Catalog-only updates are included too, and the release
manifest uses that pin. Sync refuses to downgrade a newer repository pin.
`--check --sync-hosted` reports the released, local, and hosted catalog versions.
Deploy this app version before syncing, so the hosted source descriptor exists.

## Editor data lifecycle

`static/editor-session.js` owns the complete product draft in browser memory.
Opening `/api/editor/items/{id}/open` acquires the existing 15-minute claim and
returns a fingerprinted baseline, outline geometry, metadata and asset URLs.
Published SVGs become editable pixels in the browser without uploading a mask.
Painting, ratings, length lines, variant assignments and metadata changes do
not write server draft state. Moving away or Cancel discards the working copy.

`/api/editor/process` returns a transient background-removal result. The client
keeps both source and mask and adopts them only after processing succeeds.
Inference runs outside the event loop, with one processing operation per server.
Source preparation for unworked catalog entries remains a reusable image cache.

Save sends a complete multipart document to `/api/editor/items/{id}/commit`.
The server verifies the claim and baseline fingerprint, builds temporary
artifacts, and reuses `review_commit.save_review` for whole-product validation,
SVG export and publication or moderation staging. Untouched SVG paths stay
vector data; raster masks are uploaded only for new photos or shape edits.
Archive export uses the same path without committing the draft. Existing review
routes remain compatible with older clients and share this commit service.

Independent metadata is cached until a dataset change or explicit comparison
reload. Compare continues serving its previous snapshot during a debounced
rebuild. The editor updates its catalog summary after Save rather than fetching
the entire catalog after individual edits.

Claim expiry suspends editing without clearing the browser document. Resume
editing reacquires a claim and checks the same baseline before restoring the
tools. Reloading or leaving a dirty tab displays the browser unsaved-work warning.
