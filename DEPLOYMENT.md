# Deployment (Gunicorn + Nginx)

## Prerequisites

- Python 3.11+
- A virtual environment created at `/srv/lumyseite/.venv`
- Nginx installed on the server

## Gunicorn

1. Install dependencies inside the virtual environment:

   ```bash
   /srv/lumyseite/.venv/bin/pip install -r requirements.txt gunicorn
   ```

2. Run Gunicorn directly (example):

   ```bash
   /srv/lumyseite/.venv/bin/gunicorn -c gunicorn.conf.py wsgi:application
   ```

3. Optional environment variables for tuning:

   - `GUNICORN_BIND` (default: `0.0.0.0:8073`)
   - `GUNICORN_WORKERS`
   - `GUNICORN_THREADS`
   - `GUNICORN_TIMEOUT`
   - `GUNICORN_KEEPALIVE`
   - `GUNICORN_LOGLEVEL`
   - `GUNICORN_ACCESS_LOG`
   - `GUNICORN_ERROR_LOG`

## Systemd (optional)

1. Copy the example unit:

   ```bash
   sudo cp deploy/gunicorn.service /etc/systemd/system/lumyseite.service
   ```

2. Reload and start:

   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now lumyseite.service
   ```

## Nginx

1. Copy the example config and adjust paths/server_name:

   ```bash
   sudo cp deploy/nginx.conf /etc/nginx/sites-available/lumyseite.conf
   sudo ln -s /etc/nginx/sites-available/lumyseite.conf /etc/nginx/sites-enabled/
   ```

2. Test and reload Nginx:

   ```bash
   sudo nginx -t
   sudo systemctl reload nginx
   ```

## Notes

- Static assets are served from `/srv/lumyseite/static` in the provided Nginx config.
- Update `/srv/lumyseite` paths if you deploy elsewhere.
