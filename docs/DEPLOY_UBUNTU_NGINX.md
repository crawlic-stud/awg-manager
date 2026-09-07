# Production deployment on Ubuntu with Docker and Nginx

This guide deploys `awg-manager` in a Docker container and exposes it through Nginx over HTTPS.

Replace these placeholders before running commands:

```text
YOUR_DOMAIN  — your domain, for example vpn.example.com
YOUR_EMAIL   — email for Let's Encrypt notifications
```

The guide assumes:

- Ubuntu 22.04 or 24.04;
- DNS `A` record for `YOUR_DOMAIN` already points to this server;
- Nginx runs directly on the Ubuntu host;
- the application listens internally on `127.0.0.1:8888`;
- SSH access to the server is available.

## 1. Connect to the server

```bash
ssh root@SERVER_IP
```

Update Ubuntu:

```bash
apt update
apt upgrade -y
timedatectl set-timezone Europe/Moscow
```

## 2. Configure the firewall

Keep the current SSH connection open while enabling UFW:

```bash
apt install -y ufw
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable
ufw status verbose
```

Do not open port `8888`. It is only for local communication between Nginx and the application container.

## 3. Install Docker

Remove conflicting packages if present:

```bash
apt remove -y docker.io docker-doc podman-docker containerd runc || true
```

Install Docker from the official repository:

```bash
apt install -y ca-certificates curl
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo \"$VERSION_CODENAME\") stable" > /etc/apt/sources.list.d/docker.list
apt update
apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
docker --version
docker compose version
```

## 4. Download the application

```bash
apt install -y git
mkdir -p /opt
git clone https://github.com/crawlic-stud/awg-manager.git /opt/awg-manager
cd /opt/awg-manager
```

For a private repository, use a deploy key or another authenticated Git method. Do not put Git credentials into shell history.

## 5. Create the environment file

```bash
cp .env.example .env
nano .env
```

Set real values, at minimum:

```env
SSH_USER=root
SSH_PASSWORD=YOUR_VPN_SERVER_SSH_PASSWORD
SSH_PORT=22
VPN_ENDPOINT_HOST=YOUR_VPN_SERVER_IP_OR_HOSTNAME
SERVER_NAME=YOUR_SERVER_NAME
DB_PATH=/app/data/wg_manager.sqlite3
SECRET_KEY=GENERATE_A_LONG_RANDOM_VALUE
AUTH_PASSWORD=GENERATE_A_STRONG_DASHBOARD_PASSWORD
RESEND_API_KEY=
EMAIL=
```

Generate secrets with:

```bash
openssl rand -hex 32
```

Restrict access to secrets and application data:

```bash
chmod 600 .env
mkdir -p data
chmod 700 data
```

The production compose file enables `SESSION_COOKIE_SECURE=1` and `TRUST_PROXY=1` automatically. The `data` directory contains the SQLite database and must be backed up.

## 6. Start the production container

```bash
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml logs --tail=100 awg-manager
```

Test the application directly on localhost:

```bash
curl -I http://127.0.0.1:8888/login
```

The response should be `200 OK`. If it fails, fix the container before configuring Nginx.

## 7. Install and configure Nginx

```bash
apt install -y nginx
systemctl enable --now nginx
```

Create `/etc/nginx/sites-available/awg-manager`:

```bash
nano /etc/nginx/sites-available/awg-manager
```

Use this configuration and replace `YOUR_DOMAIN`:

```nginx
server {
    listen 80;
    listen [::]:80;
    server_name YOUR_DOMAIN;

    client_max_body_size 2m;

    location / {
        proxy_pass http://127.0.0.1:8888;

        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }
}
```

Enable the site:

```bash
ln -s /etc/nginx/sites-available/awg-manager /etc/nginx/sites-enabled/awg-manager
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx
```

Test the HTTP proxy:

```bash
curl -I http://YOUR_DOMAIN/login
```

## 8. Enable HTTPS with Let's Encrypt

Install Certbot:

```bash
apt install -y certbot python3-certbot-nginx
```

Request and install the certificate:

```bash
certbot --nginx -d YOUR_DOMAIN --email YOUR_EMAIL --agree-tos --no-eff-email
```

When prompted, select the option to redirect HTTP traffic to HTTPS.

Check and reload Nginx:

```bash
nginx -t
systemctl reload nginx
curl -I https://YOUR_DOMAIN/login
```

Open `https://YOUR_DOMAIN` in a browser and sign in with `AUTH_PASSWORD`.

## 9. Check certificate renewal

```bash
certbot renew --dry-run
systemctl list-timers | grep certbot
```

## 10. Useful commands

```bash
cd /opt/awg-manager
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml logs -f awg-manager
docker compose -f docker-compose.prod.yml restart awg-manager
```

Nginx diagnostics:

```bash
nginx -t
systemctl status nginx --no-pager
journalctl -u nginx -n 100 --no-pager
```

## 11. Updating the application

Back up the database before updating:

```bash
cd /opt/awg-manager
tar -czf "/root/awg-manager-data-$(date +%F-%H%M%S).tar.gz" data
```

Pull and rebuild:

```bash
git pull --ff-only origin master
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml ps
```

The `data` directory is mounted separately and survives container rebuilds.

## 12. Troubleshooting

### Nginx returns `502 Bad Gateway`

```bash
docker compose -f /opt/awg-manager/docker-compose.prod.yml ps
curl -I http://127.0.0.1:8888/login
docker compose -f /opt/awg-manager/docker-compose.prod.yml logs --tail=100 awg-manager
```

### Login works over HTTP but not HTTPS

Check that:

- Nginx sends `X-Forwarded-Proto $scheme`;
- the production compose file is being used;
- the container was recreated after configuration changes.

```bash
docker compose -f docker-compose.prod.yml up -d --build --force-recreate
```

The production compose file sets `SESSION_COOKIE_SECURE=1`, so the browser will only send the session cookie over HTTPS.

### Certbot cannot validate the domain

Check DNS, the hosting provider firewall, UFW, and listeners:

```bash
dig +short YOUR_DOMAIN
ss -ltnp | grep -E ':80|:443'
ufw status verbose
```

