#!/usr/bin/env python3
"""Generate a broad-area summit compass map with named peaks and bearings.

Uses the open 20 m MOI DTM for terrain contours, OSM peak names, and NOAA WMM
declination so the map distinguishes true and magnetic bearings.
"""

from __future__ import annotations

import argparse
from datetime import date
from html import escape
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
import xml.etree.ElementTree as ET

from generate_contour_maps import (
    DTM_INDEX_PAGE,
    DTM_LICENSE,
    DTM_SPACING_M,
    ROOT,
    contour_segments,
    extract_dtm_grid,
    get_dtm_archive,
    make_wms_bbox,
    projected_xy,
    wgs84_to_twd97,
)


NOAA_DECLINATION_URL = "https://www.ngdc.noaa.gov/geomag-web/calculators/calculateDeclination"
NOAA_MODEL_URL = "https://www.ncei.noaa.gov/products/world-magnetic-model"


def fetch_declination(point: tuple[float, float], when: date, refresh: bool = False):
    lat, lon = point
    cache = ROOT / ".cache" / "geomag" / f"wmm-{lat:.5f}-{lon:.5f}-{when.isoformat()}.json"
    if cache.exists() and not refresh:
        data = json.loads(cache.read_text(encoding="utf-8"))
    else:
        params = (
            f"lat1={lat:.6f}&lon1={lon:.6f}&startYear={when.year}"
            f"&startMonth={when.month}&startDay={when.day}&model=WMM&resultFormat=json&key=zNEw7"
        )
        result = subprocess.run(
            ["curl", "--fail", "--silent", "--show-error", "--location", "--max-time", "30",
             f"{NOAA_DECLINATION_URL}?{params}"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        data = json.loads(result.stdout)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(data), encoding="utf-8")
    result = data["result"][0]
    return float(result["declination"]), data.get("model", "WMM"), float(result.get("declination_uncertainty", 0))


def true_bearing_and_distance(origin, target):
    lat1, lon1 = map(math.radians, origin)
    lat2, lon2 = map(math.radians, target)
    dlon = lon2 - lon1
    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    bearing = (math.degrees(math.atan2(y, x)) + 360) % 360
    dlat = lat2 - lat1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    distance = 6371008.8 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return bearing, distance


def _fetch_osm_peaks_bbox(bbox, refresh=False, depth=0):
    south, west, north, east = bbox[1], bbox[0], bbox[3], bbox[2]
    query = f'[out:json][timeout:40];(node["natural"="peak"]["name"]({south},{west},{north},{east});way["natural"="peak"]["name"]({south},{west},{north},{east}););out center tags;'
    key = hashlib.sha256(",".join(f"{v:.6f}" for v in bbox).encode()).hexdigest()[:16]
    cache = ROOT / ".cache" / "osm" / "peaks" / f"{key}.json"
    if cache.exists() and not refresh:
        data = json.loads(cache.read_text(encoding="utf-8"))
    else:
        result = subprocess.run(
            ["curl", "--fail", "--silent", "--show-error", "--location", "--max-time", "90",
             "--user-agent", "weekly-hiking-map-generator/1.0 (https://github.com/ap9035/family_hiking_route)",
             "--referer", "https://www.openstreetmap.org/", "--header", "Accept: application/json,text/plain,*/*",
             "--data-urlencode", f"data={query}", "https://overpass-api.de/api/interpreter"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if result.returncode:
            # Large metropolitan Overpass extracts can time out; split the query area and retry.
            if depth < 4 and max(east-west, north-south) > 0.035:
                mid_lon, mid_lat = (west+east)/2, (south+north)/2
                peaks=[]
                for sub_bbox in ([west,south,mid_lon,mid_lat], [mid_lon,south,east,mid_lat],
                                 [west,mid_lat,mid_lon,north], [mid_lon,mid_lat,east,north]):
                    peaks.extend(_fetch_osm_peaks_bbox(sub_bbox, refresh, depth+1))
                return peaks
            detail = result.stderr.decode("utf-8", errors="replace")
            raise RuntimeError(detail or f"curl exited with status {result.returncode}")
        data = json.loads(result.stdout)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(data), encoding="utf-8")
    peaks = []
    for feature in data.get("elements", []):
        tags = feature.get("tags", {})
        point = (feature.get("lat"), feature.get("lon")) if feature.get("type") == "node" else (
            feature.get("center", {}).get("lat"), feature.get("center", {}).get("lon")
        )
        if not tags.get("name") or None in point:
            continue
        try:
            elevation = float(tags.get("ele", ""))
        except ValueError:
            elevation = None
        peaks.append({"id": feature.get("id"), "name": tags["name"], "point": tuple(map(float, point)), "elevation": elevation})
    return peaks


def fetch_osm_peaks(bbox, refresh=False):
    unique={}
    for peak in _fetch_osm_peaks_bbox(bbox, refresh):
        key=peak.get("id") or (peak["name"],peak["point"])
        unique[key]=peak
    return list(unique.values())


def map_osm_bbox(center, bbox):
    min_e, min_n, max_e, max_n = bbox
    lat, lon = center
    # Convert projected map half-width/height to a conservative WGS84 query box.
    lat_delta = (max_n - min_n) / (2 * 110_900) + 0.002
    lon_delta = (max_e - min_e) / (2 * 111_000 * math.cos(math.radians(lat))) + 0.002
    if lon_delta * 2 > 0.25 or lat_delta * 2 > 0.25:
        raise SystemExit("Compass map extent exceeds the OSM map API limit (0.25 degrees per side). Choose a larger scale denominator.")
    return [lon - lon_delta, lat - lat_delta, lon + lon_delta, lat + lat_delta]


def bearing_ray(observer_xy, bearing_deg, frame):
    ox, oy = observer_xy
    x, y, width, height = frame
    angle = math.radians(bearing_deg)
    dx, dy = math.sin(angle), -math.cos(angle)
    candidates = []
    if dx > 0:
        candidates.append((x + width - ox) / dx)
    elif dx < 0:
        candidates.append((x - ox) / dx)
    if dy > 0:
        candidates.append((y + height - oy) / dy)
    elif dy < 0:
        candidates.append((y - oy) / dy)
    positive = [t for t in candidates if t > 0]
    t = min(positive)
    return ox + t * dx, oy + t * dy


def create_svg(config, compass, bbox, contours, peaks, declination, model, uncertainty, data_date):
    page_w, page_h = 2970, 2100
    p = compass
    frame = (
        100,
        int(float(p.get("top_margin_mm", 21)) * 10),
        int(float(p.get("map_width_mm", 277)) * 10),
        int(float(p.get("map_height_mm", 143)) * 10),
    )
    observer = tuple(map(float, p["observer"]))
    observer_label = escape(p.get("observer_label", "觀測點"))
    scale = int(p.get("scale_denominator", 50000))
    interval = int(p.get("contour_interval_m", 20))
    grid_spacing = int(p.get("grid_spacing_m", 1000))
    min_e, min_n, max_e, max_n = bbox
    x, y, w, h = frame

    contour_groups = {}
    for level, start, end in contours:
        x1, y1 = projected_xy(start, bbox, frame)
        x2, y2 = projected_xy(end, bbox, frame)
        contour_groups.setdefault(level, []).append(f"M{x1:.2f},{y1:.2f}L{x2:.2f},{y2:.2f}")
    contour_svg = []
    major_interval = int(p.get("index_contour_interval_m", 100))
    for level, paths in sorted(contour_groups.items()):
        cls = "index-contour" if level % major_interval == 0 else "contour"
        contour_svg.append(f'<path class="{cls}" d="{"".join(paths)}"/>')

    ox, oy = projected_xy(wgs84_to_twd97(observer), bbox, frame)
    bearing_lines = []
    for bearing in range(0, 360, 30):
        ex, ey = bearing_ray((ox, oy), bearing, frame)
        bearing_lines.append(f'<line class="azimuth" x1="{ox:.1f}" y1="{oy:.1f}" x2="{ex:.1f}" y2="{ey:.1f}"/>')
        label = f"{bearing:03d}°"
        lx, ly = bearing_ray((ox, oy), bearing, (x+24,y+24,w-48,h-48))
        bearing_lines.append(f'<text class="bearing-label" x="{lx:.1f}" y="{ly:.1f}" text-anchor="middle">{label}</text>')

    max_peaks = int(p.get("max_peaks", 12))
    min_distance = float(p.get("min_peak_distance_km", 2.5)) * 1000
    visible=[]
    for peak in peaks:
        bearing, distance = true_bearing_and_distance(observer, peak["point"])
        east,north=wgs84_to_twd97(peak["point"])
        if not (min_e <= east <= max_e and min_n <= north <= max_n) or distance < min_distance:
            continue
        magnetic = (bearing - declination) % 360
        visible.append((distance, peak, bearing, magnetic))
    # Keep the nearest well-named summit in each 30-degree sector to reduce label clutter.
    visible.sort(key=lambda item: item[0])
    sector_peaks={}
    for item in visible:
        sector=round(item[2]/30) % 12
        sector_peaks.setdefault(sector,item)
    visible=sorted(sector_peaks.values(), key=lambda item: item[2])[:max_peaks]
    peak_svg=[]
    peak_legend=[]
    for index,(distance,peak,true_az,magnetic_az) in enumerate(visible,1):
        px,py=projected_xy(wgs84_to_twd97(peak["point"]),bbox,frame)
        peak_svg.append(f'<circle class="peak" cx="{px:.1f}" cy="{py:.1f}" r="14"/><text class="peak-number" x="{px:.1f}" y="{py+7:.1f}" text-anchor="middle">{index:02d}</text>')
        col_x = 100 if index % 2 else 1510
        row_y = 1710 + ((index-1)//2)*40
        elev_text = f"{peak['elevation']:.0f} m" if peak["elevation"] is not None else ""
        legend_text = f"{index:02d} {escape(peak['name'])} {elev_text}｜真 {true_az:03.0f}°／磁 {magnetic_az:03.0f}°｜{distance/1000:.1f} km"
        peak_legend.append(f'<text x="{col_x}" y="{row_y}" class="footer">{legend_text}</text>')

    grid=[]
    for e in range(math.ceil(min_e/grid_spacing)*grid_spacing, int(max_e)+1, grid_spacing):
        gx=x+(e-min_e)/(max_e-min_e)*w
        grid.append(f'<line class="grid" x1="{gx:.1f}" y1="{y}" x2="{gx:.1f}" y2="{y+h}"/>')
        grid.append(f'<text class="grid-label" x="{gx:.1f}" y="{y-10}" text-anchor="middle">E {e:.0f}</text>')
    for n in range(math.ceil(min_n/grid_spacing)*grid_spacing, int(max_n)+1, grid_spacing):
        gy=y+(max_n-n)/(max_n-min_n)*h
        grid.append(f'<line class="grid" x1="{x}" y1="{gy:.1f}" x2="{x+w}" y2="{gy:.1f}"/>')
        grid.append(f'<text class="grid-label" x="{x+6}" y="{gy-6:.1f}">N {n:.0f}</text>')

    title = escape(p.get("title", f"{config['title']}｜山頂方位判讀圖"))
    subtitle = escape(f"A4 橫式｜1:{scale:,}｜等高距 {interval} m（20 m DTM）｜方位線每 30°｜峰位編號對照下方表列")
    decl_text = f"磁偏角 {declination:+.2f}°（東正西負）｜{model}｜不確定度 ±{uncertainty:.2f}°｜製圖 {data_date}"
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="297mm" height="210mm" viewBox="0 0 {page_w} {page_h}" role="img" aria-labelledby="title desc">
<title id="title">{title}</title><desc id="desc">Surrounding terrain map centered on {observer_label}, with named OSM peaks, true and magnetic bearings, and WMM declination.</desc>
<defs><clipPath id="mapclip"><rect x="{x}" y="{y}" width="{w}" height="{h}"/></clipPath>
<style>text{{font-family:sans-serif;fill:#202820}} .title{{font-size:62px;font-weight:700}} .subtitle{{font-size:27px;fill:#46564b}} .grid{{stroke:#61798055;stroke-width:2;stroke-dasharray:8 9}} .grid-label{{font-size:21px;fill:#42555a;paint-order:stroke;stroke:white;stroke-width:5}} .contour{{fill:none;stroke:#a1835d;stroke-width:1.7;opacity:.82}} .index-contour{{fill:none;stroke:#78552f;stroke-width:3.4;opacity:.9}} .azimuth{{stroke:#687a8b55;stroke-width:2;stroke-dasharray:10 11}} .bearing-label{{font-size:22px;font-weight:700;fill:#455c6a;paint-order:stroke;stroke:#fff;stroke-width:6}} .peak{{fill:#a33125;stroke:white;stroke-width:3}} .peak-number{{font-size:18px;font-weight:700;fill:white;text-anchor:middle}} .observer{{fill:#fff;stroke:#111;stroke-width:7}} .observer-label{{font-size:32px;font-weight:700;paint-order:stroke;stroke:white;stroke-width:9}} .frame{{fill:none;stroke:#35433b;stroke-width:3}} .footer{{font-size:21px}}</style></defs>
<rect width="2970" height="2100" fill="#fff"/><text id="title" x="100" y="103" class="title">{title}</text><text x="100" y="153" class="subtitle">{subtitle}</text>
<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="#f7f3e6"/>
<g clip-path="url(#mapclip)">{''.join(contour_svg)}{''.join(bearing_lines)}<g>{''.join(grid)}</g>
<circle class="observer" cx="{ox:.1f}" cy="{oy:.1f}" r="15"/><text class="observer-label" x="{ox+20:.1f}" y="{oy-18:.1f}">{observer_label}</text>{''.join(peak_svg)}</g>
<rect x="{x}" y="{y}" width="{w}" height="{h}" class="frame"/>
<g transform="translate({x+w-95} {y+115})"><text x="0" y="-32" class="observer-label" text-anchor="middle">N</text><path d="M0 0 L0 75" stroke="#202820" stroke-width="5"/></g>
{''.join(peak_legend)}
<text x="100" y="1980" class="footer">高程：<a href="{DTM_INDEX_PAGE}">內政部 20 m DTM</a>｜等高距 {interval} m｜<a href="{DTM_LICENSE}">政府資料開放授權第1版</a>｜峰名：© OpenStreetMap contributors（ODbL）</text>
<text x="100" y="2015" class="footer">表列磁方位已依磁偏角換算｜{escape(decl_text)}｜<a href="{NOAA_MODEL_URL}">NOAA WMM</a>｜遠方地形及能見度請以現場為準</text>
</svg>'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path, help="Destination osm-map.json with compass_map settings")
    parser.add_argument("--refresh", action="store_true", help="Refresh DTM, OSM, and magnetic-declination data")
    parser.add_argument("--scale", type=int, help="Map scale denominator; default 50000")
    args = parser.parse_args()
    config_path = args.config if args.config.is_absolute() else Path.cwd() / args.config
    config = json.loads(config_path.read_text(encoding="utf-8"))
    compass = config.get("compass_map")
    if not compass or "observer" not in compass or not ("dtm_regions" in compass or "dtm_region" in compass):
        raise SystemExit("Add compass_map.observer [latitude, longitude], observer_label, and dtm_regions to the config.")
    if args.scale:
        compass["scale_denominator"] = args.scale
    compass.setdefault("scale_denominator", 50000)
    compass.setdefault("map_width_mm", 277)
    compass.setdefault("map_height_mm", 143)
    compass.setdefault("top_margin_mm", 21)
    compass.setdefault("contour_interval_m", 20)
    p = {
        "center": compass["observer"],
        "scale_denominator": compass["scale_denominator"],
        "map_width_mm": compass["map_width_mm"],
        "map_height_mm": compass["map_height_mm"],
    }
    bbox, _, _, scale, _, _ = make_wms_bbox({"print_map": p, "bbox": config["bbox"]})
    regions = compass.get("dtm_regions", [compass.get("dtm_region")])
    regions = [region for region in regions if region]
    if not regions:
        raise SystemExit("Set compass_map.dtm_regions to the official DTM region(s) covering the whole view.")
    grid = {}
    for region in regions:
        archive, _ = get_dtm_archive(region, args.refresh)
        grid.update(extract_dtm_grid(archive, bbox))
    interval = int(compass["contour_interval_m"])
    contours = contour_segments(grid, interval)
    observer = tuple(map(float, compass["observer"]))
    osm_bbox = map_osm_bbox(observer, bbox)
    try:
        peaks = fetch_osm_peaks(osm_bbox, args.refresh)
    except Exception as exc:
        raise SystemExit(f"OpenStreetMap named-peak query failed: {exc}") from exc
    try:
        declination, model, uncertainty = fetch_declination(observer, date.today(), args.refresh)
    except Exception as exc:
        raise SystemExit(f"NOAA WMM magnetic-declination lookup failed; cannot create a compass-bearing map safely: {exc}") from exc
    svg = create_svg(config, compass, bbox, contours, peaks, declination, model, uncertainty, date.today().isoformat())
    prefix = compass.get("output_prefix", config_path.parent.name)
    svg_path = config_path.parent / f"{prefix}_山頂方位圖.svg"
    svg_path.write_text(svg, encoding="utf-8")
    ET.parse(svg_path)
    outputs = [svg_path]
    converter = shutil.which("rsvg-convert")
    if converter:
        pdf_path = config_path.parent / f"{prefix}_山頂方位圖.pdf"
        subprocess.run([converter, "-f", "pdf", "-o", str(pdf_path), str(svg_path)], check=True)
        outputs.append(pdf_path)
    for output in outputs:
        print(f"Generated {output} ({output.stat().st_size:,} bytes)")
    print(f"Map extent 1:{scale:,}; {len(peaks)} named OSM peaks in source data; magnetic declination {declination:+.2f}° ({model}).")


if __name__ == "__main__":
    main()
