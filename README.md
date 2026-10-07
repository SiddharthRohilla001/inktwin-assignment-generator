# InkTwin Handwriting Assignment Generator

InkTwin analyzes a handwriting sample and assignment prompt, then generates original page-by-page content and renders a downloadable PDF in the browser. It includes a static website and a Python FastAPI backend.

## Assignments

Verified accounts can generate assignments without a payment or assignment quota. Email verification requires SMTP configuration. Generation uses a vision-capable OpenAI-compatible API and fails with an explicit error if the backend provider is not configured.

The handwriting sample guides visible style analysis; this app does not produce an exact copy of a person's handwriting or create a font. The sample is sent to the configured vision provider and is not stored by the app.

## Run locally

Requires Python 3.10 or newer.

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Configure `LLM_API_KEY` and `LLM_MODEL` in `.env`. The model must support image input and JSON-object responses. Configure SMTP values to enable email verification. For local use, the frontend defaults to `http://localhost:8000/api/v1`.

```powershell
uvicorn handwriting_agent.api:app --reload --env-file .env
python -m http.server 3000
```

Open `http://localhost:3000`. Backend health and interactive API documentation are available at `http://localhost:8000/health` and `http://localhost:8000/docs`.

## Deploy the website with GitHub Pages

This repository includes a GitHub Actions workflow that publishes the static frontend (`index.html`, `config.js`, and the logo under `assets/`) to GitHub Pages on each push to `main`.

1. Create a GitHub repository and push this project to its `main` branch.
2. In the repository, open **Settings → Pages** and select **GitHub Actions** as the build and deployment source.
3. The workflow will publish the site at `https://YOUR-USERNAME.github.io/YOUR-REPOSITORY/`.

GitHub Pages only hosts static files; it does **not** run the Python API. To generate assignments, deploy the `handwriting_agent` backend separately to a Python web host such as Render. Set `LLM_API_KEY`, `LLM_MODEL`, `AUTH_SECRET_KEY`, and SMTP settings in that backend's private environment, and use a persistent database (for example, Turso) for accounts.

After the backend is deployed, set `window.INKTWIN_API_BASE` in `config.js` to its API URL ending in `/api/v1`, for example:

```js
window.INKTWIN_API_BASE = 'https://your-backend.onrender.com/api/v1';
```

Add the exact GitHub Pages origin (for example, `https://YOUR-USERNAME.github.io`) to the backend's comma-separated `CORS_ORIGINS`, redeploy the backend, and trigger the Pages workflow again. Do not put provider keys, auth secrets, SMTP passwords, or database tokens in `config.js` or any frontend file.

## Environment

| Setting | Purpose |
|---|---|
| `LLM_API_KEY` | Secret API key for the vision-capable generation provider |
| `LLM_BASE_URL` | OpenAI-compatible API root; defaults to `https://api.openai.com/v1` |
| `LLM_MODEL` | Vision-capable model name |
| `AUTH_SECRET_KEY` | Random secret of at least 32 characters for account tokens and verification codes |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` | Email verification delivery |
| `DATABASE_PATH` | Local SQLite database path; defaults to `handwriting_agent/likho.sqlite3` |
| `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN` | Optional remote Turso database; set both or leave both blank |
| `CORS_ORIGINS` | Comma-separated browser origins allowed to call the backend |
| `MAX_UPLOAD_MB` | Maximum handwriting image upload size; defaults to 10 MB |

Keep `.env` and all production credentials out of source control. See `.env.example` for the complete list of settings.

## API

- `POST /api/v1/auth/register`, `POST /api/v1/auth/verify`, and `POST /api/v1/auth/login` manage verified accounts.
- `GET /api/v1/auth/me` returns account information and generated-assignment count.
- `POST /api/v1/pages` accepts an authenticated multipart request with a handwriting image, prompt, language, and page count.
- `GET /health` reports whether the generation provider is configured.

Run the automated backend tests with:

```powershell
python -m unittest discover -s tests -v
```
