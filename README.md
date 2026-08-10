# Silicone Shadows

This is a community project to create a clean silhouette and main-length
alignment data for every product in the
[Fantasy Toybox](https://fantasytoybox.net/) catalog. The resulting dataset is
intended for size and shape comparison tools.

The review app downloads catalog images, removes their backgrounds, and provides
a quick workflow for correcting the mask, rating the result, and marking the
product's main length. The catalog contains adult products, so contributors
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

`metadata.json` contains the catalog identity, quality rating, and whether the
silhouette came from the catalog image or an alternative image. Its exact format
is defined by [`schemas/record.schema.json`](schemas/record.schema.json).

Catalog JSON, source images, masks, cutouts, alternative images, and editable
work state are downloaded or generated locally and excluded from Git. A clone
therefore contains the finished dataset without redistributing the source
catalog or its images.

## Quick start

If you have a coding agent available, you can simply ask it to **set up this
repository and start the local review app**. It does not need to inspect any of
the product images to do that.

You need Python 3.11 or newer and [Potrace](https://potrace.sourceforge.net/):

```sh
# Fedora
sudo dnf install python3 potrace

# Debian or Ubuntu
sudo apt install python3 python3-venv potrace

# macOS with Homebrew
brew install python potrace
```

On Windows, download the official
[`potrace-1.16.win64.zip`](https://potrace.sourceforge.net/download/1.16/potrace-1.16.win64.zip),
extract it, and add the directory containing `potrace.exe` to your `PATH`.
Running `potrace --version` in a new terminal should then print its version.

Then clone the repository and create an isolated environment:

```sh
git clone <repository-url> batch_outliner
cd batch_outliner
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python review.py
```

On Windows PowerShell, use the equivalent venv commands:

```powershell
git clone <repository-url> batch_outliner
cd batch_outliner
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python review.py
```

Open <http://127.0.0.1:8000>. The first run downloads the pinned catalog, and
images and masks are fetched as they enter the work queue. The first mask may
also take longer while rembg downloads its model.

## Contributing silhouettes

1. Clone and set up the project as described above.
2. Review as many entries as you like; progress is retained locally.
3. Commit the changed files under `dataset/` and open a pull request.

Pull before starting a large batch where practical, and avoid changing another
contributor's record unless you are deliberately improving it. Source-code
improvements are welcome too.

## Development

Run the checks with:

```sh
.venv/bin/python -m unittest -q
```

Additional server options are available through
`.venv/bin/python review.py --help`.

## Dataset releases

Build a versioned, data-only ZIP and SHA-256 checksum with:

```sh
.venv/bin/python release_dataset.py v0.1.0
```

Inspect the files under `dist/`, including the generated release notes. Once the
release commit is clean and pushed, create a GitHub draft by adding `--draft`:

```sh
.venv/bin/python release_dataset.py v0.1.0 --draft
```

Draft releases are visible only to repository collaborators. Review and edit it
on GitHub, then use GitHub's **Publish release** button when it is ready.

The archive contains only `dataset/` and a manifest recording the dataset
version, schema version, catalog source, commit, timestamp, and quality counts.

The catalog version is pinned in [`catalog_source.json`](catalog_source.json).
Updating its `version` is enough to make the app download a newer catalog.
