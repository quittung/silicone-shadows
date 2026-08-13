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
deploy/                   # hosted deployment templates
```

`outline.py` owns standalone mask-to-SVG conversion. `release_dataset.py` owns
dataset validation, packaging and draft-release automation. Neither depends on
the web server package.

## Dataset releases

Build a versioned, data-only archive and SHA-256 checksum:

```sh
.venv/bin/python release_dataset.py v0.1.0
```

After inspecting the generated files under `dist/`, create a GitHub draft with:

```sh
.venv/bin/python release_dataset.py v0.1.0 --draft
```

The script validates every record and SVG before upload. It then downloads the
draft assets again and verifies their checksum, manifest version and commit,
license, and rights notice. Publication remains a manual action on GitHub.

The catalog version is pinned in `catalog_source.json`; updating its `version`
makes the app download the corresponding catalog.
