# FamilyBrain — Incident Response Runbook

**Version:** 1.0
**Last updated:** 28 March 2026
**Owner:** Rheinallt Daniel Jones (Data Controller, ICO Ref: ZC109957)

---

## 1. Leaked Secret

**Severity:** CRITICAL

### Detection
- Automated alerts from GitHub secret scanning
- Unexpected API charges on Stripe, OpenAI, or Supabase dashboards
- Unusual activity in Railway deployment logs

### Response Steps

1. **Immediately revoke the compromised key** — do not wait for investigation:
   - **Stripe:** Dashboard → Developers → API keys → Roll key
   - **OpenAI:** platform.openai.com → API keys → Delete and regenerate
   - **Supabase:** Dashboard → Settings → API → Regenerate service_role key
   - **Google OAuth:** See `docs/google_oauth_rotation.md`
   - **Meta App Secret:** developers.facebook.com → App Settings → Basic → Reset
   - **Twilio:** console.twilio.com → Account → Auth Tokens → Rotate
2. **Update the new key in Railway** environment variables immediately.
3. **Redeploy** the service on Railway to pick up the new key.
4. **Audit access logs** for the compromised key:
   - Check Railway logs for unusual API calls.
   - Check Stripe dashboard for unauthorized charges.
   - Check OpenAI usage dashboard for unexpected token consumption.
5. **Assess impact:** Determine what data or actions the attacker could have accessed.
6. **If personal data was exposed:** Follow the Data Exposure procedure (Section 4).
7. **Document** the incident in the incident log with timeline, impact, and remediation.

---

## 2. Webhook Spoofing

**Severity:** HIGH

### Detection
- `webhook_signature_failed` events in security logs
- Unusual volume of webhook requests from unexpected IPs
- `ip_rate_limit_hit` events for webhook endpoints

### Response Steps

1. **Check security logs** for the pattern:
   ```bash
   grep "webhook_signature_failed" /var/log/familybrain/*.log
   ```
2. **Identify the source IPs** from the log entries.
3. **Verify legitimate webhook sources:**
   - Meta: IPs should be from Meta's published ranges
   - Stripe: Check Stripe's webhook IP list
4. **If spoofing is confirmed:**
   - The signature verification already blocks these requests.
   - Monitor for escalation (volume increase, different attack vectors).
   - Consider adding IP allowlisting at the Railway/Cloudflare level.
5. **Rotate webhook secrets** as a precaution:
   - Stripe: Dashboard → Developers → Webhooks → Reveal signing secret → Roll
   - Meta: App Dashboard → Webhooks → Verify Token (update in Railway)
6. **Document** the incident.

---

## 3. Prompt Leakage

**Severity:** MEDIUM-HIGH

### Detection
- `output_filter_triggered` events in security logs
- `prompt_injection_blocked` events indicating active attack attempts
- User reports of unexpected bot responses

### Response Steps

1. **Check security logs** for output filter triggers:
   ```bash
   grep "output_filter_triggered\|prompt_injection_blocked" /var/log/familybrain/*.log
   ```
2. **Review the blocked content** to assess what was nearly leaked.
3. **If system prompt was exposed:**
   - The prompt itself is not a secret, but it reveals internal logic.
   - Review and strengthen the `_sanitise_llm_output()` filter patterns.
   - Consider adding the leaked pattern to the blocklist.
4. **If API keys were nearly exposed:**
   - Rotate the affected keys immediately (see Section 1).
   - Investigate how the key ended up in the LLM context.
5. **Update defences:**
   - Add new patterns to `_JAILBREAK_PATTERNS` in `whatsapp_capture.py`.
   - Add new patterns to `_sanitise_llm_output()`.
6. **Document** the incident and the defence improvements made.

---

## 4. Data Exposure

**Severity:** CRITICAL

### Detection
- Evidence of unauthorized data access in audit logs
- User reports of seeing another family's data
- Broken RLS policies (see Section 6)

### GDPR Notification Requirements

Under GDPR Article 33, you **must notify the ICO within 72 hours** of becoming aware of a personal data breach that poses a risk to individuals' rights and freedoms.

### Response Steps

1. **Contain the breach immediately:**
   - If RLS is broken: see Section 6 (Emergency Lockdown).
   - If a specific endpoint is leaking data: disable it via Railway env var or emergency deploy.
2. **Assess scope:**
   - Which families' data was potentially exposed?
   - What types of data? (memories, documents, financial info, health info)
   - How long was the exposure window?
   - Was the data actually accessed, or just potentially accessible?
3. **ICO notification (within 72 hours):**
   - Go to: https://ico.org.uk/make-a-complaint/data-protection-complaints/data-protection-complaints/
   - Or call the ICO helpline: 0303 123 1113
   - Provide: nature of breach, categories of data, approximate number of affected individuals, likely consequences, measures taken
   - Reference your registration: **ZC109957**
4. **Notify affected individuals** if the breach poses a high risk:
   - Send a WhatsApp message to affected families explaining what happened.
   - Explain what data was involved and what steps they should take.
