"""Source-bound work boundaries, independent of attendance and arrival notices."""
import copy
import datetime as dt
import re


DAY, NIGHT = "\u663c", "\u591c"
MAX_FACTS = 512
FACT_FIELDS = ("serviceDate", "shift", "boundary", "status", "qualifier", "explicitTime")
SOURCE_FIELDS = ("id", "url", "name", "authorId", "authorScreenName", "createdAt", "sourceKind")
KINDS = ("personal-work-post", "half-month-schedule")
QUALIFIERS = {"short": (DAY, "end"), "long": (DAY, "end"),
              "early": (NIGHT, "start"), "late": (NIGHT, "start")}
QUALIFIER_TIMES = {"short": "16:00", "long": "18:00", "early": "16:00", "late": "18:00"}
WIRE_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": list(FACT_FIELDS),
    "properties": {
        "serviceDate": {"type": "string", "pattern": r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"},
        "shift": {"type": "string", "enum": [DAY, NIGHT]},
        "boundary": {"type": "string", "enum": ["start", "end"]},
        "status": {"type": "string", "enum": ["set", "excluded", "withdrawn", "conflict"]},
        "qualifier": {"type": ["string", "null"], "enum": [*QUALIFIERS, None]},
        "explicitTime": {"type": ["string", "null"]},
    },
}
COMPACT_FIELDS = ("shift", "kind", "time")
COMPACT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": list(COMPACT_FIELDS),
    "properties": {
        "shift": {"type": "string", "enum": [DAY, NIGHT]},
        "kind": {"type": "string", "enum": [
            *QUALIFIERS, "time", *("not-" + word for word in QUALIFIERS), "not-time", "withdrawn", "conflict"]},
        "time": {"type": ["string", "null"], "pattern": r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$"},
    },
}


class WorkTimingLimitError(ValueError):
    def __init__(self):
        super().__init__("work_timing_storage_limit")


def _require(condition):
    if not condition:
        raise ValueError("invalid_work_timing")


def _keys(value, fields):
    _require(isinstance(value, dict) and set(value) == set(fields))


def _date(value):
    _require(isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value))
    _require(dt.date.fromisoformat(value).isoformat() == value)
    return value


def _timestamp(value):
    _require(isinstance(value, str) and re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](?:\.[0-9]{1,6})?Z", value))
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def validate_source(source):
    _keys(source, SOURCE_FIELDS)
    _require(isinstance(source["id"], str) and re.fullmatch(r"[1-9][0-9]{9,24}", source["id"]))
    _require(isinstance(source["authorId"], str) and re.fullmatch(r"[1-9][0-9]{0,24}", source["authorId"]))
    _require(isinstance(source["authorScreenName"], str)
             and re.fullmatch(r"[A-Za-z0-9_]{1,15}", source["authorScreenName"]))
    _require(source["url"] == f'https://x.com/{source["authorScreenName"]}/status/{source["id"]}')
    name = source["name"]
    _require(isinstance(name, str) and re.fullmatch(
        r"[\u3041-\u3093\u30a1-\u30f6\u4e00-\u9fa0\u30fc\uff41-\uff5aA-Za-z0-9]{1,12}", name))
    _require(isinstance(source["sourceKind"], str) and source["sourceKind"] in KINDS)
    created = _timestamp(source["createdAt"])
    milliseconds = (int(source["id"]) >> 22) + 1288834974657
    _require(abs(milliseconds / 1000 - created.timestamp()) < 2)
    return source


def source_metadata(post, kind):
    source = {field: post[field] for field in SOURCE_FIELDS if field != "sourceKind"}
    source["sourceKind"] = kind
    return validate_source(source)


def scope(fact):
    return fact["serviceDate"], fact["shift"], fact["boundary"]


def fact_key(fact):
    return (*scope(fact), "excluded", fact["qualifier"], fact["explicitTime"]) \
        if fact["status"] == "excluded" else scope(fact)


def matches_target(exclusion, fact):
    if exclusion["status"] != "excluded" or fact["status"] != "set" or scope(exclusion) != scope(fact):
        return False
    word = fact["qualifier"]
    if word is None:
        word = next((name for name, role in QUALIFIERS.items()
                     if role == (fact["shift"], fact["boundary"])
                     and QUALIFIER_TIMES[name] == fact["explicitTime"]), None)
    if exclusion["qualifier"] is not None:
        return exclusion["qualifier"] == word
    return exclusion["explicitTime"] == (fact["explicitTime"] or QUALIFIER_TIMES.get(word))


