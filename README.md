# Karaoke MIDI Parser — Railway Deploy

Flask + mido MIDI parser (v7 "Full Expression") for the Roblox karaoke game.
One push to GitHub → Railway auto-builds and runs it, and gives you a public domain.

## Files
| File | Purpose |
|------|---------|
| `app.py` | The parser (Flask app, exposes `app`, reads `$PORT`). |
| `requirements.txt` | Python deps (Flask, mido, requests, gunicorn). |
| `Procfile` | Start command for the web server (gunicorn). |
| `railway.json` | Explicit Railway build/deploy config (backup for Procfile). |
| `runtime.txt` | Pins Python 3.11.9. |
| `.gitignore` | Ignore caches/env. |

## Deploy steps (from zero)
1. Put these files at the **root** of a GitHub repo (not in a subfolder).
   ```bash
   git init
   git add .
   git commit -m "MIDI parser v7"
   git branch -M main
   git remote add origin https://github.com/<you>/<repo>.git
   git push -u origin main
   ```
2. Railway → **New Project** → **Deploy from GitHub repo** → pick this repo.
3. Railway auto-detects Python (Nixpacks), installs `requirements.txt`, and runs the `Procfile` command. No manual config needed.
4. Open the service → **Settings → Networking → Generate Domain**. Copy the `https://<name>.up.railway.app` URL.
5. In Roblox, point your parser URL to `https://<name>.up.railway.app/parse-midi` (same as before, just the new domain).

## Verify it's live
- `GET  https://<domain>/health`  →  `{"status":"ok","version":"7.0"}`
- `GET  https://<domain>/`        →  service info
- `POST https://<domain>/parse-midi`  body `{"url":"<midi url>","name":"Song"}`  → parsed JSON

```bash
# quick smoke test
curl https://<domain>/health
curl -X POST https://<domain>/parse-midi \
  -H "Content-Type: application/json" \
  -d '{"url":"https://example.com/song.mid","name":"Test"}'
```

## Monthly account rotation
Since you rotate Railway accounts monthly: keep this repo on GitHub, and each month just
**Deploy from GitHub repo** again on the new account → Generate Domain → update the one URL in Roblox.
Nothing else changes.

## Notes
- `mido` file parsing is pure Python — no system packages / no `python-rtmidi` needed.
- Wire format stays `version: 2` (backward-compatible with the current Roblox client).
- No environment variables are required. `$PORT` is provided by Railway automatically.