5. **Remediate** the root cause.
6. **Document** everything: timeline, scope, notification records, remediation steps.

---

## 5. Runaway Model Spend

**Severity:** HIGH

### Detection
- `token_budget_exceeded` events in security logs
- OpenAI usage dashboard showing unexpected spikes
- Monthly bill significantly higher than expected

### Response Steps

1. **Kill the OpenAI API key immediately:**
   - Go to platform.openai.com → API keys → Delete the key.
   - This stops all API calls instantly.
2. **Assess damage:**
   - Check the OpenAI usage dashboard for total spend.
   - Check token_budget logs to identify which families triggered excessive usage.
   - Determine if this was abuse, a bug, or a prompt injection loop.
3. **Set a hard spending limit on OpenAI:**
   - platform.openai.com → Settings → Limits → Set monthly budget cap.
4. **Generate a new API key** and update in Railway.
5. **Review and tighten token budgets:**
   - Reduce `TOKEN_BUDGET_DAILY_PER_FAMILY` if needed.
   - Reduce `TOKEN_BUDGET_DAILY_GLOBAL` if needed.
6. **If caused by a bug:** Fix the loop/recursion and deploy.
7. **If caused by abuse:** Review the family's usage and consider suspension.
8. **Redeploy** with the new key and tighter limits.

---

## 6. Broken RLS (Row-Level Security)

**Severity:** CRITICAL

### Detection
- Cross-family data appearing in query responses
- `test_data_isolation.py` tests failing
- Audit log entries showing queries returning data from multiple families

### Emergency Lockdown Procedure

1. **Immediately disable the service:**
   ```bash
   # Via Railway CLI or dashboard — set the service to 0 replicas
   railway service update --replicas 0
   ```
   Or set an environment variable `MAINTENANCE_MODE=true` and redeploy.

2. **Audit the RLS policies in Supabase:**
   ```sql
   -- Check which tables have RLS enabled
   SELECT tablename, rowsecurity
   FROM pg_tables
   WHERE schemaname = 'public';

   -- Check existing policies
   SELECT * FROM pg_policies WHERE schemaname = 'public';
   ```

3. **Fix the broken policy:**
   - Ensure every table with family data has RLS enabled.
   - Ensure every policy filters by `family_id`.
   - Test with the `test_data_isolation.py` suite.

4. **Assess exposure:**
   - Query audit logs to determine if cross-family data was actually served.
   - Follow Data Exposure procedure (Section 4) if confirmed.

5. **Re-enable the service** only after RLS is verified.

---

## 7. Stripe Entitlement Corruption

**Severity:** MEDIUM

### Detection
- Users reporting access to features they should not have
- Users reporting loss of access to features they should have
- Mismatches between Stripe subscription status and the `families` table

### Response Steps

1. **Audit the current state:**
   ```sql
   -- Compare families table with expected Stripe status
   SELECT f.family_id, f.plan, f.stripe_customer_id, f.stripe_subscription_id
   FROM families f
   WHERE f.plan IS NOT NULL
   ORDER BY f.created_at DESC;
   ```

2. **Cross-reference with Stripe:**
   - For each family, check the subscription status in the Stripe dashboard.
   - Compare `plan` in the database with the actual Stripe subscription plan.

3. **Fix mismatches:**
   ```sql
   -- Example: correct a family's plan
   UPDATE families
   SET plan = 'founding', updated_at = NOW()
   WHERE family_id = 'xxx' AND stripe_subscription_id = 'sub_xxx';
   ```

4. **Check the webhook idempotency table** for missed events:
   ```sql
   SELECT * FROM processed_stripe_events
   ORDER BY processed_at DESC
   LIMIT 50;
   ```

5. **Re-process any missed webhooks** by triggering a re-send from Stripe:
   - Stripe Dashboard → Developers → Webhooks → Select endpoint → Resend events.

6. **Verify the entitlements table** is correctly seeded (migration 031).

---

## General Incident Log Template

For every incident, create an entry with:

| Field | Description |
| :--- | :--- |
| **Incident ID** | Unique identifier (e.g., INC-2026-001) |
| **Date/Time Detected** | When the incident was first noticed |
| **Date/Time Resolved** | When the incident was fully resolved |
| **Severity** | CRITICAL / HIGH / MEDIUM / LOW |
| **Category** | Which section of this runbook applies |
| **Description** | What happened |
| **Impact** | What data/users were affected |
| **Root Cause** | Why it happened |
| **Remediation** | What was done to fix it |
| **Prevention** | What changes were made to prevent recurrence |
| **ICO Notified** | Yes/No (if personal data breach) |
| **Users Notified** | Yes/No (if high risk to individuals) |

---

## Emergency Contacts

| Role | Contact |
| :--- | :--- |
| Data Controller | Rheinallt Daniel Jones |
| ICO Helpline | 0303 123 1113 |
| ICO Breach Report | https://ico.org.uk/make-a-complaint/ |
| Stripe Support | https://support.stripe.com/ |
| Supabase Support | https://supabase.com/dashboard/support |
| OpenAI Support | https://help.openai.com/ |
| Railway Support | https://railway.app/help |
