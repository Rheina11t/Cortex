# Google OAuth Secret Rotation Guide

This guide outlines the steps to rotate the Google OAuth client secret for FamilyBrain if it has been compromised.

## 1. Generate New Secret in Google Cloud Console
1. Log in to the [Google Cloud Console](https://console.cloud.google.com/).
2. Select the **FamilyBrain** project.
3. Navigate to **APIs & Services > Credentials**.
4. Find your OAuth 2.0 Client ID in the list and click the **Edit** (pencil) icon.
5. Click **RESET SECRET** at the top of the page.
6. Copy the new **Client Secret** immediately.

## 2. Update Environment Variables in Railway
1. Go to your [Railway Project Dashboard](https://railway.app/project/87de1a69-431b-4687-93c3-7cb8b682709b).
2. Select the `whatsapp-capture` (or relevant) service.
3. Navigate to the **Variables** tab.
4. Find `GOOGLE_CALENDAR_CLIENT_SECRET` and update it with the new secret.
5. Railway will automatically redeploy the service with the new variable.

## 3. Verify the Rotation
1. Open WhatsApp and send the command `/connect calendar` to the FamilyBrain bot.
2. Click the generated Google Calendar link.
3. Complete the OAuth flow. If you can successfully link your calendar, the rotation was successful.

## 4. Audit Other Credentials
The following credentials should also be reviewed for potential compromise:
- `STRIPE_SECRET_KEY`: Rotate in Stripe Dashboard if suspected.
- `SUPABASE_SERVICE_KEY`: Rotate in Supabase Project Settings > API.
- `TWILIO_AUTH_TOKEN`: Rotate in Twilio Console.
- `OPENAI_API_KEY`: Rotate in OpenAI API settings.

**Note:** Always ensure that old secrets are deleted from the Google Cloud Console once the new one is verified.
