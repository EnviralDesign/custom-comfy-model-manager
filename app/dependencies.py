from fastapi import Depends, HTTPException, status, Header
from typing import Annotated

from app.services.remote import get_session_manager

async def verify_remote_auth(authorization: Annotated[str | None, Header()] = None):
    """
    Dependency that validates the Bearer token against the active session.
    Returns True if valid, raises 401 otherwise.
    """
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
        )
    
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Authorization header format",
        )
    
    mgr = get_session_manager()
    if not mgr.validate_key(token):
         raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired session key",
        )
    
    return True
