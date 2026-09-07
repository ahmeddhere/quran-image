"""On-demand Qur'an Mushaf page-image rendering service.

Given a device's screen width the service renders the requested Madani Mushaf
page from the King Fahd Complex "QCF v1" fonts, caches it (RAM + disk), and
serves it over HTTP.  See ``SERVER.md`` for the architecture and API.

Public surface:
    quran_image.server   - the FastAPI app (``uvicorn quran_image.server:app``)
    quran_image.service  - RenderService (cache tiers + process pool)
    quran_image.dimensions, quran_image.assets - request/asset contracts
"""
__version__ = "2.0.0"
