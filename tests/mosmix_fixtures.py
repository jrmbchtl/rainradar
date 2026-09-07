"""Minimal MOSMIX-S KML fixture for parser tests.

Only a handful of elements/stations — enough to exercise the parser paths.
"""

from __future__ import annotations

KML_HEADER = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"
     xmlns:atom="http://www.w3.org/2005/Atom"
     xmlns:dwd="https://opendata.dwd.de/weather/lib/pointforecast_dwd_extension_V1_0.xsd">
<Document>
<name>MOSMIX_S_2026090607_240.kml</name>
"""

KML_FOOTER = """</Document>
</kml>
"""


def _placemark(station_id: str, elements: dict[str, str]) -> str:
    parts = ["<Placemark>", f"<name>{station_id}</name>"]
    for element, values in elements.items():
        parts.append(
            f"<dwd:Forecast dwd:elementName=\"{element}\">"
            f"<dwd:value>{values}</dwd:value>"
            f"</dwd:Forecast>"
        )
    parts.append("</Placemark>")
    return "\n".join(parts)


def build_kml(stations: dict[str, dict[str, str]]) -> bytes:
    """Build a MOSMIX KML bytes blob from {station: {element: values}}."""
    body = "\n".join(_placemark(sid, els) for sid, els in stations.items())
    return (KML_HEADER + body + "\n" + KML_FOOTER).encode("utf-8")


def build_kmz(kml_bytes: bytes) -> bytes:
    """Zip a KML blob into KMZ bytes (in-memory)."""
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("MOSMIX_S_2026090607_240.kml", kml_bytes)
    return buf.getvalue()


# A two-station sample: 01001 has data, 99999 exists with missing values only.
SAMPLE_KML = build_kml(
    {
        "01001": {
            "TTT": "280.15 281.15 282.15 -999",
            "FF": "2.0 3.0 4.0 -999",
            "DD": "180.0 190.0 200.0 -999",
            "Neff": "50.0 60.0 70.0 -999",
            "PPPP": "101300.0 101310.0 101320.0 -999",
            "ww": "1 3 61 -999",
            "RR1c": "0.0 0.2 1.5 -999",
        },
        "01048": {
            "TTT": "285.15 286.15 -999 -999",
        },
    }
)

SAMPLE_KMZ = build_kmz(SAMPLE_KML)
