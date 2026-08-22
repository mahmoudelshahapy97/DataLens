"""Client-address resolution, credential sealing, and CSRF tokens.

Three small modules that each close a specific hole:

* ``net`` -- ``X-Forwarded-For`` was read unconditionally and from the *left*, so one
  header defeated per-IP login throttling.
* ``secrets`` -- warehouse passwords sat in the control plane in plaintext, and once
  decrypted were ordinary strings that any log line could pick up.
* ``csrf`` -- there was no CSRF protection beyond ``SameSite=Lax``.
"""

from __future__ import annotations

import ipaddress

import pytest

from vanna_app.csrf import COOKIE_NAME, SAFE_METHODS, issue, matches, verify
from vanna_app.net import client_ip
from vanna_app.secrets import Cipher, Secret, SecretsUnavailable

PROXIES = (ipaddress.ip_network("10.0.0.0/8"), ipaddress.ip_network("172.16.0.0/12"))


class TestClientAddress:
    def test_header_is_ignored_when_no_proxies_are_configured(self):
        assert client_ip("203.0.113.9", "1.2.3.4", ()) == "203.0.113.9"

    def test_header_is_ignored_from_an_untrusted_peer(self):
        # The published-port case: anybody could set the header directly.
        assert client_ip("198.51.100.7", "1.2.3.4", PROXIES) == "198.51.100.7"

    def test_header_is_read_from_a_trusted_peer(self):
        assert client_ip("10.0.0.5", "203.0.113.9", PROXIES) == "203.0.113.9"

    def test_the_rightmost_untrusted_hop_wins(self):
        """The original took the leftmost entry -- the one the client supplies."""
        chain = "1.2.3.4, 203.0.113.9, 10.0.0.5"
        assert client_ip("10.0.0.5", chain, PROXIES) == "203.0.113.9"

    def test_a_spoofed_prefix_cannot_hide_the_real_address(self):
        chain = "evil-spoof, 203.0.113.9"
        assert client_ip("10.0.0.5", chain, PROXIES) == "203.0.113.9"

    def test_an_unparseable_hop_stops_the_walk(self):
        # Stopping rather than skipping is what keeps a deliberately malformed hop
        # from being a way to hide behind.
        assert client_ip("10.0.0.5", "203.0.113.9, garbage", PROXIES) == "10.0.0.5"

    def test_all_trusted_hops_falls_back_to_the_peer(self):
        assert client_ip("10.0.0.5", "10.0.0.6, 172.16.0.3", PROXIES) == "10.0.0.5"

    def test_ports_and_brackets_are_tolerated(self):
        assert client_ip("10.0.0.5", "203.0.113.9:51234", PROXIES) == "203.0.113.9"
        assert client_ip("10.0.0.5", "[2001:db8::1]", PROXIES) == "2001:db8::1"

    def test_no_peer_yields_no_address(self):
        assert client_ip("", "203.0.113.9", PROXIES) == ""


class TestSecret:
    def test_it_does_not_print_itself(self):
        secret = Secret("postgresql://vanna:hunter2@db/analytics")
        assert str(secret) == "***"
        assert repr(secret) == "Secret('***')"
        assert f"connecting to {secret}" == "connecting to ***"
        assert "hunter2" not in f"{secret!r} {secret}"

    def test_reveal_returns_the_value(self):
        assert Secret("hunter2").reveal() == "hunter2"

    def test_empty_is_falsy_and_prints_empty(self):
        assert not Secret("")
        assert str(Secret("")) == ""

    def test_equality_works_against_plain_strings(self):
        assert Secret("a") == "a"
        assert Secret("a") == Secret("a")
        assert Secret("a") != "b"


class TestCipher:
    def test_round_trip(self):
        cipher = Cipher("k" * 48)
        sealed = cipher.encrypt("postgresql://u:p@h/db")
        assert sealed.startswith("enc:v1:")
        assert "postgresql" not in sealed
        assert cipher.decrypt(sealed) == "postgresql://u:p@h/db"

    def test_sealing_is_idempotent(self):
        cipher = Cipher("k" * 48)
        once = cipher.encrypt("value")
        assert cipher.encrypt(once) == once

    def test_legacy_plaintext_still_reads(self):
        # Rows written before encryption existed must keep working, or an upgrade
        # takes a running deployment offline.
        cipher = Cipher("k" * 48)
        assert cipher.decrypt("postgresql://u:p@h/db") == "postgresql://u:p@h/db"

    def test_a_different_key_cannot_open_it(self):
        sealed = Cipher("k" * 48).encrypt("value")
        with pytest.raises(SecretsUnavailable) as caught:
            Cipher("j" * 48).decrypt(sealed)
        assert "VANNA_SECRET_KEY" in str(caught.value)

    def test_no_key_means_no_encryption_but_still_reads_plaintext(self):
        cipher = Cipher("")
        assert not cipher.enabled
        assert cipher.encrypt("value") == "value"
        assert cipher.decrypt("value") == "value"

    def test_no_key_cannot_open_ciphertext(self):
        sealed = Cipher("k" * 48).encrypt("value")
        with pytest.raises(SecretsUnavailable):
            Cipher("").decrypt(sealed)

    def test_the_same_value_seals_differently_each_time(self):
        # Fernet carries a random IV; identical plaintexts must not be spottable in
        # a table dump.
        cipher = Cipher("k" * 48)
        assert cipher.encrypt("value") != cipher.encrypt("value")

    def test_empty_stays_empty(self):
        cipher = Cipher("k" * 48)
        assert cipher.encrypt("") == ""
        assert cipher.encrypt(None) is None


class TestCsrf:
    def test_a_token_verifies_against_its_own_key(self):
        token = issue("k" * 48)
        assert verify("k" * 48, token)

    def test_a_token_does_not_verify_against_another_key(self):
        # This is what the *signed* half of signed-double-submit buys: an attacker
        # who can write a cookie for our domain still cannot forge a valid value.
        assert not verify("j" * 48, issue("k" * 48))

    def test_a_fabricated_token_is_rejected(self):
        assert not verify("k" * 48, "made.up")
        assert not verify("k" * 48, "nodot")
        assert not verify("k" * 48, "")

    def test_double_submit_needs_both_halves_to_agree(self):
        secret = "k" * 48
        token = issue(secret)
        assert matches(secret, token, token)
        assert not matches(secret, token, issue(secret))
        assert not matches(secret, token, "")
        assert not matches(secret, "", token)

    def test_tokens_are_unique(self):
        secret = "k" * 48
        assert len({issue(secret) for _ in range(50)}) == 50

    def test_read_only_methods_need_no_token(self):
        assert {"GET", "HEAD", "OPTIONS", "TRACE"} == SAFE_METHODS

    def test_the_cookie_name_is_what_the_frontend_reads(self):
        # The shared front-end module reads `vanna_csrf`; a rename on one side only
        # would silently disable the protection rather than break loudly.
        assert COOKIE_NAME == "vanna_csrf"
