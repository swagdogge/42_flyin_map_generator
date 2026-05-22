# **************************************************************************** #
#                                                                              #
#                                                         :::      ::::::::    #
#    generator.py                                       :+:      :+:    :+:    #
#                                                     +:+ +:+         +:+      #
#    By: razimmer <razimmer@student.42heilbronn.de> +#+  +:+       +#+         #
#                                                 +#+#+#+#+#+   +#+            #
#    Created: 2026/05/22 21:34:01 by razimmer          #+#    #+#              #
#    Updated: 2026/05/22 21:34:05 by razimmer         ###   ########.fr        #
#                                                                              #
# **************************************************************************** #

import re
import math
import osmnx as ox
import networkx as nx
from tqdm import tqdm
from shapely.geometry import LineString, Point

# ══════════════════════════════════════════════════════════════════════════════
#  FLIP OPTIONS — set True/False to mirror the map on each axis
# ══════════════════════════════════════════════════════════════════════════════
FLIP_X = False   # mirror left ↔ right
FLIP_Y = True    # mirror top ↔ bottom
# ══════════════════════════════════════════════════════════════════════════════



def sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/*?:"<>|]', '', name)
    return name.strip().replace(' ', '_')


def sanitize_hub_name(name: str) -> str:
    name = re.sub(r'[^A-Za-z0-9_]', '_', name)
    return re.sub(r'_+', '_', name).strip('_')


def get_street_name(node_id, graph) -> str | None:
    name_counts: dict[str, int] = {}
    for u, v, data in graph.edges(node_id, data=True):
        raw = data.get('name')
        if raw is None:
            continue
        for c in (raw if isinstance(raw, list) else [raw]):
            if c:
                name_counts[c] = name_counts.get(c, 0) + 1
    return max(name_counts, key=name_counts.get) if name_counts else None


def make_unique_name(base: str, used_names: set[str]) -> str:
    if base not in used_names:
        used_names.add(base)
        return base
    counter = 2
    while True:
        candidate = f"{base}_{counter}"
        if candidate not in used_names:
            used_names.add(candidate)
            return candidate
        counter += 1


def build_unique_hub_name(node_id, graph, used_names: set[str], prefix: str = '') -> str:
    street = get_street_name(node_id, graph)
    base = sanitize_hub_name(street) if street else str(node_id)
    return make_unique_name(f"{prefix}{base}" if prefix else base, used_names)


def apply_flips(raw_x: int, raw_y: int,
                x_min: int, x_max: int,
                y_min: int, y_max: int) -> tuple[int, int]:
    x = (x_max - (raw_x - x_min)) if FLIP_X else raw_x
    y = (y_max - (raw_y - y_min)) if FLIP_Y else raw_y
    return x, y



# Reference speed used to normalise spacing (km/h).
# A road at this speed gets exactly the user-supplied base spacing.
REFERENCE_SPEED_KMH = 50.0

# Capacity-tier fallback speeds (km/h) when OSM maxspeed tag is absent.
FALLBACK_SPEED: dict[int, float] = {
    10: 120.0,   # motorway / trunk / primary
     5:  70.0,   # secondary
     2:  30.0,   # residential / unclassified / etc.
}


def speed_to_spacing(speed_kmh: float, base_spacing: float) -> float:
    """
    Scale base_spacing linearly with speed relative to REFERENCE_SPEED_KMH.
    A 120 km/h road with base 500 m and reference 50 km/h → 500 * (120/50) = 1200 m.
    A  30 km/h road with base 500 m and reference 50 km/h → 500 * ( 30/50) =  300 m.
    """
    return base_spacing * (speed_kmh / REFERENCE_SPEED_KMH)


def parse_maxspeed(raw) -> float | None:
    """Parse OSM maxspeed tag to a float km/h value, or return None."""
    if raw is None:
        return None
    s = str(raw).strip().lower()
    # Handle lists (sometimes OSM gives multiple values)
    if isinstance(raw, list):
        s = str(raw[0]).strip().lower()
    # Drop non-numeric suffixes like "mph", "km/h", "kph"
    s = s.replace('km/h', '').replace('kph', '').replace(' ', '')
    if s.endswith('mph'):
        try:
            return float(s[:-3]) * 1.60934
        except ValueError:
            return None
    try:
        return float(s)
    except ValueError:
        return None

def oriented_geometry(geom: LineString, u_pos: tuple, v_pos: tuple) -> LineString:
    """
    Return the LineString oriented so that its start point is closest to u_pos.
    If the end of the line is closer to u than the start is, reverse it.
    """
    start = geom.coords[0]
    end   = geom.coords[-1]

    def dist2(a, b):
        return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2

    # If the geometry's start is closer to v than to u, it's backwards — reverse it
    if dist2(start, u_pos) > dist2(end, u_pos):
        return LineString(list(geom.coords)[::-1])
    return geom


def interpolate_via_hubs(
    geom: LineString,           # already oriented u→v
    u_name: str,
    v_name: str,
    cap: int,
    street_base: str,
    via_counter: dict[str, int],
    used_names: set[str],
    max_spacing: float,
    x_min: int, x_max: int,
    y_min: int, y_max: int,
) -> tuple[list[str], list[str]]:
    """
    Split a long edge into segments of at most max_spacing metres by inserting
    intermediate via-hubs along the actual road geometry (oriented u→v).
    Returns (hub_lines, conn_lines). The direct u→v connection is NOT included;
    the chain of smaller connections replaces it entirely.
    """
    total_length = geom.length
    n_segments = math.ceil(total_length / max_spacing)

    hub_lines: list[str] = []
    conn_lines: list[str] = []
    prev_name = u_name

    for i in range(1, n_segments):
        dist = total_length * i / n_segments
        pt = geom.interpolate(dist)

        via_counter[street_base] = via_counter.get(street_base, 0) + 1
        via_base = f"{street_base}_via_{via_counter[street_base]}"
        via_name = make_unique_name(via_base, used_names)

        gx = int(pt.x / 10)
        gy = int(pt.y / 10)
        x, y = apply_flips(gx, gy, x_min, x_max, y_min, y_max)

        hub_lines.append(
            f"hub: {via_name} {x} {y} [zone=normal color=blue max_drones={cap}]"
        )
        conn_lines.append(
            f"connection: {prev_name}-{via_name} [max_link_capacity={cap}]"
        )
        prev_name = via_name

    # Final segment connects last via-hub to the real end node
    conn_lines.append(f"connection: {prev_name}-{v_name} [max_link_capacity={cap}]")
    return hub_lines, conn_lines


def generate_drone_config(
    location_name: str,
    nb_drones: int = 5,
    max_edge_spacing: float = 500.0,
) -> str:
    print(ox.settings.cache_folder)
    # 1. Download, simplify, and project street network
    #
    #    simplify_graph()         – merges redundant intermediate nodes on straight
    #                               stretches and closes minor topological gaps,
    #                               preserving full edge geometry via the 'geometry'
    #                               attribute so our via-hub interpolation still works.
    #    project_graph()          – reproject to a local UTM CRS (metres).
    #    consolidate_intersections() – collapses clusters of nearby intersection nodes
    #                               (e.g. dual-carriageway junction boxes) into a single
    #                               node, reducing clutter at complex junctions.
    #                               tolerance is in metres (projected CRS).
    print("  Downloading from OpenStreetMap...")
    # simplify=True (default) already merges redundant intermediate nodes
    # during download, so no separate simplify_graph() call is needed.
    G_raw = ox.graph_from_place(location_name, network_type='drive', simplify=True)

    print("  Projecting to UTM...")
    # consolidate_intersections() is intentionally omitted — it drops edges
    # when merging nearby nodes, causing missing road segments especially on
    # higher-capacity roads in dense areas. Plain projection is sufficient.
    G_proj = ox.project_graph(G_raw)

    nodes, edges = ox.graph_to_gdfs(G_proj)

    node_ids = list(G_proj.nodes())
    start_node = node_ids[0]
    end_node   = node_ids[-1]

    # 2. Compute coordinate bounds for optional flipping
    all_raw_x = [int(d['x'] / 10) for _, d in nodes.iterrows()]
    all_raw_y = [int(d['y'] / 10) for _, d in nodes.iterrows()]
    x_min, x_max = min(all_raw_x), max(all_raw_x)
    y_min, y_max = min(all_raw_y), max(all_raw_y)

    # Fast node-position lookup in projected metres (for geometry fallback + orientation)
    node_pos: dict = {
        nid: (float(G_proj.nodes[nid]['x']), float(G_proj.nodes[nid]['y']))
        for nid in G_proj.nodes()
    }

    # 3. Build unique human-readable names for every OSM node
    used_names: set[str] = set()
    node_name_map: dict = {}

    print(f"\nBuilding hub names for {len(node_ids):,} nodes...")
    for node_id in tqdm(node_ids, unit="node", dynamic_ncols=True):
        if node_id == start_node:
            node_name_map[node_id] = build_unique_hub_name(
                node_id, G_proj, used_names, prefix='start_')
        elif node_id == end_node:
            node_name_map[node_id] = build_unique_hub_name(
                node_id, G_proj, used_names, prefix='goal_')
        else:
            node_name_map[node_id] = build_unique_hub_name(
                node_id, G_proj, used_names)

    # 4. Process edges: capacity, deduplication, geometry + direction storage.
    #
    #    The frozenset key loses u/v order, so we store the OSM node IDs
    #    alongside the geometry. When we later interpolate we can re-orient the
    #    LineString so it always runs from its u-end to its v-end.
    #
    #    For edges with no stored geometry we synthesise a straight LineString
    #    from the node coordinates (already correctly oriented u→v).
    node_capacities: dict = {nid: 1 for nid in G_proj.nodes()}
    # best_edge: frozenset({u_name, v_name}) -> (cap, geom, u_node_id, v_node_id, speed_kmh)
    best_edge: dict = {}

    all_edges = list(G_proj.edges(keys=True, data=True))
    print(f"\nProcessing {len(all_edges):,} edges...")
    for u, v, k, data in tqdm(all_edges, unit="edge", dynamic_ncols=True):
        highway_type = data.get('highway', 'road')
        if any(h in str(highway_type) for h in ['motorway', 'trunk', 'primary']):
            cap = 10
        elif 'secondary' in str(highway_type):
            cap = 5
        else:
            cap = 2

        node_capacities[u] = max(node_capacities[u], cap)
        node_capacities[v] = max(node_capacities[v], cap)

        u_name = node_name_map.get(u)
        v_name = node_name_map.get(v)
        if u_name is None or v_name is None:
            continue

        key = frozenset({u_name, v_name})
        if len(key) < 2:
            continue

        geom = data.get('geometry')
        if not isinstance(geom, LineString):
            ux, uy = node_pos[u]
            vx, vy = node_pos[v]
            geom = LineString([(ux, uy), (vx, vy)])

        # Parse maxspeed; fall back to capacity-tier default if absent
        raw_speed = data.get('maxspeed')
        speed_kmh = parse_maxspeed(raw_speed)
        if speed_kmh is None:
            speed_kmh = FALLBACK_SPEED.get(cap, REFERENCE_SPEED_KMH)

        if key not in best_edge or cap > best_edge[key][0]:
            best_edge[key] = (cap, geom, u, v, speed_kmh)

    # 5. Generate hub lines for OSM nodes
    valid_hub_names: set[str] = set(node_name_map.values())
    osm_hub_lines: list[str] = []

    print(f"\nWriting {len(nodes):,} OSM hubs...")
    for node_id, data in tqdm(nodes.iterrows(), total=len(nodes),
                               unit="hub", dynamic_ncols=True):
        raw_x = int(data['x'] / 10)
        raw_y = int(data['y'] / 10)
        x, y = apply_flips(raw_x, raw_y, x_min, x_max, y_min, y_max)

        hub_cap  = node_capacities[node_id]
        hub_name = node_name_map[node_id]

        if node_id == start_node:
            osm_hub_lines.append(
                f"start_hub: {hub_name} {x} {y} [color=green max_drones={hub_cap}]")
        elif node_id == end_node:
            osm_hub_lines.append(
                f"end_hub: {hub_name} {x} {y} [color=yellow max_drones={hub_cap}]")
        else:
            osm_hub_lines.append(
                f"hub: {hub_name} {x} {y} [zone=normal color=blue max_drones={hub_cap}]")

    # 6. Build connections, inserting via-hubs wherever the edge exceeds max_spacing
    via_hub_lines:   list[str] = []
    connection_list: list[str] = []
    via_counter:     dict[str, int] = {}

    print(f"\nBuilding connections and via-hubs for {len(best_edge):,} unique edges...")
    for key, (cap, geom, u_node, v_node, speed_kmh) in tqdm(best_edge.items(),
                                                   unit="edge", dynamic_ncols=True):
        names  = list(key)
        a_name, b_name = names[0], names[1]

        if a_name not in valid_hub_names or b_name not in valid_hub_names:
            connection_list.append(
                f"# REMOVED (unknown hub): connection: {a_name}-{b_name}"
                f" [max_link_capacity={cap}]")
            continue

        street_base = a_name.removeprefix('start_').removeprefix('goal_')

        edge_spacing = speed_to_spacing(speed_kmh, max_edge_spacing)

        if geom.length > edge_spacing:
            # Determine which hub name corresponds to u_node and which to v_node,
            # then orient the geometry so it runs from a_node → b_node.
            u_name_stored = node_name_map.get(u_node)
            v_name_stored = node_name_map.get(v_node)

            # a_name is whichever name came first from the frozenset iteration —
            # figure out whether that matches u or v so we can orient correctly.
            if a_name == u_name_stored:
                chain_start, chain_end = a_name, b_name
                geom_oriented = oriented_geometry(geom, node_pos[u_node], node_pos[v_node])
            else:
                chain_start, chain_end = a_name, b_name
                geom_oriented = oriented_geometry(geom, node_pos[v_node], node_pos[u_node])

            v_hubs, v_conns = interpolate_via_hubs(
                geom_oriented, chain_start, chain_end, cap,
                street_base, via_counter, used_names,
                edge_spacing, x_min, x_max, y_min, y_max,
            )
            via_hub_lines.extend(v_hubs)
            connection_list.extend(v_conns)
        else:
            connection_list.append(
                f"connection: {a_name}-{b_name} [max_link_capacity={cap}]")

    # 7. Assemble final output
    lines = [f"nb_drones: {nb_drones}", ""]
    lines.append("# ── HUBS ──")
    lines.extend(osm_hub_lines)

    if via_hub_lines:
        lines.append("")
        lines.append("# ── VIA-HUBS (interpolated along long edges) ──")
        lines.extend(via_hub_lines)

    lines.append("")
    lines.append("# ── CONNECTIONS ──")
    lines.extend(connection_list)

    return "\n".join(lines)


if __name__ == "__main__":
    location_input = input(
    "Enter one or more regions/places (comma separated): "
    ).strip()

    location_name = [x.strip() for x in location_input.split(",") if x.strip()]

    # Falls nur ein Eintrag -> String statt Liste
    if len(location_name) == 1:
        location_name = location_name[0]

    nb_drones_raw = input("Enter the number of drones (default 5): ").strip()
    nb_drones = int(nb_drones_raw) if nb_drones_raw.isdigit() else 5

    spacing_raw = input("Max spacing between hubs in metres (default 500): ").strip()
    try:
        max_spacing = float(spacing_raw) if spacing_raw else 500.0
    except ValueError:
        max_spacing = 500.0

    print(f"\nFlip settings: FLIP_X={FLIP_X}, FLIP_Y={FLIP_Y}")
    print(f"\nPreparing map for '{location_name}'...")
    config_text = generate_drone_config(
        location_name, nb_drones=nb_drones, max_edge_spacing=max_spacing)

    if isinstance(location_name, list):
        filename_base = "_".join(location_name)
    else:
        filename_base = location_name

    filename = sanitize_filename(filename_base) + ".txt"
    with open(filename, "w", encoding="utf-8") as f:
        f.write(config_text)

    print(f"\nDone! File saved as: {filename}")