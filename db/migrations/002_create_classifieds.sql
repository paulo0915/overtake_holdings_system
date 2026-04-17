-- Classifieds Database schema
-- Based on Section 4 of DatabaseBrief_v1.0 + approved additions

CREATE TABLE IF NOT EXISTS classifieds (
    Record_ID               TEXT PRIMARY KEY,
    Title                   TEXT NOT NULL,
    Year                    INTEGER,
    Make                    TEXT,
    Model                   TEXT,
    VIN                     TEXT,
    Mileage_mi              INTEGER,
    Ext_color               TEXT,
    Int_color               TEXT,
    Engine_spec             TEXT,
    Transmission            TEXT,
    Dealer_name             TEXT,
    Asking_price            INTEGER,
    Currency                TEXT,
    Asking_price_USD        REAL,
    FX_rate_used            REAL,
    Listing_status          TEXT DEFAULT 'Active',
    First_seen_date         TEXT,
    Last_seen_date          TEXT,
    Last_price_change_date  TEXT,
    Previous_asking_price   INTEGER,
    Country                 TEXT,
    Image_url               TEXT,
    Listing_url             TEXT NOT NULL,
    Source                  TEXT NOT NULL,
    Listing_description     TEXT,
    Scraped_at              TEXT NOT NULL
);

-- Indexes for dedup and lifecycle queries
CREATE INDEX IF NOT EXISTS idx_classifieds_source ON classifieds(Source);
CREATE INDEX IF NOT EXISTS idx_classifieds_status ON classifieds(Listing_status);
CREATE INDEX IF NOT EXISTS idx_classifieds_vin ON classifieds(VIN);
CREATE INDEX IF NOT EXISTS idx_classifieds_dealer ON classifieds(Dealer_name);
CREATE INDEX IF NOT EXISTS idx_classifieds_url ON classifieds(Listing_url);
CREATE INDEX IF NOT EXISTS idx_classifieds_make ON classifieds(Make);
CREATE INDEX IF NOT EXISTS idx_classifieds_dedup ON classifieds(VIN, Dealer_name, Listing_url);
