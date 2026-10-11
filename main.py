
import os
import re
import hmac
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import psycopg
import razorpay
from psycopg.rows import dict_row
import jwt
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


# Configuration
JWT_SECRET = os.getenv("SKYLYTER_JWT_SECRET")
if not JWT_SECRET:
    raise RuntimeError(
        "Set the SKYLYTER_JWT_SECRET environment variable before starting."
    )

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError(
        "Set the DATABASE_URL environment variable before starting."
    )
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET")
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET")

if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
    raise RuntimeError(
        "Set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET before starting the API."
    )

razorpay_client = razorpay.Client(
    auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET)
)
JWT_ALGORITHM = "HS256"
TOKEN_HOURS = 2

app = FastAPI(title="Skylyter Website Integration API")
bearer_scheme = HTTPBearer()

# Configure exact website origins for local testing or production.
_default_origins = "http://localhost:5500,http://127.0.0.1:5500"
ALLOWED_ORIGINS = [origin.strip().rstrip("/") for origin in os.getenv(
    "SKYLYTER_ALLOWED_ORIGINS", _default_origins
).split(",") if origin.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Razorpay-Signature", "X-Razorpay-Event-Id"],
)


def get_db():
    """Connect using the DATABASE_URL provided by the deployment environment."""
    return psycopg.connect(
        DATABASE_URL,
        connect_timeout=15,
        row_factory=dict_row,
    )


def init_db():
    """Verify the migrated PostgreSQL table exists."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.tables
                    WHERE table_schema = 'public'
                      AND table_name = 'website_users'
                ) AS table_exists
            """)
            if not cur.fetchone()["table_exists"]:
                raise RuntimeError(
                    "website_users table was not found in Neon."
                )


def init_payment_table():
    """Create a table to track Student Plus payment orders."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS website_payments (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL
                        REFERENCES website_users(id),
                    razorpay_order_id TEXT UNIQUE NOT NULL,
                    razorpay_payment_id TEXT UNIQUE,
                    amount_paise INTEGER NOT NULL,
                    currency TEXT NOT NULL DEFAULT 'INR',
                    status TEXT NOT NULL DEFAULT 'created'
                        CHECK (status IN ('created', 'paid', 'failed')),
                    plan TEXT NOT NULL DEFAULT 'student_plus',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    paid_at TIMESTAMPTZ
                )
            """)
            # Backward-compatible schema migration for existing production databases.
            cur.execute("ALTER TABLE website_users ADD COLUMN IF NOT EXISTS plan_expires_at TIMESTAMPTZ")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS skylyter_razorpay_webhook_events (
                    event_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)

init_db()
init_payment_table()

class RegisterRequest(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=10, max_length=128)


class LoginRequest(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=1, max_length=128)


def normalize_email(email: str) -> str:
    email = email.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(
            status_code=400,
            detail="Enter a valid email address.",
        )
    return email


def hash_password(password: str, salt: bytes) -> str:
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=2**14,
        r=8,
        p=1,
        dklen=32,
    )
    return derived.hex()


