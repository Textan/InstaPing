# InstaPing

InstaPing is a single-folder Railway worker that monitors one Instagram account and sends Bark alerts to your iPhone. It uses `instagrapi`, stores downloaded activity in encrypted bundles, and can upload those bundles to a Dropbox app folder for easy iPhone access.

> This project uses Instagram's unofficial private API. Use an account you control, keep polling conservative, and expect Instagram to sometimes require challenge or 2FA verification.

## Features

- Alerts when the target account posts feed media, Reels, Stories, or Notes.
- Alerts when the target account views your active Story.
- Alerts when the target account likes or comments on your recent posts/Reels.
- Emits a possible repeat Story view signal when the target account moves upward in your Story viewer list.
- Sends iPhone pushes through Bark.
- Downloads detected activity and stores it as encrypted `.zip.fernet` bundles.
- Uploads encrypted bundles and `.sha256` checksum files to Dropbox.
- Includes Railway healthcheck and restart config.

## How Repeat Story View Detection Works

Instagram does not expose a reliable "viewed your Story N times" count through `instagrapi`. InstaPing stores the target account's position in your Story viewer list. If they later move closer to the top, InstaPing sends a "possible repeat view" alert.

Treat this as a useful signal, not proof of an exact view count.

## Deploy On Railway

1. Push this repo to GitHub.
2. Create a Railway service from the repo.
3. Attach a Railway volume mounted at `/data`.
4. Add the environment variables below.
5. Deploy.

Railway uses:

- `main.py` for the worker and helper commands.
- `Dockerfile` for the image.
- `railway.json` for `/health`, restart policy, and deploy behavior.

## Required Environment Variables

| Variable | Description |
| --- | --- |
| `INSTAGRAM_USERNAME` | Instagram username for the monitoring account. |
| `INSTAGRAM_PASSWORD` | Instagram password. |
| `TARGET_USERNAME` | Account to monitor, without `@`. |
| `BARK_URL` | Bark endpoint, for example `https://api.day.app/YOUR_KEY`. |
| `ENCRYPTION_KEY` | Fernet key used to encrypt saved bundles. |

Generate `ENCRYPTION_KEY`:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## Recommended Environment Variables

| Variable | Default | Description |
| --- | --- | --- |
| `DATA_DIR` | `/data` | Persistent state, session, downloads, and encrypted bundles. |
| `POLL_INTERVAL_SECONDS` | `180` | Delay between polling cycles. |
| `INITIAL_BACKFILL` | `false` | If `false`, old activity is marked seen on first boot. |
| `MAX_FEED_ITEMS` | `12` | Target feed posts to scan. |
| `MAX_REELS` | `12` | Target Reels to scan. |
| `MAX_STORIES` | `20` | Stories to scan. |
| `MAX_OWN_FEED_ITEMS` | `12` | Your posts checked for target likes/comments. |
| `MAX_OWN_REELS` | `12` | Your Reels checked for target likes/comments. |
| `MAX_STORY_VIEWERS` | `200` | Story viewers fetched per active Story. |
| `MAX_MEDIA_LIKERS` | `200` | Likers scanned per recent post/Reel. |
| `MAX_MEDIA_COMMENTS` | `50` | Comments scanned per recent post/Reel. |
| `DRY_RUN` | `false` | Log alerts/uploads without sending them. |

## Dropbox Setup

Dropbox is used because it has a free plan, a strong iPhone app, iOS Files integration, and app-folder permissions.

1. Create or use a free Dropbox account.
2. Go to [Dropbox App Console](https://www.dropbox.com/developers/apps).
3. Create an app with `Scoped access` and `App folder` access.
4. Add scopes: `files.content.write` and `files.metadata.write`.
5. Install dependencies locally and generate a refresh token:

```bash
pip install -r requirements.txt
python main.py dropbox-auth
```

6. Add these Railway variables:

| Variable | Description |
| --- | --- |
| `DROPBOX_APP_KEY` | Dropbox app key. |
| `DROPBOX_APP_SECRET` | Dropbox app secret. |
| `DROPBOX_REFRESH_TOKEN` | Long-lived refresh token from `python main.py dropbox-auth`. |
| `DROPBOX_REMOTE_DIR` | Optional. Defaults to `InstaPing`. |

Uploaded files appear under:

```text
Dropbox/Apps/YOUR_APP_NAME/InstaPing/YYYY-MM-DD/
```

The files are encrypted before upload. Dropbox and iOS can browse/download them, but cannot read the contents without `ENCRYPTION_KEY`.

## Bark Setup

Install Bark on your iPhone, copy your Bark server URL/key, and set:

```text
BARK_URL=https://api.day.app/YOUR_KEY
```

## Local Development

Create `.env` from [.env.example](./.env.example), then run:

```bash
pip install -r requirements.txt
python main.py
```

Docker test:

```bash
docker build -t instaping .
docker run --env-file .env -v instaping-data:/data instaping
```

## Decrypt A Bundle

Download a `.zip.fernet` file from Dropbox, then run:

```bash
python main.py decrypt path/to/activity.zip.fernet --out decrypted
```

The script uses `ENCRYPTION_KEY` from your environment unless you pass `--key`.

## First Login And 2FA

Instagram may reject a first Railway login because the IP/device is new. If that happens:

1. Run the app locally once from a trusted network.
2. Complete any Instagram challenge.
3. Copy the generated `instagram-session.json` into the Railway `/data` volume.
4. Redeploy.

## Uptime Notes

InstaPing is tuned for high availability, but no unofficial Instagram monitor can guarantee 100% uptime. Railway, Instagram, Bark, Dropbox, and login challenges can all fail.

The project mitigates this with:

- Railway `/health` endpoint.
- Railway `ALWAYS` restart policy.
- Persistent `/data` state and Instagram session.
- Bark retry logic.
- Dropbox upload retry logic.
- Local encrypted bundle retention when Dropbox upload fails.
- Poll failures logged without killing the process.

Keep `POLL_INTERVAL_SECONDS` at `180` or higher to reduce throttling and challenge risk.

## Security

- Use a dedicated Instagram account if possible.
- Use Dropbox app-folder access, not full Dropbox access.
- Keep `ENCRYPTION_KEY`, Instagram credentials, and Dropbox tokens in Railway secrets only.
- Do not commit `.env`, session files, `/data`, or downloaded bundles.
- Encrypted bundles are only as safe as your `ENCRYPTION_KEY`.