def validate_fact(fact, with_source=False):
    _keys(fact, (*FACT_FIELDS, *(("source",) if with_source else ())))
    _date(fact["serviceDate"])
    _require(isinstance(fact["shift"], str) and fact["shift"] in (DAY, NIGHT))
    _require(isinstance(fact["boundary"], str) and fact["boundary"] in ("start", "end"))
    _require(isinstance(fact["status"], str) and fact["status"] in ("set", "excluded", "withdrawn", "conflict"))
    qualifier, when = fact["qualifier"], fact["explicitTime"]
    _require(qualifier is None or isinstance(qualifier, str) and qualifier in QUALIFIERS)
    _require(when is None or isinstance(when, str)
             and re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", when))
    if fact["status"] in ("set", "excluded"):
        _require(qualifier is not None or when is not None)
        if fact["status"] == "excluded":
            _require((qualifier is None) != (when is None))
        if qualifier is not None:
            _require(QUALIFIERS[qualifier] == (fact["shift"], fact["boundary"]))
    else:
        _require(qualifier is None and when is None)
    if with_source:
        validate_source(fact["source"])
    return fact


def expand_compact(value, service_date):
    """The wire requests only the displayed boundary: day end or night start."""
    _keys(value, COMPACT_FIELDS)
    kind = value["kind"]
    _require(isinstance(kind, str) and kind in COMPACT_SCHEMA["properties"]["kind"]["enum"])
    excluded = kind.startswith("not-")
    word = kind[4:] if excluded else kind
    fact = {
        "serviceDate": service_date, "shift": value["shift"],
        "boundary": "end" if value["shift"] == DAY else "start",
        "status": "excluded" if excluded else kind if kind in ("withdrawn", "conflict") else "set",
        "qualifier": word if word in QUALIFIERS else None,
        "explicitTime": value["time"],
    }
    return validate_fact(fact)


def validate(value, owner=None, date=None, days=None, source_kind=None):
    _keys(value, ("schemaVersion", "facts"))
    _require(type(value["schemaVersion"]) is int and value["schemaVersion"] == 1)
    _require(isinstance(value["facts"], list))
    if len(value["facts"]) > MAX_FACTS:
        raise WorkTimingLimitError()
    seen = set()
    for fact in value["facts"]:
        validate_fact(fact, with_source=True)
        key = fact_key(fact)
        _require(key not in seen)
        seen.add(key)
        source = fact["source"]
        if date is not None:
            _require(fact["serviceDate"] == date)
        if days is not None:
            _require(fact["shift"] in days.get(fact["serviceDate"], ()))
        if source_kind is not None:
            _require(source["sourceKind"] == source_kind)
        if owner is not None:
            _require(all(source[field] == owner[field]
                         for field in ("name", "authorId", "authorScreenName")))
            if source["sourceKind"] == "personal-work-post":
                _require(all(source[field] == owner[field] for field in ("id", "url", "createdAt")))
            else:
                _require((_timestamp(source["createdAt"]), int(source["id"]))
                         <= (_timestamp(owner["createdAt"]), int(owner["id"])))
    return value


def bind(facts, post, kind):
    _require(isinstance(facts, list))
    source = source_metadata(post, kind)
    result = {"schemaVersion": 1, "facts": []}
    for fact in facts:
        validate_fact(fact)
        result["facts"].append({**copy.deepcopy(fact), "source": copy.deepcopy(source)})
    return validate(result, owner=post, source_kind=kind)


def merge(previous, update, days=None):
    """Keep unmentioned facets and the original source of every retained fact."""
    values = {}
    for index, channel in enumerate((previous, update)):
        if channel is None:
            continue
        validate(channel)
        if index == 1:
            # An authorized same-post reinterpretation can replace its earlier
            # denial. Co-emitted exclusions below still apply to the new facts.
            for key, old in list(values.items()):
                if old["status"] != "excluded":
                    continue
                for fact in channel["facts"]:
                    same_source = all(old["source"][field] == fact["source"][field]
                                      for field in SOURCE_FIELDS if field != "createdAt")
                    same_source = same_source and _timestamp(old["source"]["createdAt"]) == _timestamp(
                        fact["source"]["createdAt"])
                    if same_source and matches_target(old, fact):
                        del values[key]
                        break
        for fact in channel["facts"]:
            key = fact_key(fact)
            old = values.get(key)
            order = lambda item: (_timestamp(item["source"]["createdAt"]), int(item["source"]["id"]))
            if old is None or order(fact) >= order(old):
                values[key] = copy.deepcopy(fact)
    result = {"schemaVersion": 1, "facts": [
        values[key] for key in values
        if days is None or key[1] in days.get(key[0], ())]}
    return validate(result, days=days)
