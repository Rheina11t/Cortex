# Data Protection Impact Assessment (DPIA)

**Project Name:** FamilyBrain  
**Data Controller:** Mr Rheinallt Daniel Jones  
**ICO Registration Reference:** ZC109957  
**Date of Assessment:** 29 March 2026  
**Review Date:** 29 March 2027  

---

## 1. Purpose and Description of Processing

**Nature of the Processing:**  
FamilyBrain is a UK-based WhatsApp family AI assistant acting as an emergency vault and organizational tool. It processes incoming WhatsApp messages (text, images, PDFs) sent by authorized family members. The system extracts, categorizes, and stores this information to provide scheduling, reminders, and an emergency "death binder" (a consolidated PDF of critical family information).

**Types of Data Processed:**  
The system processes highly sensitive personal data, including but not limited to:
- Identifying information (names, phone numbers, family relationships)
- Financial details (bank accounts, insurance policies, pensions)
- Medical and health information (prescriptions, GP details, medical conditions)
- Legal documents (wills, Lasting Power of Attorney)
- Daily schedules, locations, and routines

**Data Subjects:**  
Subscribing families (parents, partners, and authorized dependents).

---

## 2. Necessity and Proportionality Assessment

**Lawful Basis for Processing:**  
Processing is necessary for the performance of a contract (UK GDPR Article 6(1)(b)) to provide the FamilyBrain service requested by the user. For special category data (e.g., health information), processing is based on explicit consent (UK GDPR Article 9(2)(a)), obtained during onboarding and via the first-message privacy notice.

**Proportionality:**  
The data collected is strictly limited to what users choose to send to their vault. The processing is proportionate to the goal of providing a comprehensive family memory and emergency retrieval system. 

**Data Retention Policy:**  
Data is retained for as long as necessary to provide the emergency vault service. Because the core value of the product is long-term secure storage of critical documents (which may not be accessed for years), **data is retained indefinitely while the account remains active**. 
Data is permanently deleted when:
1. The user explicitly requests deletion (via the `/delete` or `/delete-my-data` commands).
2. The account subscription is cancelled, following a **90-day post-cancellation grace period** (allowing users time to export their data via `/mydata`).
No arbitrary inactivity-based deletion is applied, as this would defeat the purpose of an emergency vault.

---

## 3. Risks Identified

The following privacy and security risks have been identified due to the sensitive nature of the data and the architecture of the service:

1. **Data Breach / Unauthorized Access:** Malicious actors gaining access to the database containing highly sensitive financial and medical records.
2. **AI Hallucination of Personal Data:** The LLM generating incorrect or mixed-up personal data, potentially leading to incorrect medical or financial advice being relayed to the user.
3. **WhatsApp as a Channel:** WhatsApp messages transit Meta's infrastructure. While end-to-end encrypted between the user and the WhatsApp Cloud API endpoint, the data is decrypted in memory at the webhook receiver.
4. **Third-Party Sub-Processors:** Transferring sensitive family data to US-based sub-processors (e.g., OpenAI, Railway) introduces risks related to international data transfers and foreign government access.
5. **Prompt Injection / Jailbreaking:** Malicious inputs designed to trick the AI into leaking other families' data or system prompts.

---

## 4. Risk Mitigation Measures

To address the identified risks, the following security controls have been implemented (referencing Phases 1-4 of the security hardening process):

- **Row Level Security (RLS):** Implemented in Supabase to ensure strict tenant isolation. Each family's data is cryptographically isolated at the database level.
- **Input Sanitization & Output Filtering:** All LLM inputs are scanned for prompt injection patterns. Outputs are filtered to prevent the leakage of secrets or system prompts.
- **Content Moderation:** Automated detection of harmful content (e.g., self-harm, abuse) with appropriate safe fallbacks and signposting to emergency services.
- **PIN Verification:** Highly sensitive commands (e.g., `/sos`, `/delete`) require a 4-6 digit PIN, hashed using bcrypt, to prevent unauthorized execution if a phone is left unlocked.
- **Audit Logging:** Comprehensive, immutable audit logging for all sensitive actions (deletions, exports, PIN failures, rate limit breaches).
- **Rate Limiting:** In-memory sliding-window rate limiters (per-phone and per-IP) to prevent brute-force attacks and denial-of-service.
- **First-Message Privacy Notice:** Automated delivery of a UK GDPR Article 13 compliant privacy notice upon a user's first interaction with the bot.

---

## 5. Sub-Processor List and DPA Status

FamilyBrain utilizes the following sub-processors. Data Processing Agreements (DPAs) and Standard Contractual Clauses (SCCs) are in place where applicable.

| Sub-Processor | Location | Purpose | DPA / Transfer Mechanism | Notes |
| :--- | :--- | :--- | :--- | :--- |
| **Supabase** | UK (eu-west-2) | Primary database and authentication | Yes (DPA) | Data remains within UK jurisdiction (AWS London). |
| **OpenAI** | US | LLM processing (intent routing, extraction) | Yes (DPA + SCCs) | Highly sensitive family data is sent to the US for processing. OpenAI's zero-data-retention API policy applies (data not used for training). |
| **Meta (WhatsApp)** | US | Messaging channel (Cloud API) | Yes (DPA + SCCs) | Messages transit Meta infrastructure. End-to-end encrypted up to the webhook endpoint. |
| **Stripe** | US / EU | Payment processing | Yes (DPA) | Handles billing data only; no vault data. |
| **Railway** | US | Application hosting (Flask backend) | Yes (DPA + SCCs) | Processing occurs outside the UK. Environment variables and memory contain decrypted data during processing. |
| **Vercel** | US | Frontend hosting (Landing page) | Yes (DPA) | Minimal PII; handles static assets and marketing site. |

---

## 6. Residual Risk Assessment

After applying the mitigation measures outlined in Section 4, the residual risks are assessed as follows:

- **Data Breach:** Reduced to **Low**. RLS, strict IAM roles, and secure key management significantly reduce the likelihood of a mass breach.
- **AI Hallucination:** Reduced to **Medium**. While prompt engineering and context-bounding reduce hallucinations, the inherent nature of LLMs means occasional inaccuracies may occur. Users are advised to verify critical information.
- **International Transfers:** Reduced to **Medium**. SCCs and DPAs are in place, and OpenAI's API policy prohibits training on user data. However, processing sensitive data in the US carries inherent jurisdictional risks.
- **Unauthorized Access via Device:** Reduced to **Low**. The implementation of the SOS PIN for sensitive commands mitigates the risk of an unlocked device being exploited.

**Conclusion:** The processing is proportionate to the benefits provided by the service. The residual risks are acceptable given the robust technical and organizational measures implemented.

---

## 7. Sign-Off

This DPIA has been reviewed and approved by the Data Controller. The processing operations described herein comply with the requirements of the UK GDPR.

**Data Controller:** Mr Rheinallt Daniel Jones  
**ICO Registration:** ZC109957  
**Signature:** *[Signed electronically]*  
**Date:** 29 March 2026  
