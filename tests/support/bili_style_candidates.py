from companion import style_corpus
from tests.support.bili_style_corpus import NOW, VIDEO, comment, row

GOOD = {
    "context_tags": ["daily_share", "light_humor"],
    "attention_target": "前文里一个具体的反差细节",
    "response_action": "抓住这个细节做一次轻度夸张的联想",
    "appropriate_when": "轻松分享，对方没有求助或表达痛苦",
    "avoid_when": "认真求助、愤怒指责、痛苦经历或关系不明",
}


def corpus(*rows):
    return style_corpus.build(list(rows), NOW)[0]


def pair_rows(event="evt_a", video=VIDEO, parent="这只猫睡觉的姿势也太离谱了",
              reply="像是被谁按了暂停键一样"):
    return row(event, video, [comment("1", parent), comment("2", reply, parent="1")])


def live(c):
    return {r["event_id"] for r in c["records"]}