def create_token(user_id: int, email: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "email": email,
        "iat": now,
        "exp": now + timedelta(hours=TOKEN_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def get_authenticated_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
):
    token = credentials.credentials

    try:
        payload = jwt.decode(
            token,
            JWT_SECRET,
            algorithms=[JWT_ALGORITHM],
        )
        user_id = int(payload["sub"])
    except (jwt.InvalidTokenError, KeyError, ValueError, TypeError):
        raise HTTPException(
            status_code=401,
            detail="Your login session is invalid or expired. Please log in again.",
        )

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, email, plan, plan_expires_at
                FROM website_users
                WHERE id = %s
                """,
                (user_id,),
            )
            user = cur.fetchone()

    if user is None:
        raise HTTPException(status_code=401, detail="Account not found.")

    effective_plan = user["plan"]
    expires_at = user.get("plan_expires_at")
    now = datetime.now(timezone.utc)
    if effective_plan in ("plus", "student_plus") and expires_at and expires_at <= now:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE website_users SET plan = 'free', plan_expires_at = NULL WHERE id = %s",
                    (user["id"],),
                )
        effective_plan = "free"
        expires_at = None
    return {
        "id": user["id"],
        "name": user["name"],
        "email": user["email"],
        "plan": effective_plan,
        "subscription_status": "active" if effective_plan in ("plus", "student_plus") and expires_at else "free",
        "plan_expires_at": expires_at.isoformat() if expires_at else None,
    }




# Student Plus checkout: ₹199 for one month (manual renewal; no auto-renewal).
STUDENT_PLUS_AMOUNT_PAISE = 19900


class CreateOrderRequest(BaseModel):
    plan: str = Field(default="student_plus")
    billing_period: str = Field(default="monthly")


class VerifyPaymentRequest(BaseModel):
    razorpay_payment_id: str = Field(min_length=5, max_length=100)
    razorpay_order_id: str = Field(min_length=5, max_length=100)
    razorpay_signature: str = Field(min_length=10, max_length=256)


@app.post("/api/subscriptions/create-order")
def create_subscription_order(
    data: CreateOrderRequest,
    user: dict = Depends(get_authenticated_user),
):
    if data.plan != "student_plus" or data.billing_period != "monthly":
        raise HTTPException(status_code=400, detail="Unsupported subscription plan.")
    if user["plan"] not in ("free", "student_plus", "plus"):
        raise HTTPException(status_code=403, detail="This account cannot purchase a student plan.")

    try:
        order = razorpay_client.order.create({
            "amount": STUDENT_PLUS_AMOUNT_PAISE,
            "currency": "INR",
            "receipt": f"skylyter_{user['id']}_{secrets.token_hex(6)}",
            "notes": {
                "user_id": str(user["id"]),
                "plan": "student_plus",
                "billing_period": "monthly",
            },
        })
    except Exception:
        raise HTTPException(status_code=502, detail="Could not create a Razorpay order. Please try again.")

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO website_payments
                    (user_id, razorpay_order_id, amount_paise, currency, status, plan)
                VALUES (%s, %s, %s, 'INR', 'created', 'student_plus')
                """,
                (user["id"], order["id"], STUDENT_PLUS_AMOUNT_PAISE),
            )

    return {
        "key_id": RAZORPAY_KEY_ID,
        "order_id": order["id"],
        "amount": STUDENT_PLUS_AMOUNT_PAISE,
        "currency": "INR",
    }


