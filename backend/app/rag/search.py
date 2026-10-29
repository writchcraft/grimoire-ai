"""Bounded hybrid retrieval and evidence selection; no model or corpus-specific rules."""

import re
import time
from dataclasses import dataclass

STOP = set(
    (
        "what which when where how why is are was were does do did the a an on at in of for to "
        "and or it its has have with from this that actual documented documentation use uses using "
        "excluding only about much many installed run runs allowed expose exposes"
    ).split()
)
VARIANTS = {
    "ports": "port",
    "backups": "backup",
    "retained": "retention",
    "retain": "retention",
    "days": "day",
    "published": "publish",
    "publishes": "publish",
    "exposed": "publish",
    "expose": "publish",
    "exports": "export",
    "changed": "change",
    "changes": "change",
    "configuration": "config",
    "connections": "connection",
    "listens": "listen",
    "hosts": "host",
    "metrics": "metric",
}
FACTS = {
    "port": {"port"},
    "host_port": {"host", "port"},
    "container_port": {"container", "port"},
    "subnet": {"subnet", "cidr"},
    "retention": {"retention"},
    "schedule": {"daily", "schedule", "cron", "utc"},
    "check": {"check", "test"},
    "host": {"host", "hostname"},
    "password": {"password", "credential"},
    "version": {"version"},
    "memory": {"ram", "memory"},
    "cause": {"crash", "cause", "error"},
}
ATTRIBUTE_WORDS = set().union(*FACTS.values()) | set(
    (
        "tcp container production staging development publish config read-only read only "
        "check backup "
        "day before after change unchanged remained date port host listen export metric instance "
        "deployment administrator root yesterday"
    ).split()
)


def tokens(text):
    # Keep identifiers (IPs, hostnames, config keys, dates and URLs) intact.
    raw = re.findall(r"[\w]+(?:[.:/-][\w]+)*", text.casefold())
    return {VARIANTS.get(t, t) for t in raw if t not in STOP}


@dataclass
class Need:
    query: str
    entities: set[str]
    facts: set[str]


def focus(query):
    # Reported assertions before a new question are context, not positive search terms.
    return re.split(r"[.!?]\s+(?=(?:What|Which|When|Where|How|Why)\b)", query)[-1]


def plan(query):
    query = focus(query)
    # Clauses beginning with a new interrogative are independently retrievable.
    parts = re.split(
        r"[,;]?\s+and\s+(?=(?:what|which|when|where|how|on what)\b)", query, flags=re.I
    )
    needs = []
    for part in parts[:4]:
        terms = tokens(part)
        facts = {name for name, words in FACTS.items() if "_" not in name and terms & words}
        if re.search(r"\bwhen\b", part, re.I):
            facts.add("schedule")
        if "retention" in terms or ("day" in terms and "backup" in terms):
            facts.add("retention")
        # A host port is one attribute, not a request for a hostname.
        if "port" in facts and not re.search(
            r"which host runs|what host runs|hostname", part, re.I
        ):
            facts.discard("host")
        if "port" in facts:
            for scope in ("host", "container"):
                if scope in terms:
                    facts.add(scope + "_port")
            if facts & {"host_port", "container_port"}:
                facts.discard("port")
        entities = terms - ATTRIBUTE_WORDS - {"schedule", "subnet", "cidr"}
        if not entities and needs:
            entities = set(needs[-1].entities)
        needs.append(Need(part, entities, facts))
    return needs


def fact_matches(fact, text):
    terms = tokens(text)
    return FACTS[fact] <= terms if "_" in fact else bool(terms & FACTS[fact])


def coverage(need, text, metadata=""):
    all_terms = tokens(text + " " + metadata)
    entity = len(need.entities & all_terms) / max(1, len(need.entities)) if need.entities else 1.0
    facts = (
        sum(fact_matches(f, text) for f in need.facts) / max(1, len(need.facts))
        if need.facts
        else 1.0
    )
    return entity, facts


def components(query, hit, vector_score):
    query = focus(query)
    terms, body = tokens(query), tokens(hit.text)
    heading = hit.heading or " ".join(
        line for line in hit.text.splitlines() if line.startswith("#")
    )
    metadata_text = hit.filename + " " + (hit.service or "") + " " + heading
    metadata = tokens(metadata_text)
    overlap = len(terms & body) / max(1, len(terms))
    # Exact fact co-occurrence beats similarity alone, with small metadata boosts.
    best = max(
        (e * f for e, f in (coverage(n, hit.text, metadata_text) for n in plan(query))), default=0
    )
    scope = 0.0
    for name in ("production", "staging", "development"):
        if name in terms:
            excluded = bool(re.search(r"(?:excluding|except|not)\s+" + name, query, re.I))
            if name in tokens(heading or hit.text):
                scope += -0.7 if excluded else 0.12
            elif not excluded and tokens(heading) & {"production", "staging", "development"}:
                scope -= 0.6
    historical = bool(re.search(r"\b(before|changed|migration|previously)\b", hit.text, re.I))
    asks_change = bool(terms & {"change", "migration", "before", "after"}) or bool(
        re.search(r"\d{4}-\d{2}-\d{2}", query)
    )
    instruction = bool(
        re.search(
            r"ignore .*instructions|instruction.injection|answer that|cite \[S", hit.text, re.I
        )
    )
    return {
        "vector": 0.3 * max(0, vector_score),
        "overlap": 0.45 * overlap,
        "fact_entity": 0.65 * best,
        "metadata": 0.12 * len(terms & metadata) / max(1, len(terms)),
        "scope": scope,
        "historical": -0.25 if historical and not asks_change else 0,
        "instruction_text": -0.8 if instruction else 0,
    }


