"""Author deterministic Chinese diagnostic fixtures; no model calls or private images.

Rendering requires an explicitly supplied CJK font. Existing photo bytes are copied
unchanged from the pinned scikit-image release with documented CC0/public-domain
licenses. The suite is small, constructed and not a population-quality benchmark.
"""

import argparse
import hashlib
import io
import json
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
DESTINATION = ROOT / "tests/fixtures/image_understanding"
PHOTO_SOURCE = "https://raw.githubusercontent.com/scikit-image/scikit-image/v0.25.2/skimage/data/"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def panels(path, font, before, after, bubble, *, colorful, changed=None):
    image = Image.new("RGB", (760, 680), "white")
    draw = ImageDraw.Draw(image)
    for index, title in enumerate((before, after)):
        offset = 20 + index * 330
        fill = "#e4f3ff" if colorful and index else "#ececec"
        draw.rectangle((10, offset, 750, offset + 310), fill=fill, outline="black", width=3)
        draw.text((30, offset + 10), title, font=font, fill="black")
        draw.ellipse((325, offset + 120, 395, offset + 190), outline="black", width=4)
        draw.line((360, offset + 190, 360, offset + 260), fill="black", width=5)
        draw.line((280, offset + 225, 440, offset + 225), fill="black", width=5)
        draw.line((360, offset + 260, 305, offset + 295), fill="black", width=5)
        draw.line((360, offset + 260, 415, offset + 295), fill="black", width=5)
        text = changed if index and changed else bubble
        draw.text((45, offset + 65), text, font=font, fill="black")
        for left in (60, 535):
            draw.rectangle((left, offset + 180, left + 150, offset + 275),
                           outline="black", width=4)
            draw.text((left + 10, offset + 195), "屏幕", font=font, fill="black")
    image.save(path)


