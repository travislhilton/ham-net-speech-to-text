import os
from sqlalchemy import create_engine, Column, BigInteger, Integer, Text, Numeric, DateTime, JSON, ARRAY
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()
engine = create_engine(os.environ["EMCOMM_DB_DSN"])
Session = sessionmaker(engine)


class Net(Base):
    __tablename__ = "nets"
    id = Column(Integer, primary_key=True)
    name = Column(Text); frequency_mhz = Column(Numeric)
    started_utc = Column(DateTime(timezone=True)); ended_utc = Column(DateTime(timezone=True))
    net_control = Column(Text); notes = Column(Text)


class Transmission(Base):
    __tablename__ = "transmissions"
    id = Column(BigInteger, primary_key=True)
    net_id = Column(Integer)
    started_utc = Column(DateTime(timezone=True)); duration_s = Column(Numeric)
    frequency_mhz = Column(Numeric); net_name = Column(Text); station = Column(Text)
    wav_path = Column(Text); text = Column(Text); callsigns = Column(ARRAY(Text))
    segments = Column(JSON); model = Column(Text); host = Column(Text); structured = Column(JSON)
