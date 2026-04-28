import base64
import json
from pathlib import Path
from urllib.request import Request, urlopen


base_url = "http://127.0.0.1:18080"

# Preferred query mode:
# 1. Set bnet_id + match_order to fetch the Nth recent match.
# 2. Or set customer_token + match_id to fetch a specific match directly.
bnet_id = "oL1ama#5684"
customer_token = ""
match_order = 1  # 1-based
match_id = ""

test_dir = Path(__file__).resolve().parent


def build_payload() -> dict:
    payload = {
        "show_all_heroes": True,
        "analyze": True,
    }

    if customer_token:
        payload["customer_token"] = customer_token
    elif bnet_id:
        payload["bnet_id"] = bnet_id
    else:
        raise RuntimeError("Please set bnet_id, or set both customer_token and match_id.")

    if match_id:
        payload["match_id"] = match_id
        if "customer_token" not in payload:
            raise RuntimeError("customer_token is required when match_id is provided directly.")
    else:
        if int(match_order) <= 0:
            raise RuntimeError("match_order must be >= 1.")
        payload["index"] = int(match_order) - 1

    return payload


def image_filename(image_index: int, media_type: str) -> str:
    known_names = [
        "dashen-match-detail-main",
        "dashen-match-detail-data-page",
        "dashen-match-detail-ai-review",
    ]
    if image_index < len(known_names):
        stem = known_names[image_index]
    else:
        stem = f"dashen-match-detail-extra-{image_index + 1}"

    media_type = str(media_type or "").lower()
    if "jpeg" in media_type or "jpg" in media_type:
        suffix = ".jpg"
    else:
        suffix = ".png"
    return stem + suffix


payload = build_payload()
body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

request = Request(
    f"{base_url}/api/v2/dashen-match/detail/replies",
    data=body,
    headers={"Content-Type": "application/json; charset=utf-8"},
    method="POST",
)

with urlopen(request, timeout=300) as response:
    result = json.loads(response.read().decode("utf-8"))

json_path = test_dir / "dashen-match-detail-replies.json"
json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"saved: {json_path}")

if result.get("ok") is not True:
    raise RuntimeError(json.dumps(result, ensure_ascii=False, indent=2))

image_count = 0
text_count = 0
for reply in result.get("replies") or []:
    if not isinstance(reply, dict):
        continue

    reply_type = str(reply.get("type") or "").strip().lower()
    if reply_type == "image":
        encoded = str(reply.get("base64") or "").strip()
        if not encoded:
            continue
        output_path = test_dir / image_filename(image_count, str(reply.get("media_type") or "image/png"))
        output_path.write_bytes(base64.b64decode(encoded))
        image_count += 1
        print(f"saved: {output_path}")
        continue

    if reply_type == "text":
        text_count += 1
        output_path = test_dir / f"dashen-match-detail-note-{text_count}.txt"
        output_path.write_text(str(reply.get("data") or ""), encoding="utf-8")
        print(f"saved: {output_path}")

print(f"match_kind: {result.get('match_kind')}")
print(f"image_count: {image_count}")
print(f"text_count: {text_count}")
