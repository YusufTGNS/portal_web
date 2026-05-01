# Netlify Deployment Checklist

## 1) Build Settings
- Repository root: project root
- Build command: (boş bırakılabilir)
- Publish directory: (boş bırakılabilir)
- Functions directory: `netlify/functions`

## 2) Environment Variables (Netlify UI)
Set these in **Site settings > Environment variables**:

- `APP_ENV=production`
- `APP_SECRET_KEY=<uzun-random-secret>`
- `DEFAULT_ADMIN_USERNAME=admin`
- `DEFAULT_ADMIN_PASSWORD=<guclu-admin-sifresi>`
- `DEFAULT_USER_INITIAL_PASSWORD=<baslangic-sifresi>`
- `DB_PATH=/tmp/data.db`
- `UPLOAD_DIR=/tmp/uploads/messages`

## 3) Redirect / Serverless Entry
`netlify.toml` all requests route eder:
- `/* -> /.netlify/functions/server`

## 4) Final Runtime Notes
- Bu proje Flask + SQLite + dosya eki kullanır.
- Netlify serverless ortamında `/tmp` geçicidir; restart/cold start sonrası veri kalıcı değildir.
- WebSocket tabanlı canlı chat davranışı serverless ortamda sınırlı olabilir.

Kalıcı veritabanı ve tam gerçek zamanlı chat için:
- DB: Postgres (Neon/Supabase/Railway)
- Realtime backend: Render/Railway/Fly (persistent process)

## 5) Quick Smoke Test
Deploy sonrası kontrol:
- `/login` açılıyor mu
- Admin login başarılı mı
- Kullanıcı oluşturma çalışıyor mu
- Portal linkleri görüntüleniyor mu
- Widget ekleme çalışıyor mu
