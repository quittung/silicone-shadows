"""Regenerate README charts from published dataset archives."""

import json
import math
import subprocess
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory

from .dataset_changelog import release_contents
from .release_dataset import LEGACY_VERSION_PATTERN, QUALITIES, ROOT
from server.catalog import download_catalog, has_measurements

OUTPUT_DIR = ROOT / "docs" / "charts"
TYPE_COLORS = {
    "Dildo": "#22d3ee",
    "Penetrable": "#a78bfa",
    "Plug": "#4ade80",
    "OviAndEgg": "#fbbf24",
    "Other": "#fb7185",
    "Wereable": "#60a5fa",
    "Grinder": "#f97316",
    "Paw": "#e879f9",
}


def plotting():
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as error:
        raise RuntimeError(
            "Install chart dependencies: .venv/bin/python -m pip install "
            "-r admin/requirements-charts.txt"
        ) from error
    return plt


def release_label(tag: str) -> str:
    match = LEGACY_VERSION_PATTERN.fullmatch(tag)
    return f"v{int(match.group(1))}" if match else tag


def count_release(contents: list[tuple[str, bytes]], products: dict) -> dict:
    quality, types = Counter(), Counter()
    seen = set()
    for name, content in contents:
        if not name.endswith("/metadata.json"):
            continue
        record = json.loads(content)
        product_id = record.get("catalog_id")
        if product_id not in products:
            continue
        if product_id in seen or record.get("quality") not in QUALITIES:
            raise ValueError(f"Invalid or duplicate catalog record: {name}")
        seen.add(product_id)
        quality[record["quality"]] += 1
        types[products[product_id].get("pt") or "Unknown"] += 1
    return {"quality": dict(quality), "types": dict(types)}


def collect_history() -> dict:
    config = json.loads((ROOT / "catalog_source.json").read_text())
    url = config["url_template"].format(version=config["version"])
    products = {
        product["id"]: product
        for product in json.loads(download_catalog(url))
        if has_measurements(product)
    }
    releases = json.loads(
        subprocess.check_output(
            [
                "gh",
                "release",
                "list",
                "--limit",
                "1000",
                "--json",
                "tagName,isDraft,isPrerelease,publishedAt",
            ],
            cwd=ROOT,
            text=True,
        )
    )
    published = sorted(
        (r for r in releases if not r["isDraft"] and not r["isPrerelease"]),
        key=lambda r: (r["publishedAt"], r["tagName"]),
    )
    if not published:
        raise ValueError("No published dataset releases to chart")
    rows = []
    with TemporaryDirectory() as directory:
        for index, release in enumerate(published):
            contents, _ = release_contents(
                release["tagName"], Path(directory) / str(index)
            )
            rows.append(
                {
                    "tagName": release["tagName"],
                    "publishedAt": release["publishedAt"],
                    **count_release(contents, products),
                }
            )
    return {
        "catalog_version": config["version"],
        "catalog_url": url,
        "measured_products": len(products),
        "releases": rows,
    }


