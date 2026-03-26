# FamilyBrain

**Your family's second brain. WhatsApp-native family organisation for UK households.**

FamilyBrain is a WhatsApp-based personal assistant designed to help families manage their shared lives. Instead of downloading another app, families simply message the FamilyBrain bot on WhatsApp to store documents, set reminders, ask questions, and sync their calendars.

## How It Works

FamilyBrain acts as a shared, intelligent memory bank for the whole family:

1. **Capture Everything**: Send photos of school letters, PDF insurance documents, or voice notes directly to the WhatsApp bot.
2. **Intelligent Processing**: The bot extracts text via OCR, transcribes audio, and uses LLMs to understand the context and extract structured data (like dates, amounts, and action items).
3. **Instant Recall**: Ask natural language questions like *"When does the car insurance renew?"* or *"What did the school say about the trip?"* and get instant, accurate answers based on your stored data.
4. **Proactive Nudges**: FamilyBrain sends a weekly Sunday morning briefing and proactive alerts for upcoming deadlines or expiring documents.

## Key Features

- **WhatsApp-Native Interface**: No apps to download, no new interfaces to learn.
- **Document Storage & OCR**: Send images and PDFs; text is automatically extracted and the original files are securely stored.
- **Voice Note Transcription**: Speak naturally to the bot to store memories or set reminders.
- **Google & Apple Calendar Sync**: Two-way sync with Google Calendar and one-tap subscription for Apple Calendar.
- **Smart Reminders**: Automated alerts for MOTs, insurance renewals, and school deadlines.
- **Email Forwarding**: Each family gets a unique email address (via Mailgun) to forward important emails directly into their brain.
- **Multi-Tenant Architecture**: Secure data isolation between different families using Supabase Row Level Security (RLS).

## Tech Stack

- **Backend**: Python 3 / Flask (deployed on Railway)
- **Database & Storage**: Supabase (PostgreSQL + S3-compatible object storage)
- **Messaging**: Meta WhatsApp Cloud API
- **AI / LLM**: OpenAI API (GPT-4o / embeddings)
- **Email Processing**: Mailgun
- **Payments**: Stripe
- **Landing Page**: Static HTML/CSS/JS (deployed on Vercel)

## Environment Variables

To run the backend services, the following key environment variables are required:

```env
# Core Database
SUPABASE_URL=
SUPABASE_SERVICE_KEY=

# AI Services
OPENAI_API_KEY=
OPENAI_EMBEDDING_BASE_URL=

# WhatsApp Integration
USE_META_API=true
META_WHATSAPP_TOKEN=
META_WHATSAPP_PHONE_NUMBER_ID=
META_WHATSAPP_VERIFY_TOKEN=

# Email Integration
MAILGUN_API_KEY=
MAILGUN_DOMAIN=

# Payments
STRIPE_SECRET_KEY=
STRIPE_WEBHOOK_SECRET=
STRIPE_PRICE_ID_MONTHLY=
STRIPE_PRICE_ID_ANNUAL=

# Application Config
FAMILYBRAIN_BASE_URL=
ONBOARDING_BASE_URL=
```

## License

**Proprietary Software**

This repository contains proprietary software. It is not open source. All rights are reserved by the authors. Unauthorized copying, modification, distribution, or use of this software is strictly prohibited.