def build(font_path, *, fetch_photos=False):
    DESTINATION.mkdir(parents=True, exist_ok=True)
    font = ImageFont.truetype(str(font_path), 26)
    cases = []

    def add(name, category, family, facts, interpretation, **extra):
        path = DESTINATION / (name + ".png")
        cases.append({"id": name, "category": category, "family": family,
            "split": "holdout" if family in {"repeat-3", "literal-1", "color-1",
                                               "ambiguous-1", "photo-clock_motion"} else "dev",
            "image": path.name, "image_sha256": digest(path.read_bytes()),
            "facts": facts, "gold": {"interpretation": interpretation,
                "unsupported": "不推断主人身份、情绪或与助手的角色对应。"},
            "label_origin": "authored_diagnostic_not_independent_human_rating", **extra})

    repeats = [
        ("课本里的他", "多年后的我", "我就喜欢和他们玩！"),
        ("以前我笑他只爱网上聊天", "现在的我下班以后", "和网友聊天真开心。"),
        ("小时候不懂爸爸为何坐着发呆", "工作之后坐在桌前的我", "让我自己待一会儿。"),
        ("以前笑别人打游戏总查攻略", "第一次玩新游戏的我", "先看看攻略。"),
    ]
    for index, (before, after, bubble) in enumerate(repeats):
        for colorful in (False, True):
            name = f"repeat-{index}-{int(colorful)}"
            panels(DESTINATION / (name + ".png"), font, before, after, bubble,
                   colorful=colorful)
            facts = (f"上下两格，标题分别为‘{before}’和‘{after}’。两格人物坐标、"
                     f"动作、屏幕位置相同，台词都是‘{bubble}’。"
                     + ("上格灰底，下格浅蓝底。" if colorful else "两格均为灰底。"))
            add(name, "repeated_context", f"repeat-{index}", facts,
                "时间或处境改变后，自己做了曾经不理解的事；有自嘲或理解旧处境的意味。"
                "不能单凭下格变彩色说它反驳了上格。",
                initial_wrong="下格变好看了，所以是在反驳上格。",
                initial_right="两格重复动作和台词，处境变成了自己，像在自嘲或终于理解。",
                correction_right="你漏掉了相同动作和台词，下面不是在推翻上面。",
                correction_wrong="不对，只要下格颜色不同，就一定是在反驳上格。")

    literals = [("上午", "下午", "我准备出发。", "我已经到家。"),
                ("出发之前", "下雨之后", "今天带伞了吗？", "伞在包里。")]
    for index, (before, after, bubble, changed) in enumerate(literals):
        for colorful in (False, True):
            name = f"literal-{index}-{int(colorful)}"
            panels(DESTINATION / (name + ".png"), font, before, after, bubble,
                   colorful=colorful, changed=changed)
            add(name, "literal_sequence", f"literal-{index}",
                f"上格标题‘{before}’，台词‘{bubble}’；下格标题‘{after}’，台词‘{changed}’。"
                "两格人物姿势相同，屏幕在左右两边。",
                "按明确时间和台词解释日常事情；没有足够证据必须解读成自我反讽。")

    for index in range(4):
        name = f"color-{index}"
        image = Image.new("RGB", (760, 400), "white")
        draw = ImageDraw.Draw(image)
        if index < 2:
            active = "red" if index == 0 else "green"
            draw.text((30, 15), "交通灯：现在可以通行吗？", font=font, fill="black")
            draw.rectangle((310, 80, 445, 370), fill="#222222")
            for top, color in ((100, "red"), (195, "yellow"), (290, "green")):
                draw.ellipse((340, top, 415, top + 65),
                             fill=color if active == color else "#555555")
            facts = f"交通灯自上而下为三圆灯，仅{('最上方红' if index == 0 else '最下方绿')}灯亮。"
            gold = "红灯时应等待。" if index == 0 else "绿灯表示允许通行，实际过路还需注意路况。"
        else:
            colors = ("red", "blue") if index == 2 else ("blue", "red")
            draw.text((25, 15), "温度图例：红色=高温，蓝色=低温", font=font, fill="black")
            for x, label, color in zip((100, 450), ("甲", "乙"), colors, strict=True):
                draw.rectangle((x, 120, x + 180, 300), fill=color)
                draw.text((x + 60, 320), label, font=font, fill="black")
            facts = f"图例红色=高温、蓝色=低温；甲块{colors[0]}，乙块{colors[1]}。"
            gold = "甲比乙温度高。" if index == 2 else "乙比甲温度高。"
        image.save(DESTINATION / (name + ".png"))
        add(name, "color_is_evidence", f"color-{index // 2}", facts, gold)

    for index in range(4):
        name = f"ambiguous-{index}"
        image = Image.new("RGB", (760, 400), "white")
        draw = ImageDraw.Draw(image)
        title = "他又来了" if index < 2 else "还是这样"
        draw.text((270, 35), title, font=font, fill="black")
        for left in (150, 440):
            draw.ellipse((left, 150, left + 100, 250),
                         fill=("#c8e5ff" if index % 2 else "#cccccc"), outline="black", width=3)
        image.save(DESTINATION / (name + ".png"))
        add(name, "ambiguous", f"ambiguous-{index // 2}",
            f"文字‘{title}’；下方并排两个相同圆形。没有人物表情、说话者或时间说明。",
            "能描述重复、等待等可能联想；信息不足以确定人物关系、情绪、讽刺或唯一寓意。")

    photos = {"chelsea": ("猫的头部近景，棕灰色毛发和条纹，眼睛与胡须可见。", "猫"),
              "coffee": ("木桌上有橙褐色杯碟，杯中深色饮料和浅色花纹，旁边有勺子。", "咖啡杯"),
              "brick": ("灰度照片，规则排列的长方形砖块与砖缝。", "砖墙"),
              "clock_motion": ("灰度照片，圆形钟面有明显横向拖影，无法辨清数字。", "模糊的钟面")}
    for name, (facts, gold) in photos.items():
        path = DESTINATION / (name + ".png")
        if fetch_photos:
            response = httpx.get(PHOTO_SOURCE + path.name, follow_redirects=True, timeout=30)
            response.raise_for_status()
            with Image.open(io.BytesIO(response.content)) as picture:
                picture.verify()
            path.write_bytes(response.content)
        add(name, "ordinary_photo", "photo-" + name, facts,
            f"正常描述{gold}的可见内容，不强加梗或寓意，也不能推断是主人拍摄或拥有。",
            asset_source=PHOTO_SOURCE + path.name, license="CC0/public domain; see README")
    output = {"schema_version": 1, "privacy": "constructed_tasks_and_public_assets_only",
              "cases": cases}
    (DESTINATION / "cases.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--font", type=Path, required=True)
    parser.add_argument("--fetch-photos", action="store_true")
    args = parser.parse_args()
    suite = build(args.font, fetch_photos=args.fetch_photos)
    print(json.dumps({"case_count": len(suite["cases"])}))
