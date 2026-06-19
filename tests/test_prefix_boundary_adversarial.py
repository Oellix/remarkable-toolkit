#!/usr/bin/env python3
"""Adversariale Verifikation des P2-Schreib-Confinements — LINSE 'prefix-boundary'.

Bricht das Confinement NICHT durch Mocking-Tricks, sondern ruft die ECHTEN
Funktionen (rmlib.guard_write / rmlib._guard_base / rmlib._norm_cloud_path und
send.upload / send.ensure_dest) mit Angriffs-Strings auf. Jeder Pfad, der
ausserhalb der Basis landet aber NICHT abgelehnt wird, ist ein Leak.

Lauf:
    PYTHONPATH=scripts /Users/alex/Documents/Projects/reMarkable/.venv/bin/python \
        -m unittest tests.test_prefix_boundary_adversarial -v

Es werden KEINE Live-rmapi-Aufrufe gemacht: der Wiring-Test stubbt
rmlib.subprocess.run und prueft, dass nur geguardete Strings an rmapi gingen.
"""
from __future__ import annotations

import os
import tempfile
import types
import unittest

import rmlib
import send

PREFIX = rmlib.RM_ALLOWED_PREFIX_ENV  # "RM_ALLOWED_PREFIX"


class _EnvBase(unittest.TestCase):
    """Stellt RM_ALLOWED_PREFIX nach jedem Test wieder her und kontrolliert es
    explizit (es koennte vom Parent-Prozess geerbt sein)."""

    def setUp(self) -> None:
        self._saved = os.environ.get(PREFIX)

    def tearDown(self) -> None:
        if self._saved is None:
            os.environ.pop(PREFIX, None)
        else:
            os.environ[PREFIX] = self._saved

    def _set(self, value) -> None:
        if value is None:
            os.environ.pop(PREFIX, None)
        else:
            os.environ[PREFIX] = value


# --------------------------------------------------------------------------
# 1) guard_write — Praefix-Grenze & Sentinel bei Basis '/HERMES'
# --------------------------------------------------------------------------
class TestGuardWriteSingleSegmentBase(_EnvBase):
    def setUp(self) -> None:
        super().setUp()
        self._set("/HERMES")

    def test_base_itself_allowed(self):
        self.assertEqual(rmlib.guard_write("/HERMES"), "/HERMES")

    def test_child_allowed(self):
        self.assertEqual(rmlib.guard_write("/HERMES/Sub"), "/HERMES/Sub")

    def test_deep_child_allowed(self):
        self.assertEqual(rmlib.guard_write("/HERMES/a/b/c"), "/HERMES/a/b/c")

    def test_child_with_redundant_slashes_normalized_and_allowed(self):
        # '//HERMES//Sub//' -> '/HERMES/Sub' (kollabiert, dann in-base)
        self.assertEqual(rmlib.guard_write("//HERMES//Sub//"), "/HERMES/Sub")

    def test_trailing_slash_on_base_path_is_base(self):
        # '/HERMES/' normalisiert zu '/HERMES' == base → erlaubt
        self.assertEqual(rmlib.guard_write("/HERMES/"), "/HERMES")

    # --- die KERN-Angriffe der Linse: Substring-/Praefix-Verwechslung -------
    def test_sibling_prefix_HERMESX_denied(self):
        # '/HERMESX' teilt das Praefix '/HERMES' OHNE '/'-Grenze → MUSS denien.
        with self.assertRaises(SystemExit):
            rmlib.guard_write("/HERMESX")

    def test_sibling_prefix_HERMESsuffix_denied(self):
        with self.assertRaises(SystemExit):
            rmlib.guard_write("/HERMESsuffix")

    def test_sibling_prefix_HERMES_dash_denied(self):
        with self.assertRaises(SystemExit):
            rmlib.guard_write("/HERMES-evil")

    def test_case_mismatch_lower_hermes_denied(self):
        # Confinement ist case-sensitive: '/hermes' != base '/HERMES'.
        with self.assertRaises(SystemExit):
            rmlib.guard_write("/hermes")

    def test_case_mismatch_child_denied(self):
        with self.assertRaises(SystemExit):
            rmlib.guard_write("/hermes/Sub")

    def test_root_denied_under_confinement(self):
        with self.assertRaises(SystemExit):
            rmlib.guard_write("/")

    def test_empty_dest_becomes_root_denied(self):
        # '' -> _norm_cloud_path -> '/' -> ausserhalb '/HERMES' → deny.
        with self.assertRaises(SystemExit):
            rmlib.guard_write("")

    def test_unrelated_path_denied(self):
        with self.assertRaises(SystemExit):
            rmlib.guard_write("/Evil")

    def test_parent_of_base_denied(self):
        # Ein anderer Top-Level-Ordner, der NICHT base ist.
        with self.assertRaises(SystemExit):
            rmlib.guard_write("/HER")

    def test_dotdot_traversal_into_base_denied(self):
        # '/HERMES/../X' enthaelt '..' → haerter Reject in _norm_cloud_path.
        with self.assertRaises(SystemExit):
            rmlib.guard_write("/HERMES/../X")

    def test_dotdot_escaping_base_denied(self):
        with self.assertRaises(SystemExit):
            rmlib.guard_write("/HERMES/sub/../../Evil")

    def test_control_char_in_dest_denied(self):
        for s in ("/HERMES/a\nb", "/HERMES/a\tb", "/HERMES/a\x00b", "/HERMES/a\rb"):
            with self.subTest(s=repr(s)):
                with self.assertRaises(SystemExit):
                    rmlib.guard_write(s)


