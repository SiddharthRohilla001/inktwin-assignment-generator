import io
import os
import re
import secrets
import smtplib
import sqlite3
from typing import Annotated

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field

load_dotenv()

from handwriting_agent import auth
from handwriting_agent.agent import (
    AgentConfigurationError,
    AgentProviderError,
    HandwritingAgent,
)
from handwriting_agent.schemas import AgentResult, HealthResponse


MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "10")) * 1024 * 1024
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
agent = HandwritingAgent()


app = FastAPI(
    title="Likho Handwriting Agent",
    description="Analyze a handwriting sample and generate page-by-page assignment content.",
    version="1.0.0",
)
bearer_scheme = HTTPBearer(auto_error=False)

origins = [
    origin.strip()
    for origin in os.getenv("CORS_ORIGINS", "http://localhost:3000,http://localhost:5173").split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Authorization"],
)


def validate_image(data: bytes) -> str:
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in {"JPEG", "PNG", "WEBP"}:
                raise HTTPException(status_code=415, detail="Upload a JPG, PNG, or WebP image.")
            if image.width < 64 or image.height < 64:
                raise HTTPException(status_code=400, detail="The handwriting image is too small to analyze.")
            if image.width * image.height > 25_000_000:
                raise HTTPException(status_code=413, detail="Image dimensions exceed the analysis limit.")
            image.verify()
            return {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}[image.format]
    except UnidentifiedImageError as exc:
        raise HTTPException(status_code=400, detail="The uploaded file is not a valid image.") from exc
    except Image.DecompressionBombError as exc:
        raise HTTPException(status_code=413, detail="Image dimensions exceed the analysis limit.") from exc
    except OSError as exc:
        raise HTTPException(status_code=400, detail="The uploaded image could not be read.") from exc


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(
        status="ok" if agent.is_configured else "not_configured",
        provider_configured=agent.is_configured,
    )


class AccountCredentials(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=10, max_length=128)


class EmailVerification(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    code: str = Field(pattern=r"^\d{6}$")


class EmailAddress(BaseModel):
    email: str = Field(min_length=3, max_length=254)


def _validate_email(email: str) -> str:
    normalized = email.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", normalized):
        raise HTTPException(status_code=422, detail="Enter a valid email address.")
    return normalized


def _make_token(user_id: int) -> str:
    try:
        return auth.create_token(user_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


async def require_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> dict:
    if credentials is None:
        raise HTTPException(status_code=401, detail="Sign in to continue.")
    user_id = auth.decode_token(credentials.credentials)
    user = auth.get_user(user_id) if user_id is not None else None
    if user is None or not user["verified"]:
        raise HTTPException(status_code=401, detail="Your session is invalid or expired. Please sign in again.")
    return user


@app.post("/api/v1/auth/register", status_code=202)
async def register_account(credentials: AccountCredentials) -> dict[str, str]:
    email = _validate_email(credentials.email)
    if len(credentials.password) < 10:
        raise HTTPException(status_code=422, detail="Password must be at least 10 characters.")
    try:
        auth.validate_configuration()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    code = f"{secrets.randbelow(1_000_000):06d}"
    try:
        user_id = auth.create_user(email, credentials.password, code)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="An account with this email already exists.") from exc
    try:
        auth.send_verification_email(email, code)
    except (RuntimeError, OSError, smtplib.SMTPException, ValueError) as exc:
        auth.delete_unverified_user(user_id)
        raise HTTPException(
            status_code=503,
            detail="Verification email could not be sent. Check the SMTP settings and try again.",
        ) from exc
    return {"message": "Check your email for the six-digit verification code."}


@app.post("/api/v1/auth/verify")
async def verify_account(verification: EmailVerification) -> dict:
    email = _validate_email(verification.email)
    user = auth.verify_email(email, verification.code)
    if user is None:
        raise HTTPException(status_code=400, detail="Verification code is invalid or expired.")
    return {"access_token": _make_token(int(user["id"])), "token_type": "bearer", "user": auth.get_user(int(user["id"]))}


@app.post("/api/v1/auth/resend-verification", status_code=202)
async def resend_verification(address: EmailAddress) -> dict[str, str]:
    email = _validate_email(address.email)
    try:
        code = auth.create_verification_code(email)
        if code:
            auth.send_verification_email(email, code)
    except (RuntimeError, OSError, smtplib.SMTPException, ValueError) as exc:
        raise HTTPException(
            status_code=503,
            detail="Verification email could not be sent. Check the SMTP settings and try again.",
        ) from exc
    return {"message": "If this email has an unverified account, a new code will be sent when resend is available."}


@app.post("/api/v1/auth/login")
async def login_account(credentials: AccountCredentials) -> dict:
    email = _validate_email(credentials.email)
    user = auth.authenticate(email, credentials.password)
    if user is None:
        raise HTTPException(status_code=401, detail="Email or password is incorrect.")
    if not user["verified"]:
        raise HTTPException(status_code=403, detail="Verify your email before signing in.")
    return {"access_token": _make_token(int(user["id"])), "token_type": "bearer", "user": auth.get_user(int(user["id"]))}


@app.get("/api/v1/auth/me")
async def current_account(user: Annotated[dict, Depends(require_user)]) -> dict:
    return user


@app.post("/api/v1/pages", response_model=AgentResult)
async def create_pages(
    sample: Annotated[UploadFile, File(description="Photo or scan of the user's handwriting.")],
    prompt: Annotated[str, Form(min_length=1, max_length=4000)],
    user: Annotated[dict, Depends(require_user)],
    language: Annotated[str, Form(min_length=1, max_length=80)] = "English",
    page_count: Annotated[int, Form(ge=1, le=10)] = 1,
) -> AgentResult:
    if sample.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=415, detail="Upload a JPG, PNG, or WebP image.")
    if not prompt.strip():
        raise HTTPException(status_code=422, detail="Prompt must not be blank.")

    image_data = await sample.read(MAX_UPLOAD_BYTES + 1)
    if len(image_data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Image exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB upload limit.",
        )
    media_type = validate_image(image_data)

    reservation = auth.reserve_assignment(int(user["id"]))
    if reservation is None:
        raise HTTPException(status_code=403, detail="A verified account is required to generate assignment pages.")
    reservation_id, _ = reservation
    try:
        result = await agent.generate(
            image=image_data,
            image_media_type=media_type,
            prompt=prompt.strip(),
            language=language.strip(),
            page_count=page_count,
        )
        auth.finish_assignment(reservation_id, succeeded=True)
        return result
    except AgentConfigurationError as exc:
        auth.finish_assignment(reservation_id, succeeded=False)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except AgentProviderError as exc:
        auth.finish_assignment(reservation_id, succeeded=False)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception:
        auth.finish_assignment(reservation_id, succeeded=False)
        raise
