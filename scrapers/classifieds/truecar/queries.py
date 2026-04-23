"""GraphQL query text for TrueCar's ``getMarketplaceSearch``.

Trimmed from the browser-captured query: we only keep the fields we actually
store (vehicle / pricing / dealership / images + pageInfo for pagination).
Consumer/partner/seo blocks are dropped because they aren't used and some
require authentication headers we don't send.
"""

LIST_QUERY = """
query getMarketplaceSearch(
  $filters: MarketplaceListingFilters!
  $first: Int
  $offset: Int
  $sort: ListingsSort
  $galleryImagesCount: Int!
) {
  marketplaceListingSearch(
    filters: $filters
    first: $first
    offset: $offset
    sort: $sort
  ) {
    __typename
    pageInfo { hasNextPage endCursor }
    totalCount
    edges {
      cursor
      distance
      node {
        id
        galleryImages(galleryImagesCount: $galleryImagesCount) {
          edges { node { url height width } }
        }
        pricing {
          listPrice
          baseMsrp
          totalMsrp
        }
        dealership {
          id
          databaseId
          name
          parentDealershipName
          location {
            address1
            geolocation { city state postalCode }
          }
        }
        vehicle {
          condition
          exteriorColor
          interiorColor
          engine
          bodyStyle
          certifiedPreOwned
          fuelType
          transmission
          vin
          keyFeatures
          make { name slug }
          model { name slug }
          year
          mileage
          style {
            name
            trimName
            driveType
          }
        }
        conditionHistory {
          accidentCount
          ownerCount
          isCleanTitle
        }
      }
    }
  }
}
""".strip()
