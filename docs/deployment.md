# Hosted deployment

Hosted mode is designed for one application process and a persistent local
disk. SQLite stores invite accounts, hashed sessions, claims and submissions;
images and dataset artifacts remain ordinary files. Local mode is unaffected.

## Accounts and invitations

Create a contributor invitation:

```sh
.venv/bin/python -m server.cli --create-invite alice
```

Add `--reviewer` for an account that can moderate submissions. Invitations are
one-use, expire after seven days, and are accepted by opening the generated
`/invite/...` path on the hosted site.

Revoke a contributor's active sessions with:

```sh
.venv/bin/python -m server.cli --revoke-sessions alice
```

## Linux service and nginx

Clone the application outside `/home`, for example at
`/opt/silicone-shadows`, install its dependencies, and run:

```sh
sudo deploy/setup-service-user.sh /opt/silicone-shadows
```

This creates a non-login `silicone-shadows` operating-system account and seeds
private mutable state under `/var/lib/silicone-shadows/`. The Git checkout stays
read-only except for its ignored catalog cache.

Copy and customize:

- `deploy/silicone-shadows.service.example`
- `deploy/nginx.conf.example`

Replace `APP_DIR` and `DOMAIN`. The nginx template expects an existing
Let's Encrypt certificate under `/etc/letsencrypt/live/DOMAIN/`. Validate the
nginx configuration before enabling either service.

The service listens only on `127.0.0.1:8000`; nginx terminates HTTPS and proxies
the public subdomain to it. The template enforces the intended host, limits
request size and guest traffic, disables access logging, and does not buffer
guest uploads to disk.

Never use `--no-secure-cookies` for a public deployment. That option exists
only for direct local HTTP testing.

## Storage and operation

Run one hosted process; SQLite and the filesystem intentionally have no
multi-instance coordination. Back up `state.sqlite3`, `pending/`, and `dataset/`
together. Copy reviewed dataset changes from `/var/lib/silicone-shadows/dataset/`
into a maintainer checkout before committing or releasing them.

Guest processing allows five rembg jobs per network address per ten minutes,
one live job per address, and one global processing worker. Archive creation
requires a short-lived, single-use proof from a completed rembg job.

Guest uploads may briefly use an operating-system temporary file while FastAPI
parses the request. The file is closed after its bounded read and is not added
to SQLite or persistent application storage. Authenticated independent-product
submissions retain their source and finished artifacts for moderation.

Fantasy Toybox currently omits part of its new Let's Encrypt chain. The app
adds the official bundled ISRG Root YE certificate to Python's normal trust
store while retaining certificate and hostname verification for catalog and
image downloads.
