# FABRIC topology research (public sources only), 2026-09-29

## Primary source

The key finding is that FABRIC's orchestrator publishes its level-1 resource advertisement (sites, trunk links and capacities) **without authentication**:

```
curl 'https://orchestrator.fabric-testbed.net/portalresources?graph_format=JSON_NODELINK&level=1'
```

The FABRIC portal's Resources page proxies this API (`${ORCHESTRATOR_API_URL}/portalresources/summary` in
`app/api/resources/route.js`, https://github.com/fabric-testbed/fabric-portal).

- The response is a NetworkX node-link graph of about 1126 nodes and 1203 edges.
- `CompositeNode` entries are sites. Their `Location` gives the street address and `MaintenanceInfo` gives the state.
- `Link` entries of type `L1Path`/`L2Path` join `TrunkPort` connection points on per-site `*-data-sw` switches.
- `Capacities.bw` on the ports is the port speed in Gbps. `Capacities.bw` on the link is the reservable amount, which is 80% of the port speed.
- The raw snapshot is saved next to this note as `portalresources.json`. `links-table.txt` holds the extracted trunk-link table.

Other sources:
- The TeraCore announcement (2023-10-23) says "a ring spanning the continental U.S. ... 1.2 Terabits per second". The ring was built on ESnet6 fiber spectrum, with "optical equipment from Ciena and Infinera and networking equipment from Cisco".
  - https://renci.org/news/nsf-fabric-project-announces-groundbreaking-high-speed-network-infrastructure-expansion/
  - https://www.sdsc.edu/news/2023/PR20231023_FABRIC.html
- Sarpkaya et al., "Evaluation of TCP Congestion Control for Public High-Performance Wide-Area Networks", IEEE HPSR 2026, https://arxiv.org/abs/2603.12660.
  - It confirms "the 80 Gb/s reservation limit on the 100 Gb/s links of FABRIC".
  - It uses a STAR hub slice with NCSA, MICH, KANS and INDI.
  - It notes "shallow buffers of the switches" on the path. This is a qualitative statement only, with no size given.
- "FABRIC Testbed from the Eyes of a Network Researcher" (WTestbeds 2023) covers fablib usage only. It has no topology or capacity data.

## Sites (all 38 advertised, snapshot 2026-09-29)

All are `Active` except CAPE, which is in `Maint`.

| Site | Location | | Site | Location |
|---|---|---|---|---|
| LOSA | Los Angeles, CA | | NCSA | Champaign, IL |
| SALT | Salt Lake City, UT | | EDC | Champaign, IL |
| STAR | Chicago, IL (StarLight) | | INDI | Indianapolis, IN |
| NEWY | New York, NY | | EDUKY | Lexington, KY |
| WASH | McLean, VA (1755 Old Meadow Rd) | | PSC | Monroeville, PA |
| ATLA | Atlanta, GA | | MAX | College Park, MD |
| DALL | Dallas, TX | | CLEM | Anderson, SC |
| KANS | Kansas City, MO | | GATECH | Atlanta, GA |
| SEAT | Seattle, WA | | FIU | Miami, FL |
| UCSD | San Diego, CA | | RUTG | Piscataway, NJ |
| UTAH | Salt Lake City, UT | | PRIN | Plainsboro, NJ |
| SRI | Menlo Park, CA | | MASS | Holyoke, MA |
| TACC | Austin, TX | | HAWI | Honolulu, HI |
| GPN | Kansas City, MO | | MICH | Ann Arbor, MI |
| CERN | Meyrin, CH/FR | | AMST | Amsterdam, NL |
| TOKY | Tokyo, JP | | CIEN / CAPE | Ottawa, CA |

The advertisement also lists cloud and peering nodes: AL2S, AWS, GCP, AZURE and OCI.

## Inter-site trunk links (advertised)

Port and reservable values are in Gbps.

**TeraCore ring.** Every segment is a Bundle-Ether (LAG), port 1200, reservable 960:
`LOSA-SALT`, `SALT-STAR`, `STAR-NEWY`, `NEWY-WASH`, `WASH-ATLA`, `ATLA-DALL`, `DALL-LOSA`.
This is a 7-node ring, and it matches the "1.2 Tbps" nameplate.

**100G L1 links.** Port 100, reservable 80:
- STAR-WASH (chord)
- KANS-STAR, KANS-SALT, KANS-DALL
- GPN-KANS
- LOSA-SEAT, LOSA-UCSD
- SALT-UTAH
- MICH-STAR, NCSA-STAR, INDI-STAR
- ATLA-FIU
- NEWY-RUTG, NEWY-PRIN

**400G L1 links** with no reservable value advertised: CIEN-STAR and CAPE-STAR.

