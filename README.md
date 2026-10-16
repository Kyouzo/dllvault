# Fileshelf — file uploads on Flask + Cloudflare R2

Upload a file (up to 20 MiB), get a shareable download link. The Flask app runs on Render; files live in a **private** Cloudflare R2 bucket, so they survive restarts and redeploys.

```
Browser ──POST /api/upload──▶ Flask (Render) ──put_object──▶ R2 (private bucket)
Browser ──GET /download/<id>─▶ Flask ──302 to 5-min presigned URL──▶ R2 serves the bytes
```

R2 credentials stay on the server. The browser only ever sees `/download/<id>` and a short-lived signed URL for one object.

## Features

- Drag-and-drop or file picker, file name/size/type preview, live progress, cancel
- 20 MiB limit enforced in the browser **and** on the server (Flask `MAX_CONTENT_LENGTH` + exact size check)
- 128-bit random IDs; conditional writes (`If-None-Match: *`) so an object is never overwritten
- Files stored as `application/octet-stream` with `Content-Disposition: attachment` and served from R2's domain, never rendered from the app's origin
- Copy-link button with clipboard feedback; expiry date shown after upload
- One-time delete key per file (only a SHA-256 hash is stored)
- Configurable retention (default 30 days), enforced by the app and, recommended, by an R2 lifecycle rule
- Rate limiting, strict CSP and security headers, JSON errors without internals
- Light/dark theme, keyboard-accessible, visible focus, `aria-live` status, reduced-motion support
- Privacy, Terms and Cookie pages (no cookies are set; no analytics)

## Project layout

```
app.py              Flask app factory, routes, errors, security headers
storage.py          R2 service (boto3) — the only module that talks to storage
utils.py            Filename / MIME / ID sanitising
templates/          base, index, privacy, terms, cookies, error (+ _legal, _macros)
static/css/styles.css  Compiled Tailwind (committed — Render doesn't need Node)
static/js/app.js    Upload UI;  static/js/theme.js  theme toggle
tailwind/input.css  Tailwind source; tailwind.config.js
tests/test_app.py   End-to-end tests against a mocked R2 bucket
render.yaml         Render Blueprint
```

## API

| Method & path | Purpose |
|---|---|
| `GET /` | Upload page |
| `POST /api/upload` | Multipart field `file`. Requires header `X-Requested-With: fetch`. Returns `201 {ok, file{id,name,size,type,uploaded_at,expires_at}, download_url, delete_token}` |
| `GET /api/files/<id>` | Public metadata for a live file |
| `DELETE /api/files/<id>` | Headers `X-Requested-With: fetch`, `X-Delete-Token: <key>` |
| `GET /download/<id>` | 302 to a presigned R2 URL (or the public URL if `R2_PUBLIC_BASE_URL` is set) |
| `GET /health` | `{"status":"healthy"}`. Doesn't call R2, so it's cheap to poll |
| `GET /admin` | Password-gated page listing every uploaded file with delete buttons |

### Command-line uploads

`scripts/upload.py` uploads from any machine with Python 3.8+ (standard library only):

```bash
export FILESHELF_URL=https://your-app.onrender.com
python scripts/upload.py report.pdf photos.zip     # prints link + delete key per file
python scripts/upload.py data.csv --json           # one JSON object per line
python scripts/upload.py --delete <file-id> <delete-key>
```

Uploads are subject to the same rate limit as the web page (`UPLOAD_RATE_LIMIT`).

Errors always look like `{"ok": false, "error": {"code": "...", "message": "..."}}`.

## 1. Set up Cloudflare R2 (needs your account)

1. Cloudflare dashboard → **R2** → **Create bucket** (e.g. `fileshelf-uploads`). Leave public access **off**.
2. **R2 → Manage API tokens → Create API token**: permission **Object Read & Write**, scoped to that bucket only. Copy the **Access Key ID** and **Secret Access Key** (shown once).
3. Your **Account ID** is on the R2 overview page. The endpoint the app uses is `https://<ACCOUNT_ID>.r2.cloudflarestorage.com`.
4. Recommended: bucket → **Settings → Object lifecycle rules** → delete objects with prefix `uploads/` after the same number of days as `RETENTION_DAYS`. The app already refuses expired files and deletes them when requested, but the rule removes files nobody asks for again.
5. No CORS rule is needed: the browser uploads to Flask, not directly to R2, and downloads are plain redirects.

## 2. Run locally

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
cp .env.example .env        # then fill in the R2 values
flask --app app run --debug # http://127.0.0.1:5000
```

Without R2 values the site still renders, but shows "Uploads are unavailable" and the API returns `503`.

CSS changes (only if you edit templates/JS classes or `tailwind/input.css`):

```bash
npm install
npm run build:css    # or: npm run watch:css
```

Commit the regenerated `static/css/styles.css`.

## 3. Deploy to Render

1. Push this folder to a GitHub/GitLab repo. Check `git status` first: `.env` must not appear (it's in `.gitignore`).
2. Render dashboard → **New + → Blueprint** → select the repo. Render reads `render.yaml`.
   (Or **New + → Web Service**, runtime Python, build `pip install -r requirements.txt`, start
   `gunicorn app:app --workers 2 --threads 4 --timeout 60 --bind 0.0.0.0:$PORT`, health check path `/health`.)
3. In the service's **Environment** tab, set:
   - `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET_NAME`
   - `FLASK_SECRET_KEY` (the Blueprint generates one)
   - `OPERATOR_NAME`, `CONTACT_EMAIL`, `LEGAL_LAST_UPDATED` — the legal pages show a yellow `[set …]` marker until these are set
   - Optional: `RETENTION_DAYS`, `DOWNLOAD_URL_TTL`, `UPLOAD_RATE_LIMIT`, `SITE_NAME`, `RATELIMIT_STORAGE_URI`
4. Deploy. Render serves it over HTTPS on `*.onrender.com` (and on any custom domain you add).

**Why R2 at all?** Render's filesystem is ephemeral: anything written to disk disappears on every deploy or restart. This app never stores uploads on local disk beyond the in-flight request; everything goes to R2.

## Admin file listing

`GET /admin` lists **every** file in the bucket (including ones uploaded by the command-line script) with its name, size, upload date, expiry, an Open link, and a Delete button. It's protected by a password:

1. Set `ADMIN_PASSWORD` (Render → Environment, or `.env` locally) to a long, random value. If it's blank, the admin area is disabled and `/admin` returns 404.
2. Set `SESSION_COOKIE_SECURE=1` in production (the Blueprint does this) so the admin login cookie is only sent over HTTPS.
3. Go to `https://your-app.onrender.com/admin`, sign in, and manage files.

Notes:
- Admin delete removes any file immediately — it doesn't need that file's personal delete key.
- The list shows up to `ADMIN_LIST_LIMIT` files (default 2000), newest first.
- Signing in sets one strictly-necessary session cookie (documented in the Cookie Policy); ordinary visitors never get a cookie.
- The admin link isn't shown in the site navigation; browse to `/admin` directly.

## Configuration reference

| Variable | Default | Notes |
|---|---|---|
| `R2_ACCOUNT_ID` / `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` / `R2_BUCKET_NAME` | — | Required for uploads |
| `R2_PUBLIC_BASE_URL` | empty | Only if you *intentionally* attached a public domain to the bucket. Then downloads redirect to `<base>/uploads/<id>` instead of a presigned URL, and anyone who learns that URL can fetch it until deletion |
| `FLASK_SECRET_KEY` | random per process | Set it in production (also signs the admin session) |
| `ADMIN_PASSWORD` | empty | Enables `/admin`. Blank = admin area disabled |
| `ADMIN_LIST_LIMIT` | `2000` | Max files shown on the admin page |
| `SESSION_COOKIE_SECURE` | `0` | Set `1` in production so the admin cookie is HTTPS-only |
| `MAX_UPLOAD_BYTES` | `20971520` (20 MiB) | |
| `RETENTION_DAYS` | `30` | `0` = keep until deleted |
| `DOWNLOAD_URL_TTL` | `300` s | Lifetime of each presigned URL. The `/download/<id>` link itself works until the file expires |
| `UPLOAD_RATE_LIMIT` | `10 per minute;100 per day` | Per client IP |
| `RATELIMIT_STORAGE_URI` | `memory://` | Counters are per worker in memory. Use Redis (e.g. Render Key Value) for accurate limits across workers/instances |
| `SITE_NAME`, `OPERATOR_NAME`, `CONTACT_EMAIL`, `LEGAL_LAST_UPDATED` | | Used in the header and legal pages |

## Security notes

- Credentials are read from environment variables only; nothing secret reaches templates or JS.
- Filenames, MIME types and extensions are treated as untrusted labels. Names are sanitised for display and encoded per RFC 6266 in `Content-Disposition`; the declared type is display-only.
- Uploads are never executed or rendered: stored as `application/octet-stream`, downloaded as attachments, from R2's origin.
- `POST`/`DELETE` require a custom `X-Requested-With` header, which cross-site forms can't send and cross-site scripts can't send without a CORS preflight (which the app doesn't allow).
- CSP `default-src 'self'` with no inline scripts; `nosniff`, `frame-ancestors 'none'`, `no-referrer`, HSTS over HTTPS.
- Unexpected errors are logged server-side; clients get a generic message.
- Nothing is scanned for malware. If you open the service to the public, consider adding scanning and an abuse-report process. Anyone with a link can download that file.
- Before launch, have the Privacy Policy and Terms reviewed against the laws that apply to you; they're sensible starting drafts, not legal advice.

## Testing

```bash
pip install -r requirements-dev.txt
pytest -q
```

The suite replaces R2 with moto's in-memory S3 (same boto3 API) and covers: upload → R2 object (private, attachment, octet-stream) → metadata → `/download` 302 → fetching the presigned URL returns the original bytes; the exact 20 MiB boundary; oversized bodies (413); empty/missing files; malformed and unknown IDs; delete-key checks; expiry (410); no overwrite on ID collision; security headers and no cookies; errors that don't leak internals; and the "storage not configured" mode.

**Manual check against real R2** (needs your credentials): run locally with a filled `.env`, upload a file, open the link in a private window, confirm the object appears under `uploads/` in the bucket, restart the app and confirm the link still works.

## Known MVP limits

- The whole file passes through the Flask worker (fine at 20 MiB). For much larger files, switch to presigned direct-to-R2 uploads, which would need a bucket CORS rule.
- In-memory rate limits reset on restart and are per worker (see `RATELIMIT_STORAGE_URI`).
- On Render's free plan the service sleeps when idle, so the first request can take a while.
