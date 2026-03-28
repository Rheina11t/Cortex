# Monitoring Setup Guide (Phase 4)

This guide explains how to set up automated monitoring for the FamilyBrain production service.

## 1. UptimeRobot Monitoring (External)
UptimeRobot provides a free tier that can ping our health endpoint every 5 minutes and notify us if it fails.

1.  Go to [UptimeRobot.com](https://uptimerobot.com/) and create a free account.
2.  Click **"Add New Monitor"**.
3.  **Monitor Type:** HTTP(s)
4.  **Friendly Name:** FamilyBrain Production
5.  **URL (or IP):** `https://api.familybrain.co.uk/whatsapp/health`
6.  **Monitoring Interval:** 5 minutes
7.  **Alert Contacts:** Select your email address.
8.  Click **"Create Monitor"**.

### Interpreting the Health Response
The `/whatsapp/health` endpoint returns a JSON response:
- `status: "ok"`: Everything is working correctly.
- `status: "degraded"`: One or more non-critical secrets are missing, but the core database is connected.
- `status: "down"`: The database connection failed.

## 2. Railway Service Alerts
Railway provides built-in alerts for deployment failures and service health.

1.  Open the [Railway Project Dashboard](https://railway.app/project/87de1a69-431b-4687-93c3-7cb8b682709b).
2.  Go to **Settings** -> **Alerts**.
3.  Enable **"Deployment Failed"** alerts.
4.  Enable **"Service Restarted"** alerts (this can indicate memory issues or crashes).
5.  Connect your Discord or Slack if you want real-time push notifications.

## 3. Log Monitoring
To check for security events or errors in real-time:
1.  Use the Railway CLI: `railway logs`
2.  Or view the logs in the Railway dashboard under the **"Logs"** tab for the `cortex` service.
3.  Look for `[SECURITY]` or `[AUDIT]` tags in the logs for critical events.