**L2Path (VLAN-stitched) links** with their reservable values:
- MASS-NEWY: port 400, reservable 80
- CLEM-WASH: 80
- MAX-WASH: 32
- PSC-WASH: 32
- HAWI-LOSA: 32
- HAWI-SEAT: 32
- EDUKY-STAR: 8
- TACC-STAR: 8
- GATECH-WASH: 10G port, reservable 8
- SRI-LOSA: 10G port, reservable 8
- SEAT-TOKY: 80
- CERN-NEWY: 400G ports, reservable 320 plus 10
- CERN-WASH: reservable 100 plus 10
- AMST-CERN: 80
- AL2S-WASH: 100

**What is nameplate and what is inferred.**
- The port speeds and reservable values are verbatim from the advertisement.
- The 80% reservable rule is FABRIC policy. Both the advertisement and the HPSR paper show it.
- Three things are inferred:
  - That the 1200 bundles are 3x400G. This is based on the Bundle-Ether naming and the 400G ports elsewhere.
  - That the advertised links are the full physical set.
  - That no hidden optical-layer protection exists.

## Chosen subset: LOSA, SALT, STAR, NEWY, MICH

```
 LOSA ==1200== SALT ==1200== STAR ==1200== NEWY
                               |
                              100   <-- shared bottleneck
                               |
                              MICH
 (== TeraCore LAG, 960 reservable; | 100G spoke, 80 reservable)
```

**Multi-hop.** LOSA to MICH is 3 hops (LOSA, SALT, STAR, MICH). LOSA to NEWY is also 3 hops.

**Shared bottleneck.**
- LOSA, SALT and NEWY each send to MICH. All three flows must cross STAR to MICH.
- MICH's only advertised trunk is the 100G link to STAR, so this bottleneck is real, not an artifact of the subset.
- With 2 x 100G terminals per site, three source sites offer up to 600G into a 100G link.
- A second, uncongested sharing case: LOSA and SALT sending to NEWY share SALT to STAR and STAR to NEWY, which are 1200G each.

**Why these sites.**
- The four core nodes are all TeraCore ring nodes. They are large, long-lived sites, and STAR is the biggest (768 cores).
- MICH is an established site (the HPSR paper used it).
- The subset spans west to east across the country.
- The induced subgraph is a tree, so routing is unambiguous. Every real advertised link between the chosen sites is included; there are no others.

**Abstraction caveat.**
- The real ring gives a second LOSA to NEWY path: LOSA, DALL, ATLA, WASH, NEWY.
- There are also STAR-WASH and KANS chords, which are omitted.
- FABRIC's MPLS path selection for L2 and FABNet services is not publicly documented. The model therefore assumes shortest-path routing within the subset.
- Once we have a slice, pin the actual path with fablib's explicit route (ERO) option, or measure it.

**Alternative, if more contention on shared core links is wanted:**
- Add NCSA, which is another 100G STAR spoke, as a 6th site.
- Or swap the destination to INDI.

## Switch hardware and buffers

- Public material says only that TeraCore uses "networking equipment from Cisco" (RENCI/SDSC press release).
- The advertisement shows IOS-XR interface names (`HundredGigE0/0/0/x`, `FourHundredGigE0/0/0/x`, `Bundle-Ether`). That is consistent with Cisco NCS 5500/5700-class routers.
- **I could not find a public source naming the exact model**, so none is asserted.
- Buffer sizes for the FABRIC data switches are **not publicly documented**. The HPSR 2026 paper describes them only qualitatively, as "shallow buffers".
- The JSON's `switch_buffer: 2 Gb` is a modeling default, not a FABRIC number.
- Sites STAR and MICH also advertise separate P4 switches (`star-p4-sw`, `mich-p4-sw`). These are user-programmable resources, not the transit dataplane.

## To re-check with portal access (fablib)

1. Run `fablib.list_sites()` and `fablib.list_links()`, and compare them to this snapshot. The sites and links may change.
2. Confirm the LAG composition behind the 1200G links (3x400G?), and what that means for per-flow limits.
3. Find the actual routed path for L2PTP/L2STS and FABNetv4 between the chosen sites. In particular, check whether LOSA to NEWY ever uses the southern ring.
4. Measure RTTs between the subset sites. None are given here, because nothing public confirms them for these pairs.
5. Obtain the data-switch model and per-port buffer (FABRIC ops or knowledge base).
6. Check current worker and SmartNIC availability at MICH and NEWY. At snapshot time NEWY had 2 workers and 144/256 cores allocated.
7. Check whether a background-traffic or utilization baseline exists (infrastructure-metrics.fabric-testbed.net).