# --------------------------------------------------------------------------
# 2) _guard_base — Sentinel / fail-closed / Normalisierungs-Reject
# --------------------------------------------------------------------------
class TestGuardBaseEnvSemantics(_EnvBase):
    def test_unset_denies(self):
        self._set(None)
        with self.assertRaises(SystemExit):
            rmlib._guard_base()
        # und guard_write erbt das Fail-closed
        with self.assertRaises(SystemExit):
            rmlib.guard_write("/HERMES")

    def test_empty_string_denies(self):
        self._set("")
        with self.assertRaises(SystemExit):
            rmlib._guard_base()

    def test_root_slash_denies(self):
        self._set("/")
        with self.assertRaises(SystemExit):
            rmlib._guard_base()

    def test_multi_slash_normalizes_to_root_denies(self):
        # '///' -> normalisiert '/' → deny (nicht etwa Vollzugriff).
        self._set("///")
        with self.assertRaises(SystemExit):
            rmlib._guard_base()

    def test_dotdot_only_prefix_denies(self):
        # '..' als Prefix → _norm_cloud_path raised (kein '/'-Fallthrough).
        self._set("..")
        with self.assertRaises(SystemExit):
            rmlib._guard_base()

    def test_control_char_in_prefix_denies(self):
        self._set("/HER\nMES")
        with self.assertRaises(SystemExit):
            rmlib._guard_base()

    def test_sentinel_ALL_disables_confinement(self):
        self._set("ALL")
        self.assertIsNone(rmlib._guard_base())
        # guard_write gibt dann den normalisierten dest ungeprueft zurueck.
        self.assertEqual(rmlib.guard_write("/literally/anything"), "/literally/anything")
        self.assertEqual(rmlib.guard_write("/"), "/")
        self.assertEqual(rmlib.guard_write(""), "/")

    def test_sentinel_ALL_still_rejects_dotdot_and_ctrl(self):
        # ALL hebt Confinement auf, aber NICHT die Pfad-Hygiene (.. / Steuerz.).
        self._set("ALL")
        with self.assertRaises(SystemExit):
            rmlib.guard_write("/a/../b")
        with self.assertRaises(SystemExit):
            rmlib.guard_write("/a\nb")

    def test_lowercase_all_is_NOT_sentinel(self):
        # 'all' ist NICHT das Sentinel → wird als Ordner '/all' confined,
        # NICHT als Vollzugriff. Root muss weiterhin denien.
        self._set("all")
        self.assertEqual(rmlib._guard_base(), "/all")
        self.assertEqual(rmlib.guard_write("/all"), "/all")
        self.assertEqual(rmlib.guard_write("/all/x"), "/all/x")
        with self.assertRaises(SystemExit):
            rmlib.guard_write("/")
        with self.assertRaises(SystemExit):
            rmlib.guard_write("/Evil")

    def test_mixedcase_All_is_NOT_sentinel(self):
        self._set("All")
        self.assertEqual(rmlib._guard_base(), "/All")
        with self.assertRaises(SystemExit):
            rmlib.guard_write("/")

    def test_ALL_with_whitespace_is_NOT_sentinel(self):
        # ' ALL ' != 'ALL' → confined als Ordner ' ALL ' (Sentinel exakt).
        self._set(" ALL ")
        base = rmlib._guard_base()
        self.assertNotEqual(base, None)        # KEIN Vollzugriff
        with self.assertRaises(SystemExit):
            rmlib.guard_write("/")


