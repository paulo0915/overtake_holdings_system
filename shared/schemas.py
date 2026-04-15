"""Database schemas — exact column definitions from the client brief."""

# Auction Database schema (Section 3 of brief + approved additions)
AUCTION_COLUMNS = [
    "Record_ID",
    "Title",
    "Year",
    "Make",
    "Model",
    "Authenticity",
    "VIN",
    "Mileage_mi",
    "Ext_color",
    "Int_color",
    "Engine_spec",
    "Transmission",
    "Lot_number",
    "Estimate_low",
    "Estimate_high",
    "Estimate_currency",
    "Sold_status",
    "Currency",
    "Selling_price",
    "Selling_price_USD",
    "Bid_price",
    "Bid_price_USD",
    "Bid_count",
    "FX_rate_used",
    "Selling_date",
    "Auction_house",
    "Country",
    "Image_url",
    "Listing_url",
    "Source",
    "Listing_description",
    "Scraped_at",
]

# Classifieds Database schema (Section 4 of brief)
CLASSIFIEDS_COLUMNS = [
    "Record_ID",
    "Title",
    "Year",
    "Make",
    "Model",
    "Authenticity",
    "VIN",
    "Mileage_mi",
    "Ext_color",
    "Int_color",
    "Engine_spec",
    "Transmission",
    "Dealer_name",
    "Asking_price",
    "Currency",
    "Asking_price_USD",
    "FX_rate_used",
    "Listing_status",
    "First_seen_date",
    "Last_seen_date",
    "Last_price_change_date",
    "Previous_asking_price",
    "Country",
    "Image_url",
    "Listing_url",
    "Source",
    "Listing_description",
    "Scraped_at",
]

# Source frequency categories (Section 6 of brief)
AUCTION_DAILY_SOURCES = [
    {"name": "Bring a Trailer", "domain": "bringatrailer.com"},
    {"name": "Cars & Bids", "domain": "carsandbids.com"},
    {"name": "PCarMarket", "domain": "pcarmarket.com"},
    {"name": "DuPont Registry Live", "domain": "live.dupontregistry.com"},
    {"name": "Sotheby's Motorsport", "domain": "sothebysmotorsport.com"},
    {"name": "Hagerty Marketplace", "domain": "hagerty.com"},
]

AUCTION_MONTHLY_SOURCES = [
    {"name": "The MB Market", "domain": "thembmarket.com"},
    {"name": "SBX Cars", "domain": "sbxcars.com"},
    {"name": "Carbuckle", "domain": "carbuckle.com"},
    {"name": "Collecting Cars", "domain": "collectingcars.com"},
    {"name": "The Market", "domain": "themarket.co.uk"},
    {"name": "Car & Classic", "domain": "carandclassic.com"},
]

AUCTION_EVENT_SOURCES = [
    {"name": "RM Sotheby's", "domain": "rmsothebys.com"},
    {"name": "Bonhams", "domain": "cars.bonhams.com"},
    {"name": "Broad Arrow", "domain": "broadarrowauctions.com"},
    {"name": "Gooding & Company", "domain": "goodingco.com"},
    {"name": "Barrett-Jackson", "domain": "barrett-jackson.com"},
    {"name": "Mecum", "domain": "mecum.com"},
    {"name": "Worldwide Auctioneers", "domain": "worldwideauctioneers.com"},
]

CLASSIFIEDS_SOURCES = [
    {"name": "DuPont Registry", "domain": "dupontregistry.com"},
    {"name": "AutoTrader USA", "domain": "autotrader.com"},
    {"name": "Cars.com", "domain": "cars.com"},
    {"name": "CarGurus", "domain": "cargurus.com"},
    {"name": "TrueCar", "domain": "truecar.com"},
    {"name": "UsedCars.com", "domain": "usedcars.com"},
    {"name": "AutoTrader Canada", "domain": "autotrader.ca"},
    {"name": "JamesEdition", "domain": "jamesedition.com"},
]

REFERENCE_SOURCES = [
    {"name": "AutoTempest", "domain": "autotempest.com", "note": "Aggregator — reference only"},
    {"name": "Classic.com", "domain": "classic.com", "note": "Use for traditional auction date validation"},
]
