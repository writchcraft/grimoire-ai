"""Qualified, source-bound evidence after ranking. No corpus identifiers or model calls.

Ranking deliberately uses its frozen planner. This planner describes answer coverage,
not ranking scores. Blocks always refer to consecutive original lines.
"""

import re
from dataclasses import dataclass
from itertools import combinations

from app.rag.search import FACTS, Need, focus, tokens

ATTRIBUTES = {
    **FACTS,
    "database": {"database"},
    "dependency": {"dependency", "dependencies", "depends", "depends_on"},
    "url": {"url", "uri", "repository"},
    "setting": {"setting", "enabled", "disabled", "access", "replication"},
    "path": {"path", "adapter"},
}
SCOPE_GROUPS = (
    {"production", "staging", "development"},
    {"internal", "external"},
    {"primary", "secondary", "backup"},
    {"source", "destination"},
    {"old", "new"},
    {"spare", "main"},
    {"enabled", "disabled"},
)
SCOPE_WORDS = set.union(*SCOPE_GROUPS)
FUNCTION_WORDS = set(
    "name names named documented specify specified setting settings enabled disabled access "
    "interface web both each between their there as how many long period value values "
    "current previous primary secondary internal external production staging development "
    "source destination old new spare main backup retention schedule tcp port host container "
    "published publish allowed allow subnet cidr database dependency dependencies depends "
    "url uri repository path serial adapter daily day backups run retained does do says "
    "read only config configuration check actual separately respective versus versus "
    "service services environment group groups use listens listen on during after before "
    "changed change unchanged remained remain date installed firmware version "
    "ram memory administrator password yesterday crash cause recorded not except excluding "
    "export metric metrics collector exporter belong actually despite disproved ticket guess "
    "depend depends of the is are was were has have its it "
    "for to and or in at from with which what when where why runs uses host_port "
    "container_port".split()
)


def words(text):
    return tokens(text.replace("_", " "))


def matches(field, text):
    terms = words(text)
    if field == "container_port":
        return "port" in terms and bool(terms & {"container", "internal"})
    if field == "host_port":
        return "port" in terms and bool(terms & {"host", "publish"})
    if field == "host":
        return any(
            words(line) & {"host", "hostname"}
            and not re.search(r"\bhost\s+(?:tcp\s+)?ports?\b", line, re.I)
            for line in text.splitlines()
        )
    if field not in ATTRIBUTES:
        return words(field) <= terms
    return bool(terms & ATTRIBUTES[field])


