def parse_items(response: dict) -> list[str]:
    return [item["id"] for item in response["items"] if item["id"]]
