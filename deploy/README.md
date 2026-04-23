# Deploy ALREADY backend on Ubuntu (systemd)

## 1. Install the app and dependencies

```bash
# Clone or copy the project (e.g. under /var/www)
sudo mkdir -p /var/www
sudo git clone <your-repo> /var/www/alreadyapp-backend
# Or: sudo cp -r /path/to/alreadyapp-backend /var/www/alreadyapp-backend

cd /var/www/alreadyapp-backend

# Create virtualenv and install dependencies
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Create .env with your keys (ELEVENLABS_API_KEY, SUPABASE_*, JWT_SECRET, etc.)
cp .env.example .env
# Edit .env with your values
```

## 2. Install the systemd service

```bash
# Copy the unit file and set your deploy path if different from /var/www/alreadyapp-backend
sudo cp deploy/alreadyapp-backend.service /etc/systemd/system/

# If your app lives elsewhere, edit the service file:
# sudo nano /etc/systemd/system/alreadyapp-backend.service
# Update WorkingDirectory, ExecStart, and EnvironmentFile to your path.

# Set ownership so the service can read the app and .env (User=www-data)
sudo chown -R www-data:www-data /var/www/alreadyapp-backend

# Create logs directory so the service can write app.log (path must match StandardOutput/StandardError in the unit)
mkdir -p /root/alreadyapp-backend/logs

# Reload systemd and enable/start the service
sudo systemctl daemon-reload
sudo systemctl enable alreadyapp-backend
sudo systemctl start alreadyapp-backend
```

## 3. Logs

All service output (stdout and stderr) is written to a single file. Path is set in the unit file (e.g. `/root/alreadyapp-backend/logs/app.log`). Ensure that directory exists before starting the service.

## 4. Useful commands

```bash
# Status and logs
sudo systemctl status alreadyapp-backend
# Application log file (all output in one file)
tail -f /root/alreadyapp-backend/logs/app.log

# Restart after code or .env changes
sudo systemctl restart alreadyapp-backend
```

The API will listen on `http://0.0.0.0:8000`. Put a reverse proxy (e.g. nginx) in front for HTTPS and a public domain.
