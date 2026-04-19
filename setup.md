# Alankar shop assistant — setup and guide

Django app: product image vector search + Gemini/Gemma chat for customer replies. This file covers install, run, features, and improvement ideas.

---

## Prerequisites

- Python 3.11+ (tested with 3.13 on this project)
- Google AI (Gemini) API key: https://aistudio.google.com/apikey

---

## First-time setup

```powershell
cd d:\vs\Django\alankar-utsho-vector
python -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt
```

### Environment

Create `.env` in the project root (optional if you only use DB-stored keys):

```env
GEMINI_API_KEY=your_key_here
```

Optional logging:

```env
SHOPCHAT_LOG_LEVEL=DEBUG
```

### Database and admin

```powershell
.\venv\Scripts\python.exe manage.py migrate
.\venv\Scripts\python.exe manage.py createsuperuser
```

On first migrate, an empty credential table may import `GEMINI_API_KEY` once (see migration `0004`).

---

## Run the server

```powershell
.\venv\Scripts\activate
.\venv\Scripts\python.exe manage.py runserver
```

| URL | Purpose |
|-----|---------|
| http://127.0.0.1:8000/ | Chat (messenger UI) |
| http://127.0.0.1:8000/settings/ | System prompt, embedding options, API keys, product catalog |
| http://127.0.0.1:8000/admin/ | Django admin |

---

## What the system does today

### Chat

- Send **text** and/or **images** (multiple files per part). The web UI **queues** each Send and waits **20 seconds** after the last queued part (or **Send now**) then posts **one** API request with all text/images merged. Tune `CHAT_BATCH_IDLE_MS` and `MAX_CHAT_IMAGES` in `shopchat/views.py`. When you switch to an external Messenger API, reuse the same batching idea server-side or in the webhook layer.
- **Last 20 messages** per browser session stored for multi-turn context to the model.
- Assistant replies: if the model returns Markdown images `![label](url)`, the UI renders **inline images** (catalog URLs).

### Catalog and retrieval

- Upload **product images** (+ name/notes) on Settings.
- Each image is embedded with **`gemini-embedding-2-preview`** (fixed in code); vectors stored in SQLite (`JSONField`).
- **User photo:** embed → cosine similarity vs catalog → top-K matches → text block injected into the prompt.
- **Text only** (e.g. asking for a product photo): user message is **text-embedded** with the same model (multimodal space) → same similarity search → catalog URLs in context.

### Models and API keys

- Chat rotates **`gemma-4-26b-a4b-it`** and **`gemma-4-31b-it`** per enabled API key, then round-robins across all `(key, model)` slots.
- Embeddings: same embedding model; **API keys** rotate separately (`embed_rr_seq`).
- Add multiple **`GeminiApiCredential`** rows (Settings or admin) for separate Google projects/accounts.

### Assistant rules (code + settings)

- Editable **system prompt** in Settings.
- Extra rules appended in code: use only **`image_url` values from retrieval** for photos; no invented bit.ly links; valid Markdown with closing `)`.

### Logging

- Logger `shopchat` → console (see `config/settings.py` `LOGGING`).
- Full API keys are **not** logged (only a short suffix hint).

---

## Main code locations

| Path | Role |
|------|------|
| `config/settings.py` | Django, `LOGGING`, media, `.env` |
| `shopchat/models.py` | `AppSettings`, `GeminiApiCredential`, `ProductImage`, `ChatMessage` |
| `shopchat/services.py` | Embeddings, similarity, `generate_chat_reply`, round-robin |
| `shopchat/views.py` | Pages and JSON APIs |
| `shopchat/signals.py` | Product save → embed on commit |
| `templates/shopchat/` | Chat and settings |
| `static/shopchat/app.css` | Styles |
| `media/` | Uploaded files (not in git; see `.gitignore`) |

---

## What you can improve next

### Scale

- Replace linear scan over all products with **pgvector**, **FAISS**, or a hosted vector index for large catalogs.

### Cost / quota

- Run **text embedding** only when the message looks like an image/product request (intent detection), not on every text message.
- Background **embedding jobs** (Celery/RQ) for catalog uploads.

### Security / production

- `DEBUG=False`, strong `SECRET_KEY`, `ALLOWED_HOSTS`, HTTPS.
- Encrypt API keys at rest or use a secret manager; never commit `.env`.

### Search quality

- Hybrid **keyword** (name/notes) + vector scores.
- Stable **SKU / product id** in the model for exact matches.
- Server-side **post-process** of assistant replies to force image URLs from retrieval only.

### UX

- Typing indicator, streaming responses, user accounts instead of anonymous session-only chat.

### Engineering

- Docker, CI, automated tests (`pytest`).

---

## Troubleshooting

| Issue | Check |
|-------|--------|
| No API keys | Settings → Gemini keys, or `GEMINI_API_KEY` in `.env` when DB has no enabled keys |
| 429 errors | Free-tier limits; add keys, wait, or enable billing |
| Image not showing in chat | Model must output valid `![alt](full_url)`; URL must include real file path/extension; hard-refresh CSS |
| Wrong similarity after model change | Re-save or re-upload products so embeddings are rebuilt |

---

## Stack

- Django 6, SQLite, `google-genai`, Pillow, NumPy, `python-dotenv`.

Follow Google AI pricing and terms: https://ai.google.dev/gemini-api/docs/pricing

echo "# ai" >> README.md
git init
git add README.md
git commit -m "first commit"
git branch -M main
git remote add origin https://github.com/smsohagbd/ai.git
git push -u origin main

git add .
git commit -m "Initial project files added"
git push origin main



git pull
sudo systemctl daemon-reload
sudo systemctl restart ai.service