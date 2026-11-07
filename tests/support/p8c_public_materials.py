import ipaddress
import re

EXAMPLE_NETWORKS = tuple(ipaddress.ip_network(value) for value in (
    "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24",
))


def privacy_categories(text):
    categories = set()
    for value in re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text):
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            continue
        if not (address.is_loopback or address.is_unspecified or any(
            address in network for network in EXAMPLE_NETWORKS
        )):
            categories.add("literal_network_address")
    patterns = {
        "private_absolute_path": r"(?i)(?:(?<![a-z0-9])[a-z]:[\\/]|/(?:home|users|root)/)",
        "numeric_qq_identity": (
            r"(?i)\b(?:owner_qq_id|bot_qq_id|qq_id)\b[\"']?\s*[:=]\s*[\"']?\d{5,15}"
        ),
        "credential_like_value": r"\b(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]{16,}\b",
        "ssh_endpoint_details": (
            r"(?i)\bssh[_ -]*(?:port|端口)\b[\"'`]?\s*[:=]?\s*[\"'`]?\d+"
        ),
    }
    for category, pattern in patterns.items():
        if re.search(pattern, text):
            categories.add(category)
    return categories
