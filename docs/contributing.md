# Contributing and local setup

Local mode requires no account or hosted-service configuration. It writes
finished records under `dataset/`, ready to commit in a pull request.

## Prerequisites

Install Python 3.11 or newer and
[Potrace](https://potrace.sourceforge.net/):

```sh
# Fedora
sudo dnf install python3 potrace

# Debian or Ubuntu
sudo apt install python3 python3-venv potrace

# macOS with Homebrew
brew install python potrace
```

On Windows, download
[`potrace-1.16.win64.zip`](https://potrace.sourceforge.net/download/1.16/potrace-1.16.win64.zip),
extract it, and add the directory containing `potrace.exe` to `PATH`.

## Setup

```sh
git clone <repository-url> silicone-shadows
cd silicone-shadows
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m server.cli
```

On Windows PowerShell:

```powershell
git clone <repository-url> silicone-shadows
cd silicone-shadows
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m server.cli
```

Open <http://127.0.0.1:8000>. The first run downloads the pinned catalog.
Images are fetched as they enter the work queue, and the first mask may take
longer while rembg downloads its model.

## Contributing records

Review as many entries as you like, then commit the changed files under
`dataset/` and open a pull request. Pull before starting a large batch where
practical, and avoid changing another contributor's record unless you are
deliberately improving it.

Source-code improvements are welcome too. Development details are in
[development.md](development.md). Instructions for running the public hosted
service are in [deployment.md](deployment.md).