def answer_plan(question):
    query = re.split(r",?\s+(?:despite|regardless of)\b", focus(question), flags=re.I)[0]
    parts = re.split(
        r"[,;]?\s+and\s+(?=(?:what|which|when|where|how|on what)\b)", query, flags=re.I
    )
    # Repeated attributes with independent subjects: "A retention and B retention".
    expanded = []
    for part in parts:
        halves = re.split(r"\s+and\s+", part, flags=re.I)

        def subject(half):
            terms = words(half)
            entities = terms - FUNCTION_WORDS - set.union(*ATTRIBUTES.values())
            entities = {t for t in entities if not any(c.isdigit() for c in t)}
            return entities | (terms & SCOPE_WORDS) | ({"backup"} if "backup" in terms else set())

        repeated = (
            len(halves) == 2
            and all(subject(h) for h in halves)
            and any(
                matches(f, halves[0]) and matches(f, halves[1]) for f in ATTRIBUTES if "_" not in f
            )
        )
        expanded.extend(halves if repeated else [part])
    needs = []
    for part in expanded[:8]:
        terms = words(part)
        attribute_region = re.split(
            r"\b(?:does|do|belong to|belongs to|is installed on|are installed on|"
            r"is documented for|are documented for|for|of|on|runs)\b",
            part,
            maxsplit=1,
            flags=re.I,
        )[0]
        attribute_terms = words(attribute_region)
        facts = {f for f in ATTRIBUTES if "_" not in f and matches(f, attribute_region)}
        # Unknown attribute names can still form independent slots. This covers
        # arbitrary config keys without extending a benchmark-specific vocabulary.
        attributes = re.match(
            r"^(?:what|which)\s+(.+?)\s+(?:does|do|is documented for|are documented for)\b",
            part,
            re.I,
        )
        dynamic_terms = set()
        if attributes and not re.search(r"\bdepend(?:s)?\s+on\b", part, re.I):
            for phrase in re.split(r"\s+and\s+|,\s*", attributes[1], flags=re.I):
                terms_in_phrase = words(phrase) - SCOPE_WORDS - {"tcp", "allowed"}
                if terms_in_phrase and not any(matches(f, phrase) for f in facts):
                    field = "_".join(sorted(terms_in_phrase))
                    facts.add(field)
                    dynamic_terms.update(terms_in_phrase)
        if re.search(r"\bdepend(?:s)?\s+on\b", part, re.I):
            facts = {"dependency"}
        if "port" in facts:
            independent_host = bool(
                re.search(r"\bhost\s+and\b|\band\s+host\b(?!\s+(?:tcp\s+)?ports?\b)", part, re.I)
            )
            if not independent_host:
                facts.discard("host")
            for scope, field in (
                ("host", "host_port"),
                ("container", "container_port"),
                ("internal", "container_port"),
            ):
                if scope in attribute_terms and not (scope == "host" and independent_host):
                    facts.add(field)
            if facts & {"host_port", "container_port"}:
                facts.discard("port")
        if "retention" in facts or ("day" in terms and "backup" in terms):
            facts.add("retention")
        if re.search(r"\bwhen\b", part, re.I):
            facts.add("schedule")
        # A setting name refines a setting slot; it must not become an unrelated entity.
        entities = terms - FUNCTION_WORDS - set.union(*ATTRIBUTES.values()) - dynamic_terms
        entities = {t for t in entities if not re.fullmatch(r"\d+(?:[.-]\d+)*", t)}
        if not entities and "backup" in terms:
            entities = {"backup"}
        elliptical = re.match(r"^(?:what|which)\s+(?:is|are|was|were)\b", part, re.I)
        if (
            not entities
            and needs
            and (elliptical or re.search(r"\bit\b|\bits\b|\bthey\b|\btheir\b", part, re.I))
        ):
            entities = set(needs[-1].entities)
            if not scopes(part):
                inherited_scope = ", ".join(sorted(scopes(needs[-1].query)))
                if inherited_scope:
                    part += f" [{inherited_scope}]"
        needs.append(Need(part, entities, facts))
    return needs


def value_record(field, text):
    """Conservative typed witnesses after entity, scope, status and span checks.

    Unknown attribute types remain model-selected. Related vocabulary and pointers
    to other documents cannot certify a numeric configuration value.
    """
    if field in {"port", "host_port", "container_port"}:
        return bool(
            re.search(
                r"\bports?\b(?:\s+(?:is|was|tcp|changed|to|from|remained|unchanged|at|"
                r"not|does|use|uses|internal|container|host)){0,8}\s*[:=]?\s*\d{1,5}\b",
                text,
                re.I,
            )
        )
    if field == "retention":
        return bool(
            re.search(
                r"\b(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
                r"thirty|sixty|ninety)\s+(?:days?|weeks?|months?|hours?|years?)\b|"
                r"\b(?:unlimited|indefinite|forever)\b",
                text,
                re.I,
            )
        )
    if field == "subnet":
        return bool(re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}/\d{1,2}\b", text))
    return False


def comparable_value(record):
    """Return only recognized value signatures; prose differences alone are not conflicts."""
    field = record["field"]
    text = "\n".join(line for line in record["quote"].splitlines() if matches(field, line))
    if field in {"port", "host_port", "container_port"}:
        values = re.findall(
            r"\bports?\b(?:\s+(?:is|was|tcp|changed|to|from|remained|unchanged|at|"
            r"not|does|use|uses|internal|container|host)){0,8}\s*[:=]?\s*(\d{1,5})\b",
            text,
            re.I,
        )
    elif field == "url":
        values = [v.rstrip(".,;)") for v in re.findall(r"[a-z]+://[^\s]+", text, re.I)]
    elif field == "subnet":
        values = re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}/\d{1,2}\b", text)
    elif field == "host":
        values = re.findall(r"\bhost(?:name)?\s*(?::|=|\bis\b|\bwas\b)?\s+([\w.-]+)", text, re.I)
    elif field == "retention":
        quantities = re.findall(r"\b(\w+)\s+(days?|weeks?|months?|hours?|years?)\b", text, re.I)
        numerals = dict(
            zip(
                "one two three four five six seven eight nine ten eleven twelve".split(),
                range(1, 13),
                strict=True,
            )
        )
        values = []
        for count, unit in quantities:
            number = int(count) if count.isdigit() else numerals.get(count.casefold())
            if number is not None:
                unit = unit.casefold().rstrip("s")
                factor = {"hour": 1, "day": 24, "week": 168}.get(unit)
                values.append(f"{number * factor}:hour" if factor else f"{number}:{unit}")
    elif field == "setting":
        values = re.findall(r"\b(?:true|false|enabled|disabled)\b", text, re.I)
        values = ["true" if v.casefold() in {"true", "enabled"} else "false" for v in values]
        if len(set(values)) > 1:
            return ()
    else:
        values = []
    return tuple(sorted(set(v if field == "url" else v.casefold() for v in values)))