def render_chart(data: dict, field: str, output_dir: Path) -> None:
    plt = plotting()
    rows, total = data["releases"], data["measured_products"]
    if not rows or total <= 0:
        raise ValueError("Charts require releases and a nonempty measured catalog")
    if field == "quality":
        keys = list(QUALITIES)
        labels = ["Good", "Bad perspective", "Unusable"]
        colors = ["#4ade80", "#fbbf24", "#fb7185"]
        title, filename = "Image quality across releases", "image-quality-history"
    else:
        keys = sorted(
            {key for row in rows for key in row[field]},
            key=lambda key: (-rows[-1][field].get(key, 0), key),
        )
        labels = [
            {"OviAndEgg": "Ovi & egg", "Wereable": "Wearable"}.get(k, k) for k in keys
        ]
        colors = [
            TYPE_COLORS.get(key, plt.get_cmap("tab20")(i % 20))
            for i, key in enumerate(keys)
        ]
        title, filename = "Processed products by type", "product-type-history"
    values = [[row[field].get(key, 0) for row in rows] for key in keys]
    remaining = (
        [total - sum(row) for row in zip(*values)] if values else [total] * len(rows)
    )
    if any(value < 0 for value in remaining):
        raise ValueError("Release counts exceed the measured catalog")
    values.append(remaining)
    labels.append("Not processed")
    colors.append("#343d48")
    # Keep the established two-row legend; allow extra rows for future types.
    legend_rows = math.ceil(len(labels) / 5)
    height = 6.1 + max(0, legend_rows - 2) * 0.3
    with plt.rc_context(
        {
            "font.family": "DejaVu Sans",
            "font.size": 12,
            "text.color": "#e8edf2",
            "axes.labelcolor": "#98a3ae",
            "xtick.color": "#98a3ae",
            "ytick.color": "#98a3ae",
            "svg.fonttype": "none",
            "svg.hashsalt": "dataset-charts",
        }
    ):
        fig, ax = plt.subplots(figsize=(10.5, height), facecolor="#111418")
        ax.set_facecolor("#1a1f25")
        fig.subplots_adjust(
            left=0.085,
            right=0.975,
            top=0.82,
            bottom=(0.85 + 0.22 * legend_rows) / height,
        )
        x = list(range(len(rows)))
        ax.stackplot(
            x if len(rows) > 1 else [-0.5, 0.5],
            values if len(rows) > 1 else [[v[0], v[0]] for v in values],
            colors=colors,
            edgecolor="#111418",
            linewidth=0.4,
        )
        ax.axhline(total, color="#e8edf2", linewidth=1, linestyle=(0, (4, 4)))
        ax.set_ylim(0, total * 1.09)
        ax.set_xlim((0, len(rows) - 1) if len(rows) > 1 else (-0.5, 0.5))
        ax.set_yticks([tick for tick in ax.get_yticks() if 0 <= tick <= total])
        ticks = sorted(
            set(range(0, len(rows), max(1, math.ceil(len(rows) / 20))))
            | {len(rows) - 1}
        )
        ax.set_xticks(
            ticks, [release_label(rows[i]["tagName"]) for i in ticks], fontsize=11
        )
        ax.set_ylabel("Products")
        ax.tick_params(length=0, pad=8)
        for spine in ax.spines.values():
            spine.set_visible(False)
        ax.text(
            0,
            total * 1.035,
            f"Catalog · {total:,} products with measurements",
            fontsize=12,
            color="#c6d0db",
        )
        fig.text(0.085, 0.94, title, fontsize=22, weight="bold")
        fig.text(
            0.085,
            0.885,
            f"Published releases through {release_label(rows[-1]['tagName'])}  /  Catalog v{data['catalog_version']}",
            color="#98a3ae",
            fontsize=12,
        )
        legend_labels = [
            f"{label} · {series[-1]:,}" for label, series in zip(labels, values)
        ]
        fig.legend(
            ax.collections[: len(values)],
            legend_labels,
            loc="upper left",
            bbox_to_anchor=(0.075, (0.3 + 0.25 * legend_rows) / height),
            ncol=min(5, len(values)),
            frameon=False,
            labelcolor="#c6d0db",
            fontsize=12.5,
            handlelength=1.1,
            handletextpad=0.5,
            columnspacing=1.2,
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        for extension in ("svg", "png"):
            path = output_dir / f"{filename}.{extension}"
            fig.savefig(
                path,
                dpi=160,
                metadata={"Date": None} if extension == "svg" else None,
            )
            if extension == "svg":
                path.write_text("\n".join(line.rstrip() for line in path.read_text().splitlines()) + "\n")
        plt.close(fig)


def regenerate() -> None:
    plotting()  # Fail before downloading if the optional dependency is missing.
    data = collect_history()
    # Finish both renders before replacing any checked-in artifacts.
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=OUTPUT_DIR) as directory:
        temporary = Path(directory)
        for field in ("quality", "types"):
            render_chart(data, field, temporary)
        (temporary / "release-history.json").write_text(
            json.dumps(data, indent=2) + "\n"
        )
        for path in temporary.iterdir():
            path.replace(OUTPUT_DIR / path.name)
    print("Regenerated docs/charts. Commit and push these files to update the README.")
