"""Command-line entry point for local and hosted review servers."""

import argparse
import json
from pathlib import Path

import uvicorn

from .app import create_app
from .catalog import ensure_catalog
from .hosted import HostedStore


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Silicone Shadows UI.")
    parser.add_argument("--input", type=Path)
    parser.add_argument("--work", type=Path)
    parser.add_argument("--products", type=Path)
    parser.add_argument(
        "--catalog-source", type=Path, default=Path("catalog_source.json")
    )
    parser.add_argument("--dataset", type=Path, default=Path("dataset"))
    parser.add_argument("--hosted", action="store_true")
    parser.add_argument(
        "--database", type=Path, default=Path(".local/hosted/state.sqlite3")
    )
    parser.add_argument("--pending", type=Path, default=Path(".local/hosted/pending"))
    parser.add_argument("--create-invite", metavar="NAME")
    parser.add_argument("--revoke-sessions", metavar="NAME")
    parser.add_argument("--reviewer", action="store_true")
    parser.add_argument(
        "--secure-cookies",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="require HTTPS for hosted session cookies (default: true)",
    )
    parser.add_argument("--image-base-url", default="https://fantasytoybox.net/")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--trusted-host",
        action="append",
        help="accepted Host value in hosted mode; repeat for multiple names",
    )
    args = parser.parse_args()

    if args.create_invite:
        store = HostedStore(args.database)
        token = store.create_invite(args.create_invite, args.reviewer)
        print(f"Invite path: /invite/{token}")
        return
    if args.revoke_sessions:
        count = HostedStore(args.database).revoke_sessions(args.revoke_sessions)
        print(f"Revoked {count} session(s) for {args.revoke_sessions}")
        return

    args.input = args.input or Path(
        ".local/hosted/images" if args.hosted else ".local/images"
    )
    args.work = args.work or Path(
        ".local/hosted/work" if args.hosted else ".local/work"
    )
    args.input.mkdir(parents=True, exist_ok=True)
    if args.products is None:
        try:
            args.products = ensure_catalog(args.catalog_source)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            parser.error(str(error))
    elif not args.products.is_file():
        parser.error(f"product catalog does not exist: {args.products}")
    print(f"Silicone Shadows: http://{args.host}:{args.port}")
    uvicorn.run(
        create_app(
            args.input,
            args.work,
            args.products,
            args.image_base_url,
            args.dataset,
            HostedStore(args.database) if args.hosted else None,
            args.pending if args.hosted else None,
            args.secure_cookies,
            args.trusted_host,
        ),
        host=args.host,
        port=args.port,
        access_log=not args.hosted,
        proxy_headers=args.hosted,
        forwarded_allow_ips="127.0.0.1" if args.hosted else None,
        server_header=not args.hosted,
    )


if __name__ == "__main__":
    main()
