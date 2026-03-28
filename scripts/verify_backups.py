"""
FamilyBrain – Supabase Backup Verification (Phase 3).
Checks the Supabase Management API to confirm automated backups are enabled.
"""
import os
import requests
import sys

PROJECT_ID = "lmwnozlqjaggdpaoossy"
# SUPABASE_ACCESS_TOKEN should be set in environment
ACCESS_TOKEN = os.environ.get("SUPABASE_ACCESS_TOKEN")

def check_backups():
    if not ACCESS_TOKEN:
        print("Error: SUPABASE_ACCESS_TOKEN environment variable not set.")
        print("To enable backups, please follow these instructions:")
        print("1. Go to https://supabase.com/dashboard/project/lmwnozlqjaggdpaoossy/settings/database")
        print("2. Ensure 'Daily Backups' is enabled under the Backups section.")
        print("3. For automated verification, generate an access token at https://supabase.com/dashboard/account/tokens")
        return

    url = f"https://api.supabase.com/v1/projects/{PROJECT_ID}/database/backups"
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            data = response.json()
            # The API structure might vary, but we check for backup configuration
            if data.get("enabled") or data.get("wals_enabled"):
                print(f"✅ Automated backups are ENABLED for project {PROJECT_ID}.")
            else:
                print(f"⚠️ Automated backups appear to be DISABLED for project {PROJECT_ID}.")
                print("Please enable them at: https://supabase.com/dashboard/project/lmwnozlqjaggdpaoossy/settings/database")
        else:
            print(f"❌ Failed to check backups. API returned status {response.status_code}: {response.text}")
    except Exception as e:
        print(f"❌ Error connecting to Supabase API: {e}")

if __name__ == "__main__":
    check_backups()