def near_duplicate(left, right, anchors=frozenset()):
    a, b = left.casefold().split(), right.casefold().split()
    if a == b:
        return True

    # Never collapse near-identical records with different exact values or negations.
    def protected(words):
        return {
            w
            for w in words
            if any(c.isdigit() for c in w) or w in {"not", "no", "only", "production", "staging"}
        }

    if (
        protected(a) != protected(b)
        or tokens(left) != tokens(right)
        or (tokens(left) & anchors) != (tokens(right) & anchors)
    ):
        return False
    sa = {tuple(a[i : i + 3]) for i in range(max(1, len(a) - 2))}
    sb = {tuple(b[i : i + 3]) for i in range(max(1, len(b) - 2))}
    return len(sa & sb) / max(1, len(sa | sb)) > 0.94


def search(store, query, vector, threshold=0.45, service=None, limit=12):
    start = time.perf_counter()
    needs = plan(query)
    semantic = store.semantic_search(vector, 48, threshold, service) if vector else []
    scores = {h.id: h.score for h in semantic}
    candidates = {h.id: h for h in semantic}
    entities = set().union(*(n.entities for n in needs))
    for q in dict.fromkeys([focus(query), *(n.query for n in needs)]):
        terms = tokens(q)
        ordered = sorted(terms, key=lambda t: (t not in entities, t))
        ordered += sorted(
            surface for surface, base in VARIANTS.items() if base in terms and surface not in terms
        )
        for hit in store.lexical_search(" ".join(ordered), 48, service):
            candidates.setdefault(hit.id, hit)
    ranked, debug = [], {}
    for hit in candidates.values():
        parts = components(query, hit, scores.get(hit.id, 0))
        score = sum(parts.values())
        debug[hit.id] = parts
        if score > 0.1:
            ranked.append(hit.model_copy(update={"score": score}))
    ranked.sort(key=lambda h: (-h.score, h.source_path, h.chunk_number, h.id))
    # Prefer distinct sources only among close scores, retaining clearly dominant sources.
    diverse, counts = [], {}
    while ranked:
        top = ranked[0]
        index = next(
            (
                i
                for i, h in enumerate(ranked)
                if counts.get(h.document_id, 0) < 2 and h.score >= top.score - 0.15
            ),
            0,
        )
        hit = ranked.pop(index)
        diverse.append(hit)
        counts[hit.document_id] = counts.get(hit.document_id, 0) + 1
    unique = []
    for hit in diverse:
        if any(near_duplicate(hit.text, h.text, entities) for h in unique):
            continue
        unique.append(hit)
        if len(unique) >= limit:
            break
    return unique, {
        "components": debug,
        "service_filter": service,
        "needs": [
            {"query": n.query, "entities": sorted(n.entities), "facts": sorted(n.facts)}
            for n in needs
        ],
        "candidate_count": len(candidates),
        "seconds": time.perf_counter() - start,
    }


def select_evidence(store, query, hits):
    """Cover every recognized fact, then add bounded same-section siblings."""
    needs = plan(query)
    pool = list(hits)
    for hit in hits[:4]:
        if hit.score >= 0.5 and any(coverage(n, hit.text, hit.heading)[0] >= 0.5 for n in needs):
            for sibling in store.section_neighbors(hit):
                if sibling.id not in {h.id for h in pool}:
                    pool.append(sibling)
    selected = []
    for need in needs:
        for fact in sorted(need.facts) or [None]:
            eligible = []
            for hit in pool:
                e, _ = coverage(
                    need, hit.text, hit.filename + " " + hit.heading + " " + (hit.service or "")
                )
                if hit.score >= 0.5 and e >= 0.5 and (not fact or fact_matches(fact, hit.text)):
                    eligible.append(hit)
            if not eligible:
                return []
            # Score each clause on its own, so one broad source cannot win every need
            # merely because it matched another part of the original question.
            eligible.sort(
                key=lambda h: -(sum(components(need.query, h, 0).values()) + 0.05 * h.score)
            )
            if eligible[0].id not in {h.id for h in selected}:
                selected.append(eligible[0])
    selected.sort(key=lambda h: -h.score)
    expanded = list(selected)
    for hit in selected:
        for sibling in store.section_neighbors(hit):
            if sibling.id not in {h.id for h in expanded} and len(expanded) < 8:
                expanded.append(sibling)
    return expanded


def complete(query, sources):
    # Necessary attribute/entity signals only; this does not prove semantic sufficiency.
    return all(
        any(
            coverage(n, s.text, s.filename + " " + s.heading + " " + (s.service or ""))[0] >= 0.5
            and (not fact or fact_matches(fact, s.text))
            for s in sources
        )
        for n in plan(query)
        for fact in sorted(n.facts) or [None]
    )


def claims_complete(query, claims, sources):
    available = {s.label: s for s in sources}
    return all(
        any(
            coverage(
                need,
                available[c["source"]].text,
                available[c["source"]].heading
                + " "
                + available[c["source"]].filename
                + " "
                + (available[c["source"]].service or ""),
            )[0]
            >= 0.5
            and (not fact or fact_matches(fact, c["quote"]))
            for c in claims
            if c["source"] in available
        )
        for need in plan(query)
        for fact in sorted(need.facts) or [None]
    )
