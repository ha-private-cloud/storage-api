# storage-api

The storage backend for ClusterKeep. It keeps every user's files and folders, encrypted with a key only that user can unlock. It's only reachable inside the cluster, and storage-ui and clusterkeep-ui are the only things that call it.

## What it does

- Gives each user their own encrypted storage the first time they log in after it's set up, with Photos, Videos, Music and Documents folders.
- Each user gets a random key. It's never stored as it is. One copy is locked with their password and another with a recovery code that's shown to them once. Changing their password re-locks the key, and if an admin resets it they unlock it again with the recovery code. Losing both the password and the code means the files can't be recovered.
- Encrypts file contents and file and folder names with AES-256-GCM. Contents are split into 1 MiB pieces, each with its own random nonce, so a video can be played from the middle without decrypting all of it.
- Keeps the unlocked key in Redis for as long as the person is logged in, sealed under a separate secret, and drops it when they log out.
- Takes uploads in 8 MiB chunks so large files never sit in memory and an upload can carry on after a failed chunk.
- Serves files with `Range` support so videos and music can seek.
- Creates thumbnails for photos with Pillow and posters for videos with ffmpeg, all in memory so nothing unencrypted touches the disk.
- Limits each user to a quota, 20 GB by default.
- Keeps deleted items in a bin for 30 days before removing them for good.
- Only lets each default folder, and the folders inside it, hold its own kind of file. Photos only accepts images, Videos only video, Music only audio and Documents only documents.
- Searches by name.
- Only shows images, video and audio in the browser. Every other type is downloaded instead, and every file response is sandboxed, so an uploaded web page can't run in storage-ui.

## How requests are checked

Every request needs two things.

- storage-api's service token as a bearer token. Only storage-ui and clusterkeep-ui hold it.
- The user's `ck_sso` session cookie, which storage-api checks with auth-api's `/session`. Answers are cached for 30 seconds so seeking through a video doesn't flood auth-api.

If the session is valid but the user hasn't unlocked their storage in this session, the answer is 423 so storage-ui can ask them to log in again.

## Endpoints

### Health

| Method | Path | What it does |
|---|---|---|
| GET | `/healthz` | Kubernetes health check |
| GET | `/readyz` | Kubernetes readiness check, passes when Postgres and Redis are reachable |

### Vault, for clusterkeep-ui

| Method | Path | What it does |
|---|---|---|
| GET | `/api/v1/vault` | Whether the user's storage exists and is unlocked |
| POST | `/api/v1/vault/unlock` | Unlock with the password. Creates the storage and returns a recovery code the first time, or asks for the recovery code if the password has been reset |
| POST | `/api/v1/vault/recover` | Unlock with the recovery code and re-lock the key with the current password |
| POST | `/api/v1/vault/lock` | Drop the unlocked key at log out |

### Files and folders

| Method | Path | What it does |
|---|---|---|
| GET | `/api/v1/usage` | How much is used and the quota |
| GET | `/api/v1/folders` | List a folder. `parent_id` picks the folder, otherwise the top level |
| GET | `/api/v1/sections/{section}` | List the Photos, Videos, Music or Documents folder |
| GET | `/api/v1/folder-tree` | Every folder with its path, for moving things |
| GET | `/api/v1/search` | Search by name with `q` |
| POST | `/api/v1/folders` | Create a folder |
| PATCH | `/api/v1/nodes/{node_id}` | Rename or move a file or folder |
| DELETE | `/api/v1/nodes/{node_id}` | Move a file or folder to the bin |
| GET | `/api/v1/trash` | List the bin |
| POST | `/api/v1/trash/{node_id}/restore` | Restore from the bin |
| DELETE | `/api/v1/trash/{node_id}` | Delete for good |
| GET | `/api/v1/files/{node_id}/content` | Download or stream a file. `download=1` forces a download |
| GET | `/api/v1/files/{node_id}/thumbnail` | A file's thumbnail |

### Uploads

| Method | Path | What it does |
|---|---|---|
| POST | `/api/v1/uploads` | Start an upload. Checks the name, the folder's file type rule and the quota |
| GET | `/api/v1/uploads/{upload_id}` | Which chunks have arrived |
| PUT | `/api/v1/uploads/{upload_id}/chunks/{index}` | Send one chunk |
| POST | `/api/v1/uploads/{upload_id}/complete` | Finish the upload and make the file |
| DELETE | `/api/v1/uploads/{upload_id}` | Cancel an upload |

The interactive API docs are at `/docs` everywhere except production.

## Technologies

- Python 3.13 and FastAPI, served by gunicorn with Uvicorn workers
- SQLAlchemy with asyncpg and Postgres for the folder tree, encrypted names, quotas and locked keys
- Redis for unlocked keys
- cryptography for AES-256-GCM and HKDF, and argon2-cffi to turn passwords and recovery codes into keys
- Pillow and ffmpeg for thumbnails
- httpx for the session checks against auth-api
- pytest with httpx and SQLite for the tests
- uv for dependencies, Docker for the image, Helm and OpenTofu for deployment

## Configuration

Every setting comes from an environment variable starting with `STORAGE_API_`. The main ones are listed below.

| Variable | What it's for |
|---|---|
| `STORAGE_API_DATABASE_URL` | Postgres connection |
| `STORAGE_API_REDIS_URL` | Redis connection |
| `STORAGE_API_DATA_DIR` | Where encrypted files are kept |
| `STORAGE_API_AUTH_API_INTERNAL_URL` | auth-api's address inside the cluster |
| `STORAGE_API_SERVICE_TOKEN` | Token storage-ui and clusterkeep-ui must present |
| `STORAGE_API_KEY_PEPPER` | Secret mixed into every key derivation |
| `STORAGE_API_KEY_CACHE_SECRET` | Secret that seals unlocked keys in Redis |
| `STORAGE_API_DEFAULT_QUOTA_BYTES` | Quota for new users |

The key pepper must never change. If it's lost or replaced, nobody's storage can be unlocked again, which is why OpenTofu refuses to destroy it.

## Running locally

The easiest way is [common-dev-env](https://github.com/LandRegistry/common-dev-env) with the [dev-env-config](https://github.com/ha-private-cloud/dev-env-config) repository, set up as the [organisation README](https://github.com/ha-private-cloud/.github/blob/main/profile/README.md#running-it-locally) describes. That runs it on http://localhost:9084 with its own Redis, next to auth-api, storage-ui and clusterkeep-ui. The commands below run the tests.

```sh
uv sync
uv run pytest
```

## Deployment

The GitHub workflows come from the shared `pipelines` repo.

- Pushes to `development` and `preview` deploy to dev and preview.
- Pushing a `vX.Y.Z` tag builds the release image, and running the release workflow with that tag deploys it to production.

Each environment runs in its own namespace with its own Postgres and Redis. Nothing routes to it from outside the cluster. Every user's files share one NFS volume, kept apart by their keys. The volume is protected from being destroyed by OpenTofu. The Helm chart is in `charts/storage-api` and the OpenTofu config is in `tofu/`.
