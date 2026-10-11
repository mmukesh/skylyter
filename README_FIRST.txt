SKYLYTER WEBSITE + PAYMENT HANDOFF (LOCAL TESTED CANDIDATE)

WHAT IS INCLUDED
- index.html: Skylyter website. Local Live Server testing automatically calls FastAPI at http://127.0.0.1:8001.
- main.py: FastAPI backend candidate with Razorpay order creation, server-side payment verification, 30-day Student Plus expiry, webhook signature checks, and database schema setup.
- requirements.txt: Python dependencies.

CURRENT STATUS
- Local website/backend connection was observed working in the user's screenshot.
- Razorpay Test Mode checkout opened and its demo success/failure page appeared.
- This is NOT proof of a captured payment or proof that production live payments work.
- This package has not been deployed or end-to-end tested against the user's actual database and Razorpay webhook.

RUN LOCALLY (Windows PowerShell)
1. Open a terminal in this folder.
2. Create/activate a virtual environment if desired.
3. Install: pip install -r requirements.txt
4. Set environment variables in the terminal (never put secrets in HTML):
   $env:SKYLYTER_JWT_SECRET = '<long random secret>'
   $env:DATABASE_URL = '<PostgreSQL connection string>'
   $env:RAZORPAY_KEY_ID = '<Razorpay TEST key id>'
   $env:RAZORPAY_KEY_SECRET = '<Razorpay TEST key secret>'
   $env:RAZORPAY_WEBHOOK_SECRET = '<test webhook secret configured in Razorpay>'
   $env:SKYLYTER_ALLOWED_ORIGINS = 'http://127.0.0.1:5500,http://localhost:5500'
5. Start backend: python -m uvicorn main:app --host 127.0.0.1 --port 8001
6. Serve index.html with VS Code Live Server on port 5500.

BEFORE PUBLIC/LIVE LAUNCH
- Deploy backend and website to HTTPS hosting; do not use localhost in the public site.
- Set window.SKYLYTER_API_BASE_URL in the production HTML to the real HTTPS API origin, OR reverse-proxy /api on the website host.
- Set production CORS origins to the exact website origin.
- Configure Razorpay webhook endpoint https://<actual-api-domain>/api/webhooks/razorpay and use its webhook secret in server environment.
- Test success, failure, cancel, duplicate webhook, renewal/expiry, and database updates in TEST mode.
- Confirm database migration succeeded and a test payment creates exactly one paid payment record and 30-day entitlement.
- Complete legal business identity, support contact, privacy/data-retention, and refund terms.
- Only after these checks, change to matching Razorpay LIVE keys and LIVE webhook secret. Never mix test and live credentials.
- Never send passwords, DATABASE_URL, JWT secret, Razorpay secret, or webhook secret to anyone or commit them to source control.