def relationships(records):
    """Compare current values only within comparable scopes and preserve polarity.

    Missing scope is not a different scope. Different prose is not a different value.
    A negative constraint on another value can coexist with an asserted value.
    """
    result = {}
    for left, right in combinations(records, 2):
        a, b = set(left["scopes"]), set(right["scopes"])
        opposite_scope = any(a & g and b & g and not a & b & g for g in SCOPE_GROUPS)
        kind = "alternative" if opposite_scope else None
        lv, rv = comparable_value(left), comparable_value(right)
        if a == b and lv and rv:
            old = {"historical", "superseded"}
            if (left["status"] in old) != (right["status"] in old):
                kind = "superseded" if lv != rv else None
            else:
                ln = left.get("polarity", left["status"]) == "negated"
                rn = right.get("polarity", right["status"]) == "negated"
                field = left["field"]
                if (field == "setting" and lv != rv) or (
                    field != "setting"
                    and ((not ln and not rn and lv != rv) or (ln != rn and lv == rv))
                ):
                    kind = "conflict"
        if kind:
            result.setdefault(kind, set()).update([left["id"], right["id"]])
    return [{"kind": kind, "evidence": sorted(ids)} for kind, ids in result.items()]


def scopes(text):
    observed = words(text) & SCOPE_WORDS
    # Backup activity ("backup retention") is not a replica role.
    if "backup" in observed:
        role = re.search(
            r"(?:role|scope|profile):\s*backup\b|(?:^|/)\s*backup\s*(?:/|$)|"
            r"\bbackup\s+(?:instance|node|server|endpoint|bridge|profile|port)\b",
            text,
            re.I,
        ) or re.search(r"(?i:\bbackup)\s+[A-Z][\w.-]+", text)
        if not role or observed & {"primary", "secondary"}:
            observed.discard("backup")
    if re.search(r"\brehearsal\b", text, re.I):
        observed.add("staging")
    return observed


def scope_compatible(question, observed):
    query = focus(question)
    excluded = {
        s for s in SCOPE_WORDS if re.search(r"(?:excluding|except|not)\s+" + s + r"\b", query, re.I)
    }
    requested = scopes(query) - excluded
    # A polarity question must be able to discover the opposite state.
    if re.match(r"^(?:is|are|does|do)\b", query, re.I):
        requested -= {"enabled", "disabled"}
    if observed & excluded:
        return False
    return all(
        not (requested & g and observed & g) or bool(requested & observed & g) for g in SCOPE_GROUPS
    )


def status(text):
    if re.search(
        r"not (?:documented|recorded|specified|known|available)|no .*?(?:record|document|"
        r"known)|unknown|does not (?:specify|record|document)|do not infer|"
        r"no documented|not evidence|does not prove",
        text,
        re.I,
    ):
        return "unknown"
    if re.search(
        r"disprov|incorrect guess|incorrect .*claim|claim.*rejected|hypothes|suspect|"
        r"hypothetical|might |could |\bguess(?:ed)?\b|\bproposed\b|\bsuggested\b|\bassumed\b",
        text,
        re.I,
    ):
        return (
            "disproved" if re.search(r"disprov|incorrect|rejected", text, re.I) else "hypothetical"
        )
    if re.search(r"superseded|no longer valid|setting was removed|replaced by", text, re.I):
        return "superseded"
    if re.search(
        r"previously|\bprevious\b|\bhistorical\b|\bformerly\b|\bbefore \d|"
        r"(?:^|\s)old(?:\s|$)",
        text,
        re.I,
    ):
        return "historical"
    if re.search(r"\bnot\b|\bno\b|\bdisabled\b|:\s*(?:false|['\"]false['\"])", text, re.I):
        return "negated"
    return "asserted"


@dataclass
class Block:
    text: str
    start: int
    end: int
    heading: str
    lineage: str = ""