# --------------------------------------------------------------------------
# 3) Multi-segment Basis '/HERMES/Sub' — Tiefen- und Vorfahr-Grenze
# --------------------------------------------------------------------------
class TestGuardWriteMultiSegmentBase(_EnvBase):
    def setUp(self) -> None:
        super().setUp()
        self._set("/HERMES/Sub")

    def test_base_allowed(self):
        self.assertEqual(rmlib.guard_write("/HERMES/Sub"), "/HERMES/Sub")

    def test_child_allowed(self):
        self.assertEqual(rmlib.guard_write("/HERMES/Sub/Deep"), "/HERMES/Sub/Deep")

    def test_deep_child_allowed(self):
        self.assertEqual(rmlib.guard_write("/HERMES/Sub/a/b"), "/HERMES/Sub/a/b")

    def test_trailing_slash_base_value_normalized(self):
        # Basis selbst mit '/' Wert → normalisiert == base.
        self.assertEqual(rmlib.guard_write("/HERMES/Sub/"), "/HERMES/Sub")

    def test_sibling_depth_boundary_SubExtra_denied(self):
        # '/HERMES/SubExtra' teilt 'Sub' als Praefix OHNE '/'-Grenze → deny.
        with self.assertRaises(SystemExit):
            rmlib.guard_write("/HERMES/SubExtra")

    def test_ancestor_HERMES_denied(self):
        # Direkter Vorfahr der Basis ist NICHT beschreibbar.
        with self.assertRaises(SystemExit):
            rmlib.guard_write("/HERMES")

    def test_ancestor_root_denied(self):
        with self.assertRaises(SystemExit):
            rmlib.guard_write("/")

    def test_sibling_subtree_denied(self):
        with self.assertRaises(SystemExit):
            rmlib.guard_write("/HERMES/Other")

    def test_partial_first_segment_denied(self):
        # '/HERMESX/Sub' — erstes Segment ist schon ein Sibling.
        with self.assertRaises(SystemExit):
            rmlib.guard_write("/HERMESX/Sub")


