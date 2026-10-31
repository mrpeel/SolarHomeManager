# Tesla Developer & Fleet API Setup Guide

This guide explains how to configure a Tesla Developer account, host your public EC key for free (via GitHub Pages or Vercel), generate cryptographic keys on your Synology NAS, and configure automatic OAuth token refresh for **SolarHomeManager**.

> **Migrating from `FreeCharge`?**
> If you already completed this setup for `FreeCharge`, you do **not** need to re-register with Tesla. Simply copy your existing `.env` file and `tesla_private_key.pem` from `/volume1/homes/admin/tesla_tracker/` into `/volume1/homes/admin/solar_home_manager/`.

---

## 1. Prerequisites
1. **Multi-Factor Authentication (MFA):** Must be enabled on your Tesla account.
2. **Verified Email:** Your Tesla account email address must be verified.

---

## 2. Generate Your EC Prime256v1 Key Pair

Modern Tesla firmware requires commands (`charge_start`, `charge_stop`, `set_charging_amps`) to be cryptographically signed using a NIST P-256 (`prime256v1` / `secp256r1`) private key.

Run the following commands in your project folder (on your Mac or via SSH on your Synology NAS):

```bash
openssl ecparam -name prime256v1 -genkey -noout -out tesla_private_key.pem
openssl ec -in tesla_private_key.pem -pubout -out tesla_public_key.pem
```

- Keep `tesla_private_key.pem` secret inside `/volume1/homes/admin/solar_home_manager/tesla_private_key.pem` (it is git-ignored).
- `tesla_public_key.pem` must be hosted publicly at:
  `https://<your-domain>/.well-known/appspecific/com.tesla.3p.public-key.pem`

---

## 3. Host Your Public Key for Free

Tesla blocks shared Dynamic DNS domains (such as `*.synology.me` or `*.duckdns.org`). You can use **GitHub Pages** or **Vercel** for free:

### Option A: GitHub Pages (Recommended)
1. Create a public GitHub repository named `<your-github-username>.github.io`.
2. In the repository root, create an empty `.nojekyll` file (so GitHub Pages serves dot-directories) and the `.well-known/appspecific/` directory:
   ```bash
   mkdir -p .well-known/appspecific
   touch .nojekyll
   cp /path/to/tesla_public_key.pem .well-known/appspecific/com.tesla.3p.public-key.pem
   git add .nojekyll .well-known/appspecific/com.tesla.3p.public-key.pem
   git commit -m "Publish Tesla Fleet API public key"
   git push origin main
   ```
3. In GitHub **Settings** $\rightarrow$ **Pages**, set **Source** to **Deploy from a branch** (`main` / `root`).
4. Verify your key downloads cleanly in a browser at:
   `https://<your-github-username>.github.io/.well-known/appspecific/com.tesla.3p.public-key.pem`

### Option B: Vercel
1. Create a static folder structure:
   ```bash
   mkdir -p vercel-tesla-host/public/.well-known/appspecific
   cp tesla_public_key.pem vercel-tesla-host/public/.well-known/appspecific/com.tesla.3p.public-key.pem
   ```
2. Deploy with `vercel --prod` to obtain `https://<your-project>.vercel.app/.well-known/appspecific/com.tesla.3p.public-key.pem`.

---

## 4. Register Your Application in the Tesla Developer Portal

1. Log in to [developer.tesla.com](https://developer.tesla.com) and create an application:
   - **Allowed Origin URL:** `https://<your-domain>` (e.g. `https://<your-username>.github.io`)
   - **Allowed Redirect URI:** `https://<your-domain>/oauth/callback`
   - **Scopes:**
     - `[x] Vehicle Information` (`vehicle_device_data`)
     - `[x] Vehicle Location` (`vehicle_location`)
     - `[x] Vehicle Charging Management` (`vehicle_charging_cmds`)
     - `[x] Vehicle Commands` (`vehicle_cmds`)
2. Save your **Client ID** and **Client Secret** into `.env`:
   ```ini
   TESLA_CLIENT_ID=your_client_id
   TESLA_CLIENT_SECRET=your_client_secret
   ```

---

## 5. Register Your Partner Account Domain with Tesla

1. Request a **Partner Token** using your Client ID and Secret:
   ```bash
   curl -X POST https://auth.tesla.com/oauth2/v3/token \
     -H "Content-Type: application/json" \
     -d '{
       "grant_type": "client_credentials",
       "client_id": "<YOUR_CLIENT_ID>",
       "client_secret": "<YOUR_CLIENT_SECRET>",
       "scope": "openid vehicle_device_data vehicle_location vehicle_charging_cmds vehicle_cmds",
       "audience": "https://fleet-api.prd.na.vn.cloud.tesla.com"
     }'
   ```
   *(Note: If your account is in the Oceania/Asia-Pacific region and Tesla routes your account to NA or EU, use the matching regional `TESLA_API_BASE_URL` in `.env`).*

2. Register your domain with the returned Partner `access_token`:
   ```bash
   curl -X POST https://fleet-api.prd.na.vn.cloud.tesla.com/api/1/partner_accounts \
     -H "Authorization: Bearer <YOUR_PARTNER_TOKEN>" \
     -H "Content-Type: application/json" \
     -d '{"domain": "<your-domain>"}'
   ```

---

## 6. Generate User OAuth Tokens (`TESLA_API_TOKEN` & `TESLA_REFRESH_TOKEN`)

1. Open this URL in your browser (substituting `<YOUR_CLIENT_ID>` and `<your-domain>`):
   ```text
   https://auth.tesla.com/oauth2/v3/authorize?client_id=<YOUR_CLIENT_ID>&redirect_uri=https://<your-domain>/oauth/callback&response_type=code&scope=openid%20offline_access%20vehicle_device_data%20vehicle_location%20vehicle_charging_cmds%20vehicle_cmds&state=solarhome
   ```
2. Log in to your Tesla account and approve access.
3. Your browser will redirect to `https://<your-domain>/oauth/callback?code=...` (showing a 404 page, which is expected). Copy the `code` parameter from the address bar.
4. Exchange the authorization code for an `access_token` and `refresh_token`:
   ```bash
   curl -X POST https://auth.tesla.com/oauth2/v3/token \
     -H "Content-Type: application/json" \
     -d '{
       "grant_type": "authorization_code",
       "client_id": "<YOUR_CLIENT_ID>",
       "client_secret": "<YOUR_CLIENT_SECRET>",
       "code": "<COPIED_AUTH_CODE>",
       "redirect_uri": "https://<your-domain>/oauth/callback",
       "audience": "https://fleet-api.prd.na.vn.cloud.tesla.com"
     }'
   ```
5. Add both `access_token` and `refresh_token` to your `.env` file:
   ```ini
   TESLA_API_TOKEN=<paste_access_token_here>
   TESLA_REFRESH_TOKEN=<paste_refresh_token_here>
   ```
   **Automatic Token Renewal:** Whenever `TESLA_API_TOKEN` expires (every 8 hours), `solar_home_manager.py` automatically calls `refresh_tesla_token()` using `TESLA_REFRESH_TOKEN` and writes the new `TESLA_API_TOKEN` and `TESLA_REFRESH_TOKEN` directly back to `.env`.

---

## 7. Pair the Virtual Key with Your Vehicle

1. With the Tesla mobile app installed on your phone and near your vehicle, open this link on your phone:
   ```text
   https://tesla.com/_ak/<your-domain>
   ```
2. Tap **Approve** in the Tesla app to add your developer key to the vehicle.

---

## 8. How Pure-Python Command Signing Works on the Synology NAS

`solar_home_manager.py` uses `tesla-fleet-api` (`VehicleSigned`) and `cryptography` to load `tesla_private_key.pem` in memory and sign Vehicle Command Protocol protobuf payloads directly in Python. You do **not** need to run Tesla's Go `vehicle-command` HTTP proxy or Docker containers on your DS218+.
