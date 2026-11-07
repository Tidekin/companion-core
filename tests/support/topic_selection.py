import json

from companion.store import after
from tests.support.watch_ledger import observe


def judged(core, number, tags, rating, *, days_ago=0, url=None):
    event_id = observe(core, number, url=url)
    at = after(core.clock(), -days_ago * 86400)
    core.store.execute(
        "INSERT INTO watch_ledger(entry_id,source_event_id,platform,item_ref,depth,state,rating,"
        "take,topic_tags_json,created_at,updated_at) VALUES(?,?,'bilibili',?,'text','JUDGED',"
        "?,'看过',?,?,?)", (f"watch_{number}", event_id, url or event_id, rating,
                           json.dumps(tags, ensure_ascii=False), at, at))
    return event_id