# --------------------------------------------------------------------------
# 4) WIRING: ist der guard-Rueckgabewert WIRKLICH der einzige Cloud-Pfad,
#    den send.py an rmapi gibt?  -> rmlib.subprocess.run stubben, argv fangen.
# --------------------------------------------------------------------------
class TestUploadWiringNoBypass(_EnvBase):
    """Faengt JEDEN rmapi-Subprozess ab (rmlib.subprocess.run) und prueft, dass
    KEIN out-of-base Cloud-String je in einer argv landet. Echte guard_write /
    ensure_dest laufen; nur der exec ist gefaket. bin/rmapi existiert → require()
    passiert."""

    def setUp(self) -> None:
        super().setUp()
        self._orig_run = rmlib.subprocess.run
        self.calls: list[list[str]] = []

        def fake_run(argv, *a, **kw):
            # argv kopieren (Liste der vollen rmapi-Kommandozeile)
            self.calls.append(list(argv))
            return types.SimpleNamespace(returncode=0)

        rmlib.subprocess.run = fake_run
        # lokale Dummy-Quelldatei fuer 'put' (wird nie wirklich hochgeladen).
        # Liegt in einem tempdir (NICHT im Repo) — die Quelldatei muss nur
        # os.path.isfile() bestehen, ihr Pfad ist fuer das Cloud-Confinement
        # irrelevant (local_first wird nie geguarded).
        fd, self._localfile = tempfile.mkstemp(prefix="rm-wiring-", suffix=".txt")
        with os.fdopen(fd, "w") as f:
            f.write("x")

    def tearDown(self) -> None:
        rmlib.subprocess.run = self._orig_run
        try:
            os.remove(self._localfile)
        except OSError:
            pass
        super().tearDown()

    def _cloud_args_seen(self) -> list[str]:
        """Alle Argumente ueber alle rmapi-Aufrufe, die wie Cloud-Pfade aussehen
        (mit '/' beginnen) und NICHT die lokale Quelldatei sind."""
        seen: list[str] = []
        for argv in self.calls:
            # argv[0] = RMAPI binary, argv[1] = verb, Rest = Flags/Pfade
            for tok in argv[2:]:
                if tok == self._localfile:
                    continue
                if isinstance(tok, str) and tok.startswith("/"):
                    seen.append(tok)
        return seen

    def test_out_of_base_dest_raises_and_never_reaches_rmapi(self):
        self._set("/HERMES")
        with self.assertRaises(SystemExit):
            send.upload(self._localfile, "/Evil")
        # Kein Aufruf darf '/Evil' (oder irgendeinen out-of-base Pfad) enthalten.
        for argv in self.calls:
            self.assertNotIn("/Evil", argv)
        self.assertNotIn("/Evil", self._cloud_args_seen())

    def test_sibling_prefix_dest_raises_and_never_reaches_rmapi(self):
        self._set("/HERMES")
        with self.assertRaises(SystemExit):
            send.upload(self._localfile, "/HERMESX")
        for argv in self.calls:
            self.assertNotIn("/HERMESX", argv)

    def test_root_dest_under_confinement_raises_no_put(self):
        self._set("/HERMES")
        with self.assertRaises(SystemExit):
            send.upload(self._localfile, "/")
        # Es darf KEIN 'put' abgesetzt worden sein.
        verbs = [argv[1] for argv in self.calls if len(argv) > 1]
        self.assertNotIn("put", verbs)

    def test_in_base_dest_only_guarded_paths_reach_rmapi(self):
        self._set("/HERMES/Sub")
        rc = send.upload(self._localfile, "/HERMES/Sub/Deep")
        self.assertEqual(rc, 0)
        cloud = self._cloud_args_seen()
        # Jeder Cloud-Pfad, der rmapi erreicht, MUSS at-or-below base sein …
        for p in cloud:
            self.assertTrue(
                p == "/HERMES/Sub" or p.startswith("/HERMES/Sub/"),
                f"out-of-base Pfad an rmapi geleakt: {p!r}")
        # … und der Vorfahr '/HERMES' darf NIE als (un-geguardeter) mkdir kommen
        # (ensure_dest ueberspringt ihn).
        self.assertNotIn("/HERMES", cloud)
        # mindestens das finale put-Ziel muss vorgekommen sein
        self.assertIn("/HERMES/Sub/Deep", cloud)

    def test_ancestor_skip_does_not_emit_parent_mkdir(self):
        # Basis tiefer: '/HERMES/Sub/Inner', Ziel == Basis. ensure_dest darf
        # WEDER '/HERMES' NOCH '/HERMES/Sub' als mkdir an rmapi geben (beide
        # echte Vorfahren der Basis).
        self._set("/HERMES/Sub/Inner")
        rc = send.upload(self._localfile, "/HERMES/Sub/Inner")
        self.assertEqual(rc, 0)
        cloud = self._cloud_args_seen()
        self.assertNotIn("/HERMES", cloud)
        self.assertNotIn("/HERMES/Sub", cloud)
        for p in cloud:
            self.assertTrue(
                p == "/HERMES/Sub/Inner" or p.startswith("/HERMES/Sub/Inner/"),
                f"out-of-base Pfad an rmapi geleakt: {p!r}")

    def test_all_sentinel_passes_paths_through(self):
        # Sanity: unter ALL erreicht der normalisierte Pfad rmapi (kein Confine).
        self._set("ALL")
        rc = send.upload(self._localfile, "/anywhere/x")
        self.assertEqual(rc, 0)
        self.assertIn("/anywhere/x", self._cloud_args_seen())


if __name__ == "__main__":
    unittest.main()
