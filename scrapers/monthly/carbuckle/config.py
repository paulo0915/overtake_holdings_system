"""Constants for Carbuckle auction results."""

# Public read-only Typesense search key (exposed in browser requests by design)
TYPESENSE_HOST = "xiht4zj8lq037woyp-3.a1.typesense.net"
TYPESENSE_API_KEY = "tBRt2aG7iRFN10LJZxyOfIjYVHsDJgso"
COLLECTION = "listings_live"

# Firebase project — used for publicly-readable Firestore REST API
FIREBASE_PROJECT = "carbuckle-live-4542"

BASE_URL = "https://www.carbuckle.com"

# Image CDN — paths are URL-encoded cimg/galleryId/hash.ext segments
IMAGE_CDN_BASE = "https://carbuckle.speedwhole.com/resize//t/lgm/p/"

PER_PAGE = 50
