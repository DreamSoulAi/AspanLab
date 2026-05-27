"""
Subscription gating.

Single source of truth: is this user's subscription valid right now?

States:
  active  → plan_expires in the future
  grace   → expired ≤ GRACE_DAYS ago (still works, warnings shown)
  blocked → expired > GRACE_DAYS ago (API calls return 402)

Admins (is_admin=True) bypass all checks.
"""

from datetime import datetime, timedelta
from typing import Literal

from backend.models.user import User

TRIAL_DAYS = 7
GRACE_DAYS = 3

Status = Literal["active", "grace", "blocked"]


def get_status(user: User) -> Status:
    if user.is_admin:
        return "active"
    if not user.plan_expires:
        return "blocked"
    now = datetime.utcnow()
    if user.plan_expires > now:
        return "active"
    if user.plan_expires + timedelta(days=GRACE_DAYS) > now:
        return "grace"
    return "blocked"


def days_left(user: User) -> int:
    """Days until plan_expires. Negative = already in grace/blocked."""
    if not user.plan_expires:
        return -999
    return (user.plan_expires - datetime.utcnow()).days


def extend(user: User, days: int) -> None:
    """Add `days` to plan_expires (or start from now if already expired)."""
    now = datetime.utcnow()
    base = user.plan_expires if user.plan_expires and user.plan_expires > now else now
    user.plan_expires = base + timedelta(days=days)
