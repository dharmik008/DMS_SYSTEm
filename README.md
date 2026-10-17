# Caryanams DMS — Unified Platform
**Dealer Management System** + **Caryanams Studio** (AI Background Removal)

## 🚀 Quick Start (one click)

| Your computer | Do this |
|---|---|
| **Windows** | Double-click **`start.bat`** |
| **Mac** | Double-click **`start.command`** (first time: right-click → Open) |
| **Linux** | Run **`./start.sh`** |

That's it. The first start installs everything (a few minutes); after that
it starts in seconds. Your browser opens automatically.

- Admin panel: `http://localhost:5000/admin/login` — username `admin`, password `admin123`
- Stop: press **Ctrl+C** in the window, or close it

**Needs:** Python 3.9 – 3.12 (3.12 recommended:
https://www.python.org/downloads/release/python-3128/). The start file finds
it for you; if Python is missing it tells you where to get it.

**Database:** with no `DATABASE_URL` in `.env`, a local SQLite file is used,
so nothing needs installing. If `.env` points at PostgreSQL and it isn't
running, the start file asks whether to use SQLite for now instead.

**Options** (add after the start file or `python run.py`):
`--sqlite` · `--port 8080` · `--no-browser` · `--reload` (auto-restart on
code edits) · `--setup-only` · `--reinstall`

<details><summary>Manual start (advanced)</summary>

```bash
pip install -r requirements.txt
python run.py
```
</details>

---

## 📁 Project Structure

```
├── app.py                  Main Flask app (factory pattern)
├── requirements.txt        All dependencies
│
├── auth/routes.py          Blueprint: /auth
├── dealer/routes.py        Blueprint: /dealer
├── user/routes.py          Blueprint: /
├── background/             ← NEW: Caryanams Studio
│   ├── __init__.py
│   ├── routes.py           Blueprint: /studio
│   └── utils.py            AI image processing logic
│
├── templates/
│   ├── dealer/
│   │   ├── base.html       Sidebar (Studio link added)
│   │   ├── dashboard.html  Dashboard (Studio CTA added)
│   │   └── vehicle_form.html (Studio button added)
│   └── background/
│       └── remove.html     ← NEW: Studio UI
│
└── static/
    ├── images/uploads/     DMS vehicle images
    ├── processed/          ← NEW: Studio output images
    └── custom_bgs/         ← NEW: ba1_studio.jpg + custom BGs
```

---

## 🎨 Studio Routes

| URL | Description |
|-----|-------------|
| `/studio/` | Studio main page |
| `/studio/api/upload` | Upload car images (POST) |
| `/studio/api/remove-bg/<id>` | Remove background (POST) |
| `/studio/api/remove-bg-batch` | Batch remove (POST) |
| `/studio/api/apply-bg/<id>` | Apply background (POST) |
| `/studio/api/apply-to-all` | Apply BG to all images (POST) |
| `/studio/api/upload-bg-image` | Upload custom BG (POST) |
| `/studio/api/download/<id>` | Download processed image |
| `/studio/api/gallery` | Gallery list (GET) |
| `/studio/api/car-360/<id>` | 360° frame data (GET) |

---

## 🧩 Dependencies

### DMS Core
- Flask, Flask-SQLAlchemy, Flask-Login

### Caryanams Studio
- **rembg** — AI background removal (u2net / isnet models)
- **opencv-python-headless** — GrabCut fallback
- **Pillow** — Image compositing, watermark, shadow
- **numpy** — Pixel-level operations

---

## 📌 Notes

- Both modules share ONE SQLite database (`Caryanams.db`)
- Studio uses separate tables: `studio_image`, `studio_credit_log`
- `UPLOAD_FOLDER` from DMS config is reused for original uploads
- Processed images go to `static/processed/`
- Place `ba1_studio.jpg` in `static/custom_bgs/` for studio background
- First run will download AI models (~170MB for u2net)
