import ipaddress
import socket
import unittest
from unittest import mock

from scripts import source_relevance as sr


def _fake_resolve(ip):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))]


class EgressProxyRangeTests(unittest.TestCase):
    def test_hostname_resolving_into_proxy_range_is_allowed(self):
        with mock.patch.object(sr.socket, "getaddrinfo", return_value=_fake_resolve("198.18.0.1")):
            _value, _parts, addresses = sr._resolve_public_source_url(
                "https://en.wikipedia.org/wiki/X"
            )
        self.assertEqual(addresses, [ipaddress.ip_address("198.18.0.1")])

    def test_literal_proxy_range_ip_is_blocked(self):
        with self.assertRaises(ValueError):
            sr._resolve_public_source_url("http://198.18.0.1/")

    def test_other_private_ranges_still_blocked(self):
        for ip in ("127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "172.16.0.1"):
            with self.subTest(ip=ip):
                with mock.patch.object(sr.socket, "getaddrinfo", return_value=_fake_resolve(ip)):
                    with self.assertRaises(ValueError):
                        sr._resolve_public_source_url("https://evil.example/")


if __name__ == "__main__":
    unittest.main()
