from tests.sdk_reader_fixture import answer, call
from tests.support.native_image_pipeline import admit_image, claim, pixels, upload_headers


async def animation(core, client, config, plan, tmp_path, mime, *, number=42):
    event, image = await admit_image(core, client, config, plan, tmp_path, number=number)
    lease = await claim(client, event, image)
    data = pixels(mime)
    response = await client.post("/internal/v2/content/" + event["event_id"] + "/image",
        content=data, headers=upload_headers(plan, event, image, lease, data, mime))
    assert response.status_code == 200, response.text
    return event


def responses():
    return [answer(calls=[call("motion-1", "understand_video", {"question": "动作怎样变化？",
                  "preview_reply": "抽帧里这一团橙色好抢眼。像个小橘子。"})]),
            answer("一轮动图从红色画面变为橙色画面。"), answer("画面由红色变成橙色。")]
