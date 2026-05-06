-- Auction Database schema
-- Based on Section 3 of DatabaseBrief_v1.0 + approved additions
--
-- ``record_id`` is a DB-generated UUID primary key (``gen_random_uuid()``) —
-- scrapers never send it. Dedup happens on ``listing_url`` UNIQUE (the upsert
-- conflict key).

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS auctions (
    record_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    Title           TEXT NOT NULL,
    Year            INTEGER,
    Make            TEXT,
    Model           TEXT,
    VIN             TEXT,
    Mileage_mi      INTEGER,
    Ext_color       TEXT,
    Int_color       TEXT,
    Engine_spec     TEXT,
    Transmission    TEXT,
    Lot_number      TEXT,
    Estimate_low    INTEGER,
    Estimate_high   INTEGER,
    Estimate_currency TEXT,
    Sold_status     TEXT,
    Auction_status  TEXT,
    Currency        TEXT,
    Selling_price   INTEGER,
    Selling_price_USD REAL,
    FX_rate_used    REAL,
    Selling_date    TEXT,
    Auction_house   TEXT,
    Auction_event   TEXT,
    Country         TEXT,
    Image_url       TEXT,
    Listing_url     TEXT NOT NULL UNIQUE,
    Source          TEXT NOT NULL,
    Listing_description TEXT,
    Scraped_at      TEXT NOT NULL
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_auctions_source ON auctions(Source);
CREATE INDEX IF NOT EXISTS idx_auctions_make ON auctions(Make);
CREATE INDEX IF NOT EXISTS idx_auctions_year ON auctions(Year);
CREATE INDEX IF NOT EXISTS idx_auctions_sold_status ON auctions(Sold_status);
CREATE INDEX IF NOT EXISTS idx_auctions_auction_status ON auctions(Auction_status);
CREATE INDEX IF NOT EXISTS idx_auctions_selling_date ON auctions(Selling_date);
CREATE INDEX IF NOT EXISTS idx_auctions_listing_url ON auctions(Listing_url);
CREATE INDEX IF NOT EXISTS idx_auctions_lot_source ON auctions(Lot_number, Listing_url);
