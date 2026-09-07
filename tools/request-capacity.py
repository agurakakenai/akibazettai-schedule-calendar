"""Conservative, dependency-free admission profiles for the new timing contracts.

Profiles reserve measured static text/schema costs plus framing, and bound
untrusted text by its escaped UTF-8 bytes. They are not service TPM accounting
or a vision-token estimator. Re-measure before changing a pinned contract.
"""
import hashlib
import json


LIMIT = 10000
FRAMING_RESERVE = 512
PERSONAL = {
    "promptHash": "8213567ed0473d4e4557e254d1b4f81098d4481305e7beab53d8c452af8dc3ca",
    "schemaHash": "14a171cc8689ebe4996f6aff7f2f97081a46133b8c86622b565883422351268a",
    "output": 2304, "system": 1664, "schemaBase": 736, "schemaPerLine": 9,
    "lineFrame": 11, "bodyFrame": 16,
}
HALF_MONTH = {
    "promptHash": "5f789845d7a1f90f8795f0d50e5e910891324daf6b0e4a77c62e578ffb62b41d",
    "schemaHash": "daa559fb7b9a86d39fc91c1c9626adb9aa9cd182cb49bc065220be40ccec9e20",
    "output": 3840, "system": 1024, "schemaBase": 640,
}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class CapacityHold(ValueError):
    def __init__(self, reason="azure_capacity_hold"):
        self.reason = reason
        super().__init__(reason)


def profile_hash(profile):
    return digest(canonical({"profile": profile, "limit": LIMIT, "framingReserve": FRAMING_RESERVE}))


def _profile(profile, prompt, schema, output):
    if (digest(prompt) != profile["promptHash"] or digest(canonical(schema)) != profile["schemaHash"]
            or type(output) is not int or output != profile["output"]):
        raise CapacityHold("azure_capacity_profile_stale")


def _admit(reservation):
    if reservation > LIMIT:
        raise CapacityHold()
    return {"textReservationBound": reservation, "admissionLimit": LIMIT,
            "framingReserve": FRAMING_RESERVE, "imageTokensIncluded": False,
            "serviceTpmGuaranteed": False}


def personal(payload, prompt, schema, output):
    _profile(PERSONAL, prompt, schema, output)
    lines = payload.get("bodyLines") if isinstance(payload, dict) else None
    if not isinstance(lines, list) or not 1 <= len(lines) <= 128:
        raise CapacityHold()
    escaped = 0
    for index, line in enumerate(lines, 1):
        if (not isinstance(line, dict) or set(line) != {"id", "text"}
                or type(line["id"]) is not int or line["id"] != index
                or not isinstance(line["text"], str)):
            raise CapacityHold()
        escaped += len(canonical(line["text"])[1:-1].encode("utf-8"))
    metadata = {key: value for key, value in payload.items() if key != "bodyLines"}
    return _admit(
        PERSONAL["system"] + PERSONAL["schemaBase"]
        + (PERSONAL["schemaPerLine"] + PERSONAL["lineFrame"]) * len(lines)
        + PERSONAL["bodyFrame"] + len(canonical(metadata).encode("utf-8")) + escaped
        + output + FRAMING_RESERVE)


def half_month(user_text, prompt, schema, output):
    _profile(HALF_MONTH, prompt, schema, output)
    if not isinstance(user_text, str):
        raise CapacityHold()
    return _admit(HALF_MONTH["system"] + HALF_MONTH["schemaBase"]
                  + len(user_text.encode("utf-8")) + output + FRAMING_RESERVE)
