"""
Known Florida open-data source registry.

Each entry describes a data portal and its API protocol so the
SourceDiscoveryEngine knows how to probe it.  Protocols:
  - socrata       : Socrata Open Data API (SODA) — returns dataset catalog
  - arcgis        : ArcGIS REST API — /rest/services tree
  - ckan          : CKAN Data Portal — /api/3/action/package_list
  - direct        : Single known file/endpoint, no discovery needed
  - web           : Unstructured / gated site — requires WebCrawler
                    (HTML tables, file downloads behind navigation,
                     JS-rendered pages, login-gated community portals)
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Literal

DataCategory = Literal[
    "property", "tax", "flood", "schools", "crime", "transit",
    "geospatial", "hoa", "community", "legal", "misc"
]
Protocol = Literal["socrata", "arcgis", "ckan", "direct", "web"]


@dataclass(frozen=True)
class FloridaSource:
    name: str
    base_url: str
    protocol: Protocol
    categories: list[DataCategory]
    notes: str = ""
    # Socrata domain (e.g. "data.example.gov") — equals hostname for Socrata portals
    socrata_domain: str = ""
    # Web-crawler options (only relevant when protocol == "web")
    requires_js: bool = False       # True → use Playwright instead of httpx
    requires_auth: bool = False     # True → session/cookie injection needed
    crawl_depth: int = 1            # How many link-hops to follow from base_url
    auth_notes: str = ""            # Human-readable auth instructions
    crawl_seed_paths: list[str] = field(default_factory=list)
    # Paths relative to base_url to use as crawl entry points (in addition to base_url)


# ---------------------------------------------------------------------------
# Statewide / multi-county portals  (structured APIs)
# ---------------------------------------------------------------------------
STATEWIDE_SOURCES: list[FloridaSource] = [
    FloridaSource(
        name="Florida Department of Revenue — Property Tax",
        base_url="https://floridarevenue.com/property/Pages/DataPortal.aspx",
        protocol="direct",
        categories=["property", "tax"],
        notes="Annual NAL (Name-Address-Legal) files by county; CSV/ZIP downloads.",
    ),
    FloridaSource(
        name="Florida Geographic Data Library (FGDL)",
        base_url="https://www.fgdl.org/metadatafull/fgdl_html/",
        protocol="direct",
        categories=["geospatial"],
        notes="State-maintained GIS clearinghouse; shapefiles and metadata.",
    ),
    FloridaSource(
        name="Florida Department of Environmental Protection — GIS",
        base_url="https://geodata.dep.state.fl.us/",
        protocol="arcgis",
        categories=["geospatial", "flood"],
        notes="DEP ArcGIS REST services including wetlands, floodplain layers.",
    ),
    FloridaSource(
        name="Florida Division of Emergency Management — Flood Zones",
        base_url="https://floridadisaster.org/",
        protocol="direct",
        categories=["flood"],
        notes="NFIP flood zone maps; links to FEMA FIRM panels for FL.",
    ),
    FloridaSource(
        name="NOAA — National Flood Hazard Layer",
        base_url="https://hazards.fema.gov/gis/nfhl/rest/services/public/NFHL/MapServer",
        protocol="arcgis",
        categories=["flood"],
        notes="Federal authoritative flood hazard data for all counties.",
    ),
    FloridaSource(
        name="Florida Department of Education — School Data",
        base_url="https://www.fldoe.org/accountability/",
        protocol="direct",
        categories=["schools"],
        notes="Annual school grades and accountability reports (Excel/CSV).",
    ),
    FloridaSource(
        name="Florida Department of Transportation — Open Data",
        base_url="https://gis-fdot.opendata.arcgis.com",
        protocol="arcgis",
        categories=["transit", "geospatial"],
        notes="Transit routes, road network, SunRail alignments.",
    ),
    FloridaSource(
        name="Florida Crime Data — FDLE",
        base_url="https://www.fdle.state.fl.us/FSAC/CJAB-Home",
        protocol="direct",
        categories=["crime"],
        notes="Florida Uniform Crime Reports (UCR); Excel/CSV by agency.",
    ),
    FloridaSource(
        name="US Census Bureau — American Community Survey (FL)",
        base_url="https://api.census.gov/data/2022/acs/acs5",
        protocol="direct",
        categories=["misc"],
        notes="Demographic and housing statistics; REST JSON API.",
    ),
]

# ---------------------------------------------------------------------------
# County-level portals  (Socrata & ArcGIS)
# ---------------------------------------------------------------------------
COUNTY_SOURCES: list[FloridaSource] = [
    # Miami-Dade
    FloridaSource(
        name="Miami-Dade County Open Data",
        base_url="https://gis-mdc.opendata.arcgis.com",
        protocol="arcgis",
        categories=["property", "geospatial", "misc"],
        notes=(
            "Property boundaries, zoning, transit layers. Legacy Socrata portal "
            "opendata.miamidade.gov is decommissioned (API 404, verified 2026-10-02); "
            "use this ArcGIS hub instead."
        ),
    ),
    FloridaSource(
        name="Miami-Dade Property Appraiser",
        base_url="https://www.miamidade.gov/Apps/PA/propertysearch/",
        protocol="direct",
        categories=["property", "tax"],
        notes="Parcel-level property data; bulk export available.",
    ),
    # Broward
    FloridaSource(
        name="Broward County Open Data",
        base_url="https://services9.arcgis.com/RHVPKKiFTONKtxq3/arcgis/rest/services",
        protocol="arcgis",
        categories=["property", "geospatial", "misc"],
        notes=(
            "Migrated from Socrata (opendata.broward.org, decommissioned) to the "
            "Broward County GeoHub ArcGIS Open Data portal. Service root verified "
            "live 2026-10-02 (~70 FeatureServers). Human-facing portal: "
            "https://geohub-bcgis.opendata.arcgis.com"
        ),
    ),
    FloridaSource(
        name="Broward County Property Appraiser",
        base_url="https://bcpa.net/",
        protocol="direct",
        categories=["property", "tax"],
        notes="Parcel data; bulk downloads for NAL.",
    ),
    # Palm Beach
    FloridaSource(
        name="Palm Beach County Open Data",
        base_url="https://discover.pbcgov.org/pzb/gis/",
        protocol="arcgis",
        categories=["property", "geospatial"],
    ),
    FloridaSource(
        name="Palm Beach County Property Appraiser",
        base_url="https://www.pbcgov.org/papa/",
        protocol="direct",
        categories=["property", "tax"],
    ),
    # Hillsborough (Tampa)
    FloridaSource(
        name="Hillsborough County Open Data",
        base_url="https://gis.hillsboroughcounty.org/",
        protocol="arcgis",
        categories=["property", "geospatial", "misc"],
    ),
    FloridaSource(
        name="Hillsborough County Property Appraiser",
        base_url="https://www.hcpafl.org/",
        protocol="direct",
        categories=["property", "tax"],
    ),
    # Orange (Orlando)
    FloridaSource(
        name="Orange County Property Appraiser",
        base_url="https://www.ocpafl.org/",
        protocol="direct",
        categories=["property", "tax"],
    ),
    FloridaSource(
        name="Orange County GIS",
        base_url="https://ocfl.maps.arcgis.com/",
        protocol="arcgis",
        categories=["geospatial"],
    ),
    # Pinellas (St. Pete)
    FloridaSource(
        name="Pinellas County Property Appraiser",
        base_url="https://www.pcpao.gov/",
        protocol="direct",
        categories=["property", "tax"],
    ),
    # Duval (Jacksonville)
    FloridaSource(
        name="Jacksonville / Duval County Open Data",
        base_url="https://data.jacksonville.com",
        protocol="web",
        categories=["property", "crime", "misc"],
        notes=(
            "Replaced dead Socrata portal opendata.coj.net (NXDOMAIN, 2026-10-02). "
            "Platform of data.jacksonville.com unverified — it bot-blocks automated "
            "clients (403/406), so the web-crawler path is used. Re-check for a "
            "structured API before relying on this source."
        ),
        requires_js=True,
        crawl_depth=1,
    ),
    FloridaSource(
        name="Duval County Property Appraiser",
        base_url="https://www.coj.net/departments/property-appraiser.aspx",
        protocol="direct",
        categories=["property", "tax"],
    ),
    # Lee (Fort Myers)
    FloridaSource(
        name="Lee County Property Appraiser",
        base_url="https://www.leepa.org/",
        protocol="direct",
        categories=["property", "tax"],
    ),
    # Collier (Naples)
    FloridaSource(
        name="Collier County Property Appraiser",
        base_url="https://www.collierappraiser.com/",
        protocol="direct",
        categories=["property", "tax"],
    ),
    # Sarasota
    FloridaSource(
        name="Sarasota County Open Data",
        base_url="https://gis.sarasotacountyfl.gov/",
        protocol="arcgis",
        categories=["property", "geospatial"],
    ),
    # Polk (Lakeland)
    FloridaSource(
        name="Polk County Property Appraiser",
        base_url="https://www.polkpa.org/",
        protocol="direct",
        categories=["property", "tax"],
    ),
]

# ---------------------------------------------------------------------------
# Unstructured sources  (protocol="web")
# Requires WebCrawler — HTML pages, file downloads behind navigation,
# JS-rendered portals, and login-gated community / HOA sites.
# ---------------------------------------------------------------------------
UNSTRUCTURED_SOURCES: list[FloridaSource] = [

    # -----------------------------------------------------------------------
    # State / regulatory — unstructured HTML but publicly accessible
    # -----------------------------------------------------------------------
    FloridaSource(
        name="Florida DBPR — HOA & Condo Registration Search",
        base_url="https://www.myfloridalicense.com/wl11.asp",
        protocol="web",
        categories=["hoa", "community"],
        notes=(
            "FL Dept of Business and Professional Regulation public search "
            "for registered HOAs and condo associations. HTML form results; "
            "no bulk download available. Crawl search results by county."
        ),
        requires_js=False,
        crawl_depth=2,
        crawl_seed_paths=["/wl11.asp?SID=&searchTerm=&licenseType=HOA"],
    ),
    FloridaSource(
        name="Florida Dept of Revenue — NAL Bulk Data Pages",
        base_url="https://floridarevenue.com/property/Pages/DataPortal_RequestAssessmentRollGISData.aspx",
        protocol="web",
        categories=["property", "tax"],
        notes=(
            "Bulk NAL/GIS assessment roll request pages — download links are "
            "behind an HTML form (county selector). Crawl to extract per-county "
            "download URLs for CSV and shapefile packages."
        ),
        requires_js=False,
        crawl_depth=1,
    ),
    FloridaSource(
        name="Florida Courts E-Filing — Public Foreclosure Records",
        base_url="https://www.myeclerk.myorangeclerk.com/",
        protocol="web",
        categories=["legal", "property"],
        notes=(
            "County clerk public-facing court records search. Foreclosure / "
            "lis pendens filings are public. Each county clerk runs a separate "
            "instance. Orange County entry point listed here; adapt base_url "
            "per county."
        ),
        requires_js=True,
        crawl_depth=2,
    ),
    FloridaSource(
        name="Miami-Dade Clerk of Courts — Public Records",
        base_url="https://www.miami-dadeclerk.com/",
        protocol="web",
        categories=["legal", "property"],
        notes=(
            "Public foreclosure filings, lis pendens, deed transfers. "
            "HTML search interface; JS-rendered result tables."
        ),
        requires_js=True,
        crawl_depth=2,
        crawl_seed_paths=["/public-records/"],
    ),
    FloridaSource(
        name="Broward Clerk of Courts — Public Records",
        base_url="https://www.browardclerk.org/",
        protocol="web",
        categories=["legal", "property"],
        notes="Foreclosure dockets and official records search.",
        requires_js=True,
        crawl_depth=2,
        crawl_seed_paths=["/web2/"],
    ),

    # -----------------------------------------------------------------------
    # HOA / Gated Community aggregators
    # -----------------------------------------------------------------------
    FloridaSource(
        name="HOA-USA Florida — Community Directory",
        base_url="https://www.hoa-usa.com/florida/",
        protocol="web",
        categories=["hoa", "community"],
        notes=(
            "Directory of Florida HOA communities organized by county and city. "
            "Crawl county sub-pages to extract community names, addresses, "
            "management company contacts, and (where listed) monthly dues ranges."
        ),
        requires_js=False,
        crawl_depth=3,
        crawl_seed_paths=[
            "/florida/miami-dade-county/",
            "/florida/broward-county/",
            "/florida/palm-beach-county/",
            "/florida/hillsborough-county/",
            "/florida/orange-county/",
            "/florida/pinellas-county/",
            "/florida/duval-county/",
            "/florida/lee-county/",
            "/florida/collier-county/",
            "/florida/sarasota-county/",
        ],
    ),
    FloridaSource(
        name="Florida Community Association Professionals (FCAP)",
        base_url="https://www.fcapgroup.com/florida-hoa-resources/",
        protocol="web",
        categories=["hoa", "community"],
        notes="Resource hub with links to FL community association management firms and community listings.",
        requires_js=False,
        crawl_depth=2,
    ),

    # -----------------------------------------------------------------------
    # Large planned / gated communities with public-facing data
    # -----------------------------------------------------------------------
    FloridaSource(
        name="The Villages — Community Information",
        base_url="https://www.thevillages.com/",
        protocol="web",
        categories=["community", "property", "hoa"],
        notes=(
            "Nation's largest retirement community (Sumter/Lake/Marion counties). "
            "Public site lists amenities, districts, and home listings. "
            "Bond / CDD assessment data on sub-pages."
        ),
        requires_js=True,
        crawl_depth=2,
        crawl_seed_paths=["/community/", "/real-estate/"],
    ),
    FloridaSource(
        name="Babcock Ranch — Community & CDD Data",
        base_url="https://www.babcockranch.com/",
        protocol="web",
        categories=["community", "property", "hoa"],
        notes="Florida's first solar-powered town (Charlotte County). CDD fees and community data on public pages.",
        requires_js=True,
        crawl_depth=2,
        crawl_seed_paths=["/living-here/", "/community/"],
    ),
    FloridaSource(
        name="Solivita — Community Information (Kissimmee)",
        base_url="https://www.solivita.com/",
        protocol="web",
        categories=["community", "hoa"],
        notes="AV Homes 55+ gated community; Polk County. HOA fees and amenity data on public pages.",
        requires_js=True,
        crawl_depth=2,
    ),
    FloridaSource(
        name="On Top of the World — Community (Ocala)",
        base_url="https://www.ontopoftheworldcommunities.com/",
        protocol="web",
        categories=["community", "hoa"],
        notes="Large 55+ gated community in Marion County. Public amenity and HOA overview pages.",
        requires_js=False,
        crawl_depth=2,
    ),
    FloridaSource(
        name="Pelican Bay Foundation — Naples",
        base_url="https://www.pelicanbay.org/",
        protocol="web",
        categories=["community", "hoa"],
        notes=(
            "Collier County upscale gated community. Foundation publishes "
            "annual assessments and budget documents (PDFs) publicly."
        ),
        requires_js=False,
        crawl_depth=2,
        crawl_seed_paths=["/about/budget-documents/"],
    ),
    FloridaSource(
        name="Sun City Center Community Association",
        base_url="https://www.suncitycenter.org/",
        protocol="web",
        categories=["community", "hoa"],
        notes="Hillsborough County 55+ community. Public docs include assessment schedules.",
        requires_js=False,
        crawl_depth=2,
    ),

    # -----------------------------------------------------------------------
    # CDD (Community Development District) — public taxing authorities
    # -----------------------------------------------------------------------
    FloridaSource(
        name="Florida Department of Economic Opportunity — CDD Registry",
        base_url="https://www.floridajobs.org/community-planning-and-development/community-development-districts",
        protocol="web",
        categories=["hoa", "community", "tax"],
        notes=(
            "Official state registry of all ~2,000 Florida CDDs. Each CDD is a "
            "special-purpose government with publicly filed budgets and assessment "
            "rates. Crawl to extract CDD names, counties, and contact/website links."
        ),
        requires_js=False,
        crawl_depth=2,
    ),
    FloridaSource(
        name="Inframark — CDD District Portals (FL)",
        base_url="https://www.inframarkims.com/community-list/",
        protocol="web",
        categories=["hoa", "community", "tax"],
        notes=(
            "Inframark manages 200+ FL CDDs; community list page links to "
            "individual district sites that publish budgets and assessment schedules."
        ),
        requires_js=False,
        crawl_depth=3,
    ),
    FloridaSource(
        name="Governmental Management Services (GMS) — CDD Portals",
        base_url="https://www.gmscfl.com/districts/",
        protocol="web",
        categories=["hoa", "community", "tax"],
        notes="GMS manages CDDs statewide; each district page links public budget PDFs.",
        requires_js=False,
        crawl_depth=3,
    ),

    # -----------------------------------------------------------------------
    # Login-gated / semi-private (auth required for full data)
    # -----------------------------------------------------------------------
    FloridaSource(
        name="SunBiz — FL Dept of State Business Entity Search",
        base_url="https://search.sunbiz.org/Inquiry/CorporationSearch/SearchResults",
        protocol="web",
        categories=["hoa", "community", "misc"],
        notes=(
            "Public corporate registry — HOA legal entities are registered here. "
            "Search by entity name returns HTML tables of registered agents, "
            "addresses, and officer data. No login required."
        ),
        requires_js=False,
        crawl_depth=2,
        crawl_seed_paths=[
            "/Inquiry/CorporationSearch/SearchResults?inquiryType=EntityName&inquiryDirectionType=ForwardList&searchNameOrder=HOMEOWNERS+ASSOCIATION",
            "/Inquiry/CorporationSearch/SearchResults?inquiryType=EntityName&inquiryDirectionType=ForwardList&searchNameOrder=COMMUNITY+DEVELOPMENT+DISTRICT",
        ],
    ),
    FloridaSource(
        name="Florida MLS Public-Facing Portals (Stellar MLS IDX)",
        base_url="https://www.stellarmls.com/",
        protocol="web",
        categories=["property"],
        notes=(
            "Stellar MLS (Central/South FL) exposes IDX data through member "
            "broker sites. Full MLS access requires licensed agent credentials. "
            "Public search pages provide limited listing data. "
            "Set requires_auth=True and inject session cookies for full access."
        ),
        requires_js=True,
        requires_auth=True,
        crawl_depth=1,
        auth_notes=(
            "Requires Florida-licensed real estate agent NRDS ID and MLS password. "
            "Obtain through Stellar MLS membership. Inject as session cookie "
            "'SMLSSID' after authenticating at https://matrix.stellarmls.com/"
        ),
    ),
    FloridaSource(
        name="BeachesMLS — Palm Beach / Broward Listings",
        base_url="https://www.beachesmls.com/",
        protocol="web",
        categories=["property"],
        notes=(
            "Southeast FL MLS (Palm Beach/Broward/Martin/St. Lucie counties). "
            "Public IDX feeds available via broker websites. "
            "Full data requires licensed agent login."
        ),
        requires_js=True,
        requires_auth=True,
        crawl_depth=1,
        auth_notes=(
            "Requires Beaches MLS member credentials. "
            "Contact membership@rapb.com for data licensing options."
        ),
    ),
]

ALL_SOURCES: list[FloridaSource] = STATEWIDE_SOURCES + COUNTY_SOURCES + UNSTRUCTURED_SOURCES

# Convenience filters
STRUCTURED_SOURCES: list[FloridaSource] = [
    s for s in ALL_SOURCES if s.protocol != "web"
]
WEB_SOURCES: list[FloridaSource] = [
    s for s in ALL_SOURCES if s.protocol == "web"
]
GATED_SOURCES: list[FloridaSource] = [
    s for s in ALL_SOURCES if s.requires_auth
]
JS_REQUIRED_SOURCES: list[FloridaSource] = [
    s for s in ALL_SOURCES if s.requires_js
]