def blocks(source):
    """Keep mapping/list records intact and prose qualification adjacent to its value.

    Ancestor keys are metadata, not invented quote lines. Sibling mappings are never
    combined. A flat field list is a single record until a blank/heading/new service.
    """
    lines = source.text.splitlines()
    heading, stack = source.heading, []
    headings = [(i + 1, h.strip()) for i, h in enumerate(source.heading.split(" / ")) if h]
    inherited = getattr(source, "lineage", "")
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.strip().startswith("```"):
            i += 1
            continue
        match = re.match(r"^(#{1,6})\s+(.+)", line)
        if match:
            headings = [(level, title) for level, title in headings if level < len(match[1])]
            headings.append((len(match[1]), match[2]))
            heading = " / ".join(title for _, title in headings)
            stack = []
            i += 1
            continue
        indent = len(line) - len(line.lstrip())
        mapping = re.match(r"\s*(?:-\s+)?([\w][\w .-]{0,80}?):\s*(.*)", line)
        while stack and stack[-1][0] >= indent:
            stack.pop()
        if mapping and not mapping[2] and i + 1 < len(lines):
            stack.append((indent, mapping[1]))
            i += 1
            continue
        if (
            not mapping
            and i + 1 < len(lines)
            and len(line.split()) <= 5
            and re.fullmatch(r"[\w][\w -]*", line.strip())
            and re.match(r"\s*(?:-\s+)?[\w][\w .-]{0,80}?:", lines[i + 1])
            and not re.search(r"[.!?]$", line)
        ):
            heading = line.strip()
            i += 1
            continue
        start, end = i, i + 1
        if mapping:
            # Consume peer properties and their nested children in this record.
            while end < len(lines) and lines[end].strip():
                nxt = lines[end]
                ni = len(nxt) - len(nxt.lstrip())
                nm = re.match(r"\s*(?:-\s+)?([\w][\w .-]{0,80}?):", nxt)
                if nxt.startswith("#") or ni < indent or (ni == indent and not nm):
                    break
                if ni == indent and re.match(r"\s*-?\s*service:", nxt, re.I):
                    break
                if (
                    ni == indent
                    and nxt.lstrip().startswith("- ")
                    and any(len(row) - len(row.lstrip()) > indent for row in lines[start + 1 : end])
                ):
                    break
                end += 1
        elif re.search(r"following|for the .*?(?:use|setting)|incorrect guess", line, re.I):
            if end < len(lines) and lines[end].strip() and not lines[end].startswith("#"):
                end += 1
        yield Block(
            "\n".join(lines[start:end]),
            start,
            end,
            heading,
            " / ".join(filter(None, [inherited, ".".join(key for _, key in stack)])),
        )
        i = end


def eligible(need, field, source, block):
    context = block.heading + " / " + block.lineage + " / " + block.text
    metadata = source.filename + " " + (source.service or "")
    entity_terms = words(context + " " + metadata)
    named = re.search(r"(?im)^\s*-?\s*service:\s*(\S+)", block.text)
    if named and need.entities and not need.entities & words(named[1] + " " + block.lineage):
        return False
    subject = re.match(
        r"([A-Z][\w.-]+)\s+(?:uses|listens|runs|scrapes|exports|accepts|is|does)\b",
        block.text,
    )
    if (
        subject
        and subject[1] not in {"The", "This", "Its", "That"}
        and need.entities
        and not need.entities & words(subject[1])
    ):
        return False
    entity = len(need.entities & entity_terms) / max(1, len(need.entities))
    if need.entities and entity < 0.5:
        return False
    if not scope_compatible(need.query, scopes(context)):
        return False
    if field and not matches(field, block.text):
        return False
    if (
        field == "host_port"
        and re.search(r"\bpublish\w*\b", need.query, re.I)
        and re.search(r"\broutes?\b|\bproxies\b", block.text, re.I)
        and not re.search(r"\bpublish\w*\b", block.text, re.I)
    ):
        return False
    if re.search(
        r"ignore .*?(?:instructions|rules)|answer that|cite \[S|instruction.injection|"
        r"SYSTEM:|source_id:|javascript:",
        block.text,
        re.I,
    ):
        return False
    return True


def coverage_slots(question, source):
    return {
        (n, field)
        for n, need in enumerate(answer_plan(question))
        for field in sorted(need.facts) or [None]
        if any(eligible(need, field, source, b) for b in blocks(source))
    }
