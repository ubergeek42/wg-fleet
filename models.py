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
        site: contestforge site id the client enrolled as (fleets from fleets_url only)
        host_key: The machine's ssh host public key, "<type> <base64>" (fleets from fleets_url only)
    """
    __tablename__ = 'clients'

    id = Column(Integer, primary_key=True, autoincrement=True)
    fleet_id = Column(String, nullable=False)
    public_key = Column(String, nullable=False)
    assigned_ip = Column(String, nullable=False)
    http_request_ip = Column(String, nullable=False)
    hostname = Column(String, nullable=True)
    timestamp = Column(DateTime, nullable=False, default=lambda: datetime.now(UTC))
    site = Column(String, nullable=True)
    host_key = Column(String, nullable=True)

    def __repr__(self):
        return f"<Client(fleet={self.fleet_id}, ip={self.assigned_ip}, hostname={self.hostname})>"

class KnownFleet(Base):
    """
    A fleet from fleets_url that has an interface on this server.

    Remembers the addresses each fleet first got, so a later fleets.json can never
    move them (machines keep their configs), and when the fleet left fleets.json, so
    its interface can be torn down once its clients are gone.

    Attributes:
        name: Fleet name
        ip6: Server address in the fleet
        subnet: Fleet subnet
        port: WireGuard port
        removed_at: When the fleet left fleets.json or expired; None while it's served
    """
    __tablename__ = 'known_fleets'

    name = Column(String, primary_key=True)
    ip6 = Column(String, nullable=False)
    subnet = Column(String, nullable=False)
    port = Column(Integer, nullable=False)
    removed_at = Column(DateTime, nullable=True)

    def __repr__(self):
        return f"<KnownFleet(name={self.name}, subnet={self.subnet}, port={self.port})>"
