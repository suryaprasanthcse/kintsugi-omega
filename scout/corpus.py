"""The frozen research corpus: a small fictional world of companies, sites and products.

Built deterministically from a fixed seed, so every run, every seed and every replay sees the
same documents (spec §5.9.2: no internet variance). Every document has three or four pages and
each fact sits on exactly one page, never the last, so answering means reading several pages.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from functools import cache

SEED = 731
CITIES = ("Port Ellery", "Marrow Bay", "Lindqvist", "Asterholm", "Cape Verran", "Nordhaven",
          "Saltmarsh", "Iver City", "Calder Springs", "Oakhurst")  # fmt: skip
REGIONS = ("Kerrin Basin", "Tamsin Valley", "Orrin Highlands", "Blackwater Delta",
           "Selwyn Plateau", "Mercer Coast", "Halden Fjord", "Quarry Downs")  # fmt: skip
FIRST = (
    "Ada", "Bram", "Cleo", "Dario", "Esme", "Femi", "Greta", "Hugo", "Ines", "Jonah",
    "Kaia", "Lior", "Mina", "Nils", "Orla", "Pavel", "Quinn", "Rosa", "Soren", "Tova",
)  # fmt: skip
LAST = ("Kowal", "Ferreira", "Lindahl", "Okafor", "Marchetti", "Haugen", "Tanaka", "Abara",
        "Novak", "Castell", "Imrie", "Varga")  # fmt: skip
PREFIX = ("Vale", "Kerr", "Ost", "Brin", "Tal", "Mor", "Quin", "Sel", "Ard", "Cor", "Hal",
          "Lun", "Pell", "Rav", "Thor", "Wex")  # fmt: skip
SUFFIX = ("mont", "ova", "wick", "haven", "ridge", "stead", "more", "field", "brook", "crest")
SECTORS = {
    "Minerals": ("tonnes of ore", "mining"),
    "Energy": ("megawatt-hours", "energy"),
    "Foods": ("tonnes of grain", "food processing"),
    "Optics": ("lens units", "optics"),
    "Textiles": ("metres of fabric", "textiles"),
    "Robotics": ("robot arms", "robotics"),
}
SITE_KINDS = {"Minerals": "Mine", "Energy": "Power Station", "Foods": "Mill", "Optics": "Works",
              "Textiles": "Weaving Hall", "Robotics": "Assembly Plant"}  # fmt: skip
PRODUCT_WORDS = ("Aurora", "Lumen", "Kestrel", "Nimbus", "Tessera", "Vector", "Halcyon", "Orbit",
                 "Sable", "Juniper", "Cobalt", "Meridian")  # fmt: skip


@dataclass(frozen=True)
class Company:
    name: str
    sector: str
    founded: int
    hq: str
    ceo: str
    ceo_since: int
    staff: int


@dataclass(frozen=True)
class Site:
    name: str
    operator: str
    region: str
    built: int
    capacity: int
    unit: str


@dataclass(frozen=True)
class Product:
    name: str
    maker: str
    launched: int
    category: str


@dataclass(frozen=True)
class Doc:
    id: str
    title: str
    pages: tuple[str, ...]


@dataclass(frozen=True)
class World:
    companies: tuple[Company, ...]
    sites: tuple[Site, ...]
    products: tuple[Product, ...]
    docs: dict[str, Doc]

    def doc_of(self, name: str) -> str:
        return next(doc.id for doc in self.docs.values() if doc.title.startswith(name + " -"))


@cache
def world() -> World:
    rng = random.Random(SEED)
    names = [p + s for p in PREFIX for s in SUFFIX]
    rng.shuffle(names)
    companies, sites, products = [], [], []
    for i in range(18):
        sector = list(SECTORS)[i % len(SECTORS)]
        founded = rng.randrange(1952, 2016)
        companies.append(Company(f"{names[i]} {sector}", sector, founded, rng.choice(CITIES),
                                 f"{rng.choice(FIRST)} {rng.choice(LAST)}",
                                 rng.randrange(max(founded + 3, 2005), 2026),
                                 rng.randrange(40, 9000)))  # fmt: skip
    for i, company in enumerate(companies):
        for j in range(1 + i % 2):
            site = f"{names[18 + 2 * i + j]} {SITE_KINDS[company.sector]}"
            sites.append(Site(site, company.name, rng.choice(REGIONS), rng.randrange(1960, 2024),
                              rng.randrange(120, 98_000), SECTORS[company.sector][0]))  # fmt: skip
        word = PRODUCT_WORDS[i % len(PRODUCT_WORDS)]
        products.append(Product(f"{word} {100 + 7 * i}", company.name, rng.randrange(2008, 2026),
                                SECTORS[company.sector][1]))  # fmt: skip
    docs: dict[str, Doc] = {}
    for n, c in enumerate(companies):
        operated = ", ".join(s.name for s in sites if s.operator == c.name)
        docs[f"doc-c{n:02d}"] = Doc(f"doc-c{n:02d}", f"{c.name} - company profile", (
            f"{c.name} is a {SECTORS[c.sector][1]} company headquartered in {c.hq}. "
            f"It employs around {c.staff} people.",
            f"{c.name} was founded in {c.founded}. Its chief executive is {c.ceo}, "
            f"who took the role in {c.ceo_since}.",
            f"{c.name} operates the following sites: {operated}.",
            f"Recent coverage of {c.name} focused on hiring and supplier contracts; the company "
            "publishes an annual sustainability statement.",
        ))  # fmt: skip
    for n, s in enumerate(sites):
        docs[f"doc-s{n:02d}"] = Doc(f"doc-s{n:02d}", f"{s.name} - site report", (
            f"{s.name} is located in the {s.region}. Construction finished in {s.built}.",
            f"{s.name} is operated by {s.operator}. Its annual capacity is {s.capacity} {s.unit}.",
            f"Safety record: {s.name} reported no lost-time incidents in the last audit cycle.",
        ))  # fmt: skip
    for n, p in enumerate(products):
        docs[f"doc-p{n:02d}"] = Doc(f"doc-p{n:02d}", f"{p.name} - product sheet", (
            f"{p.name} is a {p.category} product sold to industrial buyers.",
            f"{p.name} is made by {p.maker}. It was launched in {p.launched}.",
            f"Customer notes: buyers of {p.name} cite delivery times and support quality.",
        ))  # fmt: skip
    for n in range(8):  # market notes: names in passing, no answers
        mentioned = rng.sample(companies, 3)
        docs[f"doc-m{n:02d}"] = Doc(f"doc-m{n:02d}", f"Market notes {n + 1} - newsletter", (
            "Analysts covered " + ", ".join(c.name for c in mentioned) + " this quarter.",
            f"Shares in {mentioned[0].name} moved on sector news; {mentioned[1].name} declined "
            "to comment on capacity plans.",
        ))  # fmt: skip
    return World(tuple(companies), tuple(sites), tuple(products), docs)
