from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .. import auth
from ..services import settings

router = APIRouter(prefix="/api/auth", tags=["auth"])


class SetupBody(BaseModel):
    passcode: str = Field(..., min_length=6, max_length=200)
    name: str = Field("", max_length=60)


class LoginBody(BaseModel):
    passcode: str = Field(..., max_length=200)


class ChangeBody(BaseModel):
    current: str = Field(..., max_length=200)
    new: str = Field(..., min_length=6, max_length=200)


def _set_cookie(response: Response, request: Request, token: str) -> None:
    response.set_cookie(auth.COOKIE_NAME, token, max_age=auth.SESSION_DAYS * 86400, httponly=True,
                        samesite="strict", secure=request.url.scheme == "https", path="/")


@router.get("/status")
def status(request: Request):
    return {
        "setup_required": not auth.is_setup(),
        "authenticated": auth.session_valid(request.cookies.get(auth.COOKIE_NAME)),
        "local_request": auth.is_loopback(request),
        "name": settings.get("profile.name") or "",
    }


@router.post("/setup")
def setup(body: SetupBody, request: Request, response: Response):
    if auth.is_setup():
        raise HTTPException(409, "A passcode already exists. Sign in instead.")
    if not auth.is_loopback(request):
        raise HTTPException(403, "For safety, create the passcode on the laptop itself (http://localhost).")
    auth.set_passcode(body.passcode)
    if body.name.strip():
        settings.put("profile.name", body.name.strip())
    _set_cookie(response, request, auth.login(body.passcode, request))
    return {"ok": True}


@router.post("/login")
def login(body: LoginBody, request: Request, response: Response):
    if not auth.is_setup():
        raise HTTPException(409, "No passcode yet. Open E.V. on the laptop to create one.")
    try:
        token = auth.login(body.passcode, request)
    except PermissionError as e:
        raise HTTPException(401, str(e)) from e
    _set_cookie(response, request, token)
    return {"ok": True}


@router.post("/logout")
def logout(request: Request, response: Response):
    auth.logout(request.cookies.get(auth.COOKIE_NAME))
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return {"ok": True}


@router.post("/change")
def change(body: ChangeBody, request: Request, response: Response):
    if not auth.session_valid(request.cookies.get(auth.COOKIE_NAME)):
        raise HTTPException(401, "Sign in first.")
    try:
        auth.change_passcode(body.current, body.new)
    except (PermissionError, ValueError) as e:
        raise HTTPException(400, str(e)) from e
    _set_cookie(response, request, auth.login(body.new, request))
    return {"ok": True, "message": "Passcode changed. Other devices were signed out."}
