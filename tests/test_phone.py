from station import phone


def test_kinds():
    assert phone.kind("192.168.1.20") == "network"
    assert phone.kind("192.168.1.5") == "network"
    assert phone.kind("100.64.0.10") == "tailscale"
    for ip in ("127.0.0.1", "169.254.3.4", "8.8.8.8", "fe80::1", "nonsense"):
        assert phone.kind(ip) is None


def test_addresses_order_and_dedupe():
    a = phone.addresses("192.168.1.20:8091", 8091, ips=["127.0.0.1", "192.168.1.20", "169.254.1.1", "100.64.0.10"])
    assert [x["url"] for x in a] == ["http://192.168.1.20:8091/", "http://100.64.0.10:8091/"]
    assert a[1]["label"].startswith("Tailscale")
    assert phone.addresses("localhost:8091", 8091, ips=["192.168.0.9"])[0]["url"] == "http://192.168.0.9:8091/"


def test_qr_svg():
    svg = phone.qr_svg("http://192.168.0.9:8091/")
    assert svg.startswith("<svg") and "path" in svg


def test_card_download_link_from_config():
    html = "<p><!--DOWNLOAD_QR--><!--DOWNLOAD_URL--></p>"
    out = phone.card_with_download(html, "https://example.org/get")
    assert "https://example.org/get" in out and "<svg" in out
    assert phone.REPO_URL in phone.card_with_download(html, None)           # not set: the public project page
    assert phone.REPO_URL.startswith("https://github.com/") and phone.REPO_URL.endswith("/ham-net-speech-to-text")
    assert "Ask the instructor" in phone.card_with_download(html, "")       # "" = no link on the card
