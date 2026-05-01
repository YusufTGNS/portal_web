# Render Deployment

## 1) Repo Push
Projeyi GitHub'a push et.

## 2) Render Web Service
- Render > New + > **Web Service**
- Repo seç
- Runtime: **Python**
- Build Command: `pip install -r requirements.txt`
- Start Command: `gunicorn --workers 2 --threads 8 --bind 0.0.0.0:$PORT app:app`

## 3) Environment Variables
Render dashboard'da ekle:

- `APP_ENV=production`
- `APP_SECRET_KEY=<uzun-random-secret>`
- `DEFAULT_ADMIN_USERNAME=admin`
- `DEFAULT_ADMIN_PASSWORD=<guclu-admin-sifresi>`
- `DEFAULT_USER_INITIAL_PASSWORD=<guclu-baslangic-sifresi>`
- `DB_PATH=/tmp/data.db`
- `UPLOAD_DIR=/tmp/uploads/messages`
- `REMEMBER_ME_DAYS=30`

## 4) First Checks
- `/login` aciliyor mu
- Admin login oluyor mu
- Kullanici olusturma calisiyor mu
- Portal / Feedback / Chat aciliyor mu
