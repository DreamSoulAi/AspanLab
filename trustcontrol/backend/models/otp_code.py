from sqlalchemy import Column, Integer, String, Boolean, DateTime
from datetime import datetime
from backend.database import Base


class OtpCode(Base):
    __tablename__ = "otp_codes"

    id         = Column(Integer, primary_key=True)
    email      = Column(String(255), index=True, nullable=False)
    code       = Column(String(6),   nullable=False)
    expires_at = Column(DateTime,    nullable=False)
    used       = Column(Boolean,     default=False)
    created_at = Column(DateTime,    default=datetime.utcnow)
