from companion_onebot_bridge import journal
from companion_onebot_bridge.spool import encode


def chain(*data):
    rows, head = [], "0" * 64
    for value in data:
        row = {"seq": len(rows) + 1, "previous": head, "at": "2026-10-26T12:00:00+00:00",
               "kind": "SYNTHETIC", "data": value}
        rows.append(row)
        head = journal.digest(row)
    return ("".join(encode(row) + "\n" for row in rows)).encode("utf-8"), rows, head


def write(path, raw):
    path.write_bytes(raw)
    path.chmod(0o600)
