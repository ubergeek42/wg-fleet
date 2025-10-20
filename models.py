from sqlalchemy import Column, Integer, String, DateTime
from sqlalchemy.orm import declarative_base
from datetime import datetime, UTC

Base = declarative_base()

class Client(Base):
    """
    Database model for registered WireGuard clients.

    Attributes:
        id: Primary key
        fleet_id: Name of the fleet this client belongs to
        public_key: Client's WireGuard public key
        assigned_ip: IPv6 address assigned to client
        http_request_ip: Source IP from registration request
        hostname: Optional client hostname
        timestamp: Last registration or ping time
    """
    __tablename__ = 'clients'

    id = Column(Integer, primary_key=True, autoincrement=True)
    fleet_id = Column(String, nullable=False)
    public_key = Column(String, nullable=False)
    assigned_ip = Column(String, nullable=False)
    http_request_ip = Column(String, nullable=False)
    hostname = Column(String, nullable=True)
    timestamp = Column(DateTime, nullable=False, default=lambda: datetime.now(UTC))

    def __repr__(self):
        return f"<Client(fleet={self.fleet_id}, ip={self.assigned_ip}, hostname={self.hostname})>"