@app.post("/api/subscriptions/verify-payment")
def verify_subscription_payment(
    data: VerifyPaymentRequest,
    user: dict = Depends(get_authenticated_user),
):
    # First ensure this order belongs to the currently logged-in user.
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, amount_paise, currency, status
                FROM website_payments
                WHERE razorpay_order_id = %s AND user_id = %s
                """,
                (data.razorpay_order_id, user["id"]),
            )
            payment_order = cur.fetchone()

    if payment_order is None:
        raise HTTPException(status_code=404, detail="Payment order was not found for this account.")

    # Verify the Razorpay signature on the server.
    signed_payload = f"{data.razorpay_order_id}|{data.razorpay_payment_id}".encode("utf-8")
    expected_signature = hmac.new(
        RAZORPAY_KEY_SECRET.encode("utf-8"),
        signed_payload,
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected_signature, data.razorpay_signature):
        raise HTTPException(status_code=400, detail="Payment signature verification failed.")

    try:
        razorpay_payment = razorpay_client.payment.fetch(data.razorpay_payment_id)
    
    except Exception as exc:
        print(
            "Razorpay payment fetch failed:",
            type(exc).__name__,
            str(exc),
        )
        raise HTTPException(
            status_code=502,
            detail="Could not confirm payment status with Razorpay.",
        )

    if (
        razorpay_payment.get("order_id") != data.razorpay_order_id
        or razorpay_payment.get("amount") != payment_order["amount_paise"]
        or razorpay_payment.get("currency") != payment_order["currency"]
        or razorpay_payment.get("status") != "captured"
    ):
        raise HTTPException(status_code=400, detail="Payment is not captured or does not match this order.")

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM website_payments WHERE id = %s FOR UPDATE",
                (payment_order["id"],),
            )
            current_order = cur.fetchone()
            if current_order and current_order["status"] != "paid":
                cur.execute(
                    """
                    UPDATE website_payments
                    SET razorpay_payment_id = %s, status = 'paid', paid_at = NOW()
                    WHERE id = %s AND status <> 'paid'
                    """,
                    (data.razorpay_payment_id, payment_order["id"]),
                )
                cur.execute(
                    """
                    UPDATE website_users
                    SET plan = 'plus',
                        plan_expires_at = GREATEST(COALESCE(plan_expires_at, NOW()), NOW()) + INTERVAL '30 days'
                    WHERE id = %s
                    """,
                    (user["id"],),
                )

    return {
        "verified": True,
        "plan": "student_plus",
        "message": "Payment verified. Student Plus access has been enabled for this account.",
    }



@app.post("/api/webhooks/razorpay")
async def razorpay_webhook(request: Request):
    """Verify Razorpay webhook signatures and process captured payments idempotently."""
    if not RAZORPAY_WEBHOOK_SECRET:
        raise HTTPException(status_code=503, detail="Payment webhook is not configured.")
    raw_body = await request.body()
    supplied_signature = request.headers.get("X-Razorpay-Signature", "")
    expected_signature = hmac.new(
        RAZORPAY_WEBHOOK_SECRET.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    if not supplied_signature or not hmac.compare_digest(expected_signature, supplied_signature):
        raise HTTPException(status_code=400, detail="Invalid webhook signature.")
    event_id = request.headers.get("X-Razorpay-Event-Id")
    if not event_id:
        raise HTTPException(status_code=400, detail="Missing Razorpay event ID.")
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid webhook JSON.")
    event_type = payload.get("event", "unknown")
    if event_type not in ("payment.captured", "order.paid"):
        return {"received": True, "ignored": True}
    payment = ((payload.get("payload") or {}).get("payment") or {}).get("entity") or {}
    if not payment:
        # order.paid may include a payment entity depending on event payload; without
        # verified payment details we intentionally do not grant subscription access.
        return {"received": True, "ignored": True}
    order_id = payment.get("order_id")
    payment_id = payment.get("id")
    amount = payment.get("amount")
    currency = payment.get("currency")
    status = payment.get("status")
    if not order_id or not payment_id or status != "captured":
        return {"received": True, "ignored": True}
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, user_id, amount_paise, currency, status FROM website_payments WHERE razorpay_order_id = %s FOR UPDATE", (order_id,))
            row = cur.fetchone()
            if row is None or row["amount_paise"] != amount or row["currency"] != currency:
                return {"received": True, "ignored": True}
            cur.execute("INSERT INTO skylyter_razorpay_webhook_events (event_id, event_type) VALUES (%s, %s) ON CONFLICT (event_id) DO NOTHING RETURNING event_id", (event_id, event_type))
            inserted = cur.fetchone()
            if not inserted:
                return {"received": True, "duplicate": True}
            if row["status"] != "paid":
                cur.execute("UPDATE website_payments SET razorpay_payment_id = %s, status = 'paid', paid_at = NOW() WHERE id = %s AND status <> 'paid'", (payment_id, row["id"]))
                cur.execute("UPDATE website_users SET plan = 'plus', plan_expires_at = GREATEST(COALESCE(plan_expires_at, NOW()), NOW()) + INTERVAL '30 days' WHERE id = %s", (row["user_id"],))
    return {"received": True, "processed": True}


@app.get("/api/account/me")
def account_me(user: dict = Depends(get_authenticated_user)):
    return {"success": True, "user": user}


@app.get("/api/health")
def health_check():
    return {
        "success": True,
        "message": "Skylyter website backend connection is working",
    }


@app.post("/api/auth/register", status_code=201)
def register(data: RegisterRequest):
    name = data.name.strip()
    if len(name) < 2:
        raise HTTPException(status_code=400, detail="Enter your full name.")

    email = normalize_email(data.email)

    if not re.search(r"[A-Za-z]", data.password) or not re.search(
        r"\d", data.password
    ):
        raise HTTPException(
            status_code=400,
            detail="Password must contain at least one letter and one number.",
        )

    salt = secrets.token_bytes(16)
    password_hash = hash_password(data.password, salt)

    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO website_users
                        (name, email, password_hash, password_salt, plan, created_at)
                    VALUES (%s, %s, %s, %s, 'free', %s)
                    RETURNING id
                    """,
                    (
                        name,
                        email,
                        password_hash,
                        salt.hex(),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                user_id = cur.fetchone()["id"]
    except psycopg.errors.UniqueViolation:
        raise HTTPException(
            status_code=409,
            detail="An account with this email already exists.",
        )

    return {
        "success": True,
        "message": "Account created successfully.",
        "user": {
            "id": user_id,
            "name": name,
            "email": email,
            "plan": "free",
        },
    }


@app.post("/api/auth/login")
def login(data: LoginRequest):
    email = normalize_email(data.email)

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, email, password_hash, password_salt, plan
                FROM website_users
                WHERE email = %s
                """,
                (email,),
            )
            user = cur.fetchone()

    if user is None:
        raise HTTPException(
            status_code=401,
            detail="Invalid email or password.",
        )

    salt = bytes.fromhex(user["password_salt"])
    submitted_hash = hash_password(data.password, salt)

    if not hmac.compare_digest(submitted_hash, user["password_hash"]):
        raise HTTPException(
            status_code=401,
            detail="Invalid email or password.",
        )

    token = create_token(user["id"], user["email"])

    return {
        "success": True,
        "message": "Login successful.",
        "access_token": token,
        "token_type": "bearer",
        "expires_in": TOKEN_HOURS * 3600,
        "user": {
            "id": user["id"],
            "name": user["name"],
            "email": user["email"],
            "plan": user["plan"],
        },
    }

