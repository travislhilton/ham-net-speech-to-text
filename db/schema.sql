-- emcomm_voice schema (PostgreSQL on home server)
CREATE TABLE IF NOT EXISTS nets (
  id SERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  frequency_mhz NUMERIC(8,4),
  started_utc TIMESTAMPTZ NOT NULL,
  ended_utc TIMESTAMPTZ,
  net_control TEXT,
  notes TEXT
);
CREATE TABLE IF NOT EXISTS transmissions (
  id BIGSERIAL PRIMARY KEY,
  net_id INT REFERENCES nets(id),
  started_utc TIMESTAMPTZ NOT NULL,
  duration_s NUMERIC(7,2),
  frequency_mhz NUMERIC(8,4),
  net_name TEXT,
  station TEXT,            -- receiving station
  wav_path TEXT NOT NULL,
  text TEXT,
  callsigns TEXT[],
  segments JSONB,
  model TEXT, host TEXT,
  structured JSONB,        -- analyze/structure.py output
  created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS tx_started ON transmissions(started_utc);
CREATE INDEX IF NOT EXISTS tx_text ON transmissions USING gin (to_tsvector('english', coalesce(text,'')));
