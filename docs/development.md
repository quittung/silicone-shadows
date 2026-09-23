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
dataset validation, packaging and draft-release automation. Neither depends on
the web server package.

## Dataset releases

Build a data-only archive using the next integer release name from GitHub:

```sh
.venv/bin/python admin/release_dataset.py
```

Pass a name such as `v12` explicitly to override the automatic name. After
inspecting the generated files under `dist/`, create a GitHub draft with:

```sh
.venv/bin/python admin/release_dataset.py --draft
```

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

Then sync approved records, commit them, push, and create the verified draft in
one run:

```sh
.venv/bin/python admin/release_dataset.py --sync-hosted --push --draft
```

Omit `--push --draft` to sync, commit, and build the archive locally. Syncing
requires a clean checkout and mirrors the server dataset, including deletions.
Add `--sync-hosted` to `--check` to also compare the hosted dataset using a
temporary download, without syncing or modifying the checkout or server.

The script validates every record and SVG before upload. It then downloads the
draft archive again and verifies it against GitHub's SHA-256 digest, along with
its manifest version and commit, license, and rights notice. Publication remains
a manual action on GitHub.

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
