# HTTPS & Reverse Proxy Setup

Kiso's API runs on HTTP by default. For production deployments — especially on public VPS — you should terminate TLS at a reverse proxy in front of kiso.

## Prerequisites

- A domain name pointing to your VPS (e.g., `bot.example.com`)
- Ports 80 and 443 open on your VPS firewall

## Option 1: Caddy (recommended)

Caddy automatically provisions and renews HTTPS certificates via Let's Encrypt. Zero configuration needed beyond a domain name.

### docker-compose.https.yml

```yaml
services:
  kiso:
    image: kiso:latest
    restart: unless-stopped
    # No ports exposed directly — Caddy handles external traffic
    env_file: /root/.kiso/instances/kiso/.env
    volumes:
      - /root/.kiso/instances/kiso:/root/.kiso

  caddy:
    image: caddy:2
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
      - caddy_config:/config

volumes:
  caddy_data:
  caddy_config:
```

### Caddyfile

```
bot.example.com {
    reverse_proxy kiso:8333
}
```

### Steps

1. Point your domain to your VPS IP (`A` record)
2. Copy `docker-compose.https.yml` and `Caddyfile` to your VPS
3. Edit the domain in `Caddyfile`
4. Stop the standalone kiso container: `docker stop kiso-<instance>`
5. Start with compose: `docker compose -f docker-compose.https.yml up -d`
6. Update `external_url` in `config.toml`: `external_url = "https://bot.example.com"`
7. Restart kiso: `docker compose -f docker-compose.https.yml restart kiso`

Caddy will automatically obtain a Let's Encrypt certificate on first request.

---

## Option 2: nginx

If you already have nginx on your VPS:

### nginx.example.conf

```nginx
server {
    listen 80;
    server_name bot.example.com;

    # Redirect HTTP to HTTPS
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name bot.example.com;

    # Let's Encrypt certificates (managed by certbot)
    ssl_certificate     /etc/letsencrypt/live/bot.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/bot.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8333;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

### Steps

1. Install certbot: `sudo apt install certbot python3-certbot-nginx`
2. Copy `nginx.example.conf` to `/etc/nginx/sites-available/kiso`
3. Symlink: `sudo ln -s /etc/nginx/sites-available/kiso /etc/nginx/sites-enabled/`
4. Edit the domain in the config
5. Get certificate: `sudo certbot --nginx -d bot.example.com`
6. Update `external_url` in `config.toml`
7. Reload: `sudo nginx -s reload`

---

## Option 3: Traefik

For environments with multiple services and Docker-native service discovery:

### docker-compose.traefik.yml

```yaml
services:
  traefik:
    image: traefik:v3.0
    restart: unless-stopped
    command:
      - "--providers.docker=true"
      - "--providers.docker.exposedbydefault=false"
      - "--entrypoints.web.address=:80"
      - "--entrypoints.websecure.address=:443"
      - "--certificatesresolvers.letsencrypt.acme.httpchallenge=true"
      - "--certificatesresolvers.letsencrypt.acme.httpchallenge.entrypoint=web"
      - "--certificatesresolvers.letsencrypt.acme.email=you@example.com"
      - "--certificatesresolvers.letsencrypt.acme.storage=/letsencrypt/acme.json"
      - "--entrypoints.web.http.redirections.entryPoint.to=websecure"
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - traefik_certs:/letsencrypt

  kiso:
    image: kiso:latest
    restart: unless-stopped
    env_file: /root/.kiso/instances/kiso/.env
    volumes:
      - /root/.kiso/instances/kiso:/root/.kiso
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.kiso.rule=Host(`bot.example.com`)"
      - "traefik.http.routers.kiso.entrypoints=websecure"
      - "traefik.http.routers.kiso.tls.certresolver=letsencrypt"
      - "traefik.http.services.kiso.loadbalancer.server.port=8333"

volumes:
  traefik_certs:
```

### Steps

1. Edit domain and email in `docker-compose.traefik.yml`
2. Stop standalone kiso container
3. `docker compose -f docker-compose.traefik.yml up -d`
4. Update `external_url` in `config.toml`

---

## Option 4: SSH tunnel (no domain needed)

For quick development access without HTTPS or a domain:

```bash
# From your laptop — creates a secure tunnel
ssh -L 8333:localhost:8333 your-vps

# Now access kiso at http://localhost:8333 on your laptop
```

This is secure (traffic encrypted by SSH) but only works for one user at a time and doesn't provide a public URL for file links.

For the installer's `external_url`, leave it empty when using SSH tunnels — pub file links will use relative paths.
