#!/usr/bin/env python3
"""Sicherheits-Tests für den Schreib-Guard (P2).

Stdlib-unittest, KEIN pytest. Lauf vom Repo-Wurzelverzeichnis:

    PYTHONPATH=scripts .venv/bin/python -m unittest discover -s tests -t .

Diese Tests sind rein (kein Netz, kein rmapi). Für ``rmapi_write`` wird
``rmlib.subprocess.run`` gepatcht, sodass kein echter Subprozess startet und wir
genau prüfen können, WELCHE Argumente an rmapi gegangen WÄREN — und dass der
Guard BEVOR irgendein Subprozess startet abbricht.

Adversariales Ziel: Jeder Escape-Versuch (Traversal, Substring, Look-alike,
fail-open) MUSS fehlschlagen (SystemExit), niemals still durchrutschen.
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

import rmlib


# Eindeutiges Unicode-Look-alike: GREEK CAPITAL LETTER EPSILON (U+0395) statt
# ASCII 'E' in "HERMES". Sieht identisch aus, ist aber ein anderer Codepoint —
# darf NICHT als "/HERMES" durchgehen.
LOOKALIKE = "/HΕRMES/x"


def _env(**kw):
    """Kontextmanager: os.environ exakt auf die übergebenen Keys setzen.

    clear=True entfernt insbesondere ein evtl. von außen gesetztes
    RM_ALLOWED_PREFIX, damit 'unset' deterministisch getestet werden kann.
    Wer RM_ALLOWED_PREFIX setzen will, übergibt es explizit.
    """
    return mock.patch.dict(os.environ, kw, clear=True)


class NormCloudPathTests(unittest.TestCase):
    """_norm_cloud_path: value-authoritativ, raise statt still droppen."""

    def test_collapses_leading_and_double_slashes(self):
        self.assertEqual(rmlib._norm_cloud_path("//HERMES//a///b"), "/HERMES/a/b")

    def test_collapses_trailing_slash(self):
        self.assertEqual(rmlib._norm_cloud_path("/HERMES/a/"), "/HERMES/a")

    def test_empty_and_root_become_root(self):
        self.assertEqual(rmlib._norm_cloud_path(""), "/")
        self.assertEqual(rmlib._norm_cloud_path("/"), "/")
        self.assertEqual(rmlib._norm_cloud_path("///"), "/")

    def test_dotdot_segment_raises(self):
        with self.assertRaises(SystemExit):
            rmlib._norm_cloud_path("/HERMES/../Secret")

    def test_dotdot_leading_raises(self):
        with self.assertRaises(SystemExit):
            rmlib._norm_cloud_path("../Secret")

    def test_newline_raises(self):
        with self.assertRaises(SystemExit):
            rmlib._norm_cloud_path("/HERMES/a\n/Secret")

    def test_carriage_return_raises(self):
        with self.assertRaises(SystemExit):
            rmlib._norm_cloud_path("/HERMES\r/x")

    def test_nul_byte_raises(self):
        with self.assertRaises(SystemExit):
            rmlib._norm_cloud_path("/HERMES\x00/x")

    def test_tab_raises(self):
        with self.assertRaises(SystemExit):
            rmlib._norm_cloud_path("/HERMES\t/x")

    def test_single_dot_segment_is_kept_literal(self):
        # '.' ist kein '..' und kein Steuerzeichen → bleibt ein normales Segment
        # (keine CWD-Auflösung). Wichtig: es wird NICHT als Traversal behandelt.
        self.assertEqual(rmlib._norm_cloud_path("/HERMES/./a"), "/HERMES/./a")

    # --- Hardening aus adversarialem Verify (2026-06-18) -------------------
    def test_c1_control_nel_raises(self):
        # C1-Control U+0085 (NEL) — war vor dem Verify un-rejected (nur C0+DEL).
        with self.assertRaises(SystemExit):
            rmlib._norm_cloud_path("/HERMES/a\x85b")

    def test_unicode_line_separator_raises(self):
        # U+2028 LINE SEPARATOR — Log-/Argument-Hygiene.
        with self.assertRaises(SystemExit):
            rmlib._norm_cloud_path("/HERMES/a\u2028b")

    def test_backslash_alt_separator_raises(self):
        # '/HERMES/..\\..\\evil' schlüpft sonst als EIN Segment am '..'-Check
        # vorbei — Backslash wird als Alt-Separator abgelehnt (Defense-in-Depth).
        with self.assertRaises(SystemExit):
            rmlib._norm_cloud_path("/HERMES/..\\..\\evil")

    def test_fullwidth_solidus_alt_separator_raises(self):
        # U+FF0F ／ (Fullwidth Solidus) — Slash-Confusable, abgelehnt.
        with self.assertRaises(SystemExit):
            rmlib._norm_cloud_path("/HERMES/..／evil")

    def test_dotdot_substring_in_name_allowed(self):
        # '..' als Teil eines Namens (kein eigenes Segment, kein Alt-Sep) bleibt
        # erlaubt — wir lehnen nur das EXAKTE '..'-Segment + Separatoren ab,
        # nicht jeden Namen, der '..' enthält (z. B. 'Notes..final').
        self.assertEqual(rmlib._norm_cloud_path("/HERMES/Notes..final"),
                         "/HERMES/Notes..final")


class GuardBaseTests(unittest.TestCase):
    """_guard_base: fail-closed, ALL-Sentinel, '/'-Deny — alles zur Aufrufzeit."""

    def test_unset_denies(self):
        with _env():  # RM_ALLOWED_PREFIX nicht gesetzt
            with self.assertRaises(SystemExit):
                rmlib._guard_base()

    def test_empty_denies(self):
        with _env(RM_ALLOWED_PREFIX=""):
            with self.assertRaises(SystemExit):
                rmlib._guard_base()

    def test_all_sentinel_returns_none(self):
        with _env(RM_ALLOWED_PREFIX="ALL"):
            self.assertIsNone(rmlib._guard_base())

    def test_all_is_case_sensitive(self):
        # "all"/"All" sind NICHT das Sentinel → werden als Ordnername normalisiert
        # (und damit zur Confinement-Basis '/all'), heben Confinement NICHT auf.
        with _env(RM_ALLOWED_PREFIX="all"):
            self.assertEqual(rmlib._guard_base(), "/all")

    def test_slash_only_prefix_denies(self):
        with _env(RM_ALLOWED_PREFIX="/"):
            with self.assertRaises(SystemExit):
                rmlib._guard_base()

    def test_prefix_normalizing_to_root_denies(self):
        # Mehrfach-Slash, der zu '/' kollabiert → Deny (nicht "alles erlauben").
        with _env(RM_ALLOWED_PREFIX="///"):
            with self.assertRaises(SystemExit):
                rmlib._guard_base()

    def test_normal_prefix_returns_normalized_base(self):
        with _env(RM_ALLOWED_PREFIX="HERMES"):
            self.assertEqual(rmlib._guard_base(), "/HERMES")
        with _env(RM_ALLOWED_PREFIX="//HERMES//"):
            self.assertEqual(rmlib._guard_base(), "/HERMES")


class GuardWriteConfinedTests(unittest.TestCase):
    """guard_write unter RM_ALLOWED_PREFIX=/HERMES: erlaubte vs. abgelehnte Ziele."""

    def setUp(self):
        self._patch = _env(RM_ALLOWED_PREFIX="/HERMES")
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    # --- erlaubt -----------------------------------------------------------
    def test_base_itself_allowed(self):
        self.assertEqual(rmlib.guard_write("/HERMES"), "/HERMES")

    def test_child_allowed(self):
        self.assertEqual(rmlib.guard_write("/HERMES/a"), "/HERMES/a")

    def test_deep_child_allowed(self):
        self.assertEqual(rmlib.guard_write("/HERMES/a/b/c"), "/HERMES/a/b/c")

    def test_double_slash_child_collapses_and_allowed(self):
        # Slash-Collapse: '//HERMES//a' ist in-base und wird normalisiert erlaubt.
        self.assertEqual(rmlib.guard_write("//HERMES//a"), "/HERMES/a")

    def test_trailing_slash_base_allowed(self):
        self.assertEqual(rmlib.guard_write("/HERMES/"), "/HERMES")

    # --- abgelehnt (die Escape-Matrix) ------------------------------------
    def test_traversal_escape_denied(self):
        # '/HERMES/../Secret' → '..' wird in _norm_cloud_path hart abgelehnt.
        with self.assertRaises(SystemExit):
            rmlib.guard_write("/HERMES/../Secret")

    def test_substring_sibling_denied(self):
        # '/HERMESX' darf NICHT als unter '/HERMES' liegend gelten (kein '/'-Grenz).
        with self.assertRaises(SystemExit):
            rmlib.guard_write("/HERMESX/x")

    def test_case_variant_denied(self):
        # Pfade sind case-sensitive: '/hermes' ≠ '/HERMES'.
        with self.assertRaises(SystemExit):
            rmlib.guard_write("/hermes/x")

    def test_leading_space_segment_denied(self):
        # '/ HERMES/x' → Segment ' HERMES' ≠ 'HERMES' → außerhalb.
        with self.assertRaises(SystemExit):
            rmlib.guard_write("/ HERMES/x")

    def test_unicode_lookalike_denied(self):
        # Look-alike ist kein Steuerzeichen/kein '..' → _norm lässt ihn passieren,
        # aber die Byte-Gleichheit im Guard lehnt ihn ab (kein Confusable-Bypass).
        with self.assertRaises(SystemExit):
            rmlib.guard_write(LOOKALIKE)

    def test_unrelated_path_denied(self):
        with self.assertRaises(SystemExit):
            rmlib.guard_write("/Secret")

    def test_root_denied_under_confinement(self):
        # Wurzel-Upload-Bypass: guard_write('/') MUSS unter Confinement raisen.
        with self.assertRaises(SystemExit):
            rmlib.guard_write("/")

    def test_empty_dest_denied_under_confinement(self):
        # leeres dest normalisiert zu '/' → ebenfalls Deny.
        with self.assertRaises(SystemExit):
            rmlib.guard_write("")


class GuardWriteFailClosedTests(unittest.TestCase):
    """guard_write erbt fail-closed/ALL-Verhalten von _guard_base."""

    def test_unset_denies_even_for_innocuous_path(self):
        with _env():
            with self.assertRaises(SystemExit):
                rmlib.guard_write("/HERMES/a")

    def test_empty_prefix_denies(self):
        with _env(RM_ALLOWED_PREFIX=""):
            with self.assertRaises(SystemExit):
                rmlib.guard_write("/HERMES/a")

    def test_all_allows_any_normalized_path(self):
        with _env(RM_ALLOWED_PREFIX="ALL"):
            self.assertEqual(rmlib.guard_write("/Secret/x"), "/Secret/x")
            self.assertEqual(rmlib.guard_write("/"), "/")
            self.assertEqual(rmlib.guard_write("//A//B"), "/A/B")

    def test_all_still_rejects_traversal(self):
        # Selbst mit ALL bleibt '..' verboten (value-authoritativ, kein Traversal).
        with _env(RM_ALLOWED_PREFIX="ALL"):
            with self.assertRaises(SystemExit):
                rmlib.guard_write("/A/../B")


class GuardWriteMultiSegmentBaseTests(unittest.TestCase):
    """base mehrsegmentig '/HERMES/Sub': nur dieser Teilbaum ist erlaubt."""

    def setUp(self):
        self._patch = _env(RM_ALLOWED_PREFIX="/HERMES/Sub")
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def test_base_itself_allowed(self):
        self.assertEqual(rmlib.guard_write("/HERMES/Sub"), "/HERMES/Sub")

    def test_child_allowed(self):
        self.assertEqual(rmlib.guard_write("/HERMES/Sub/x"), "/HERMES/Sub/x")

    def test_parent_of_base_denied(self):
        # '/HERMES' ist ECHTER VORFAHR der Basis → kein Schreibziel.
        with self.assertRaises(SystemExit):
            rmlib.guard_write("/HERMES")

    def test_sibling_under_parent_denied(self):
        with self.assertRaises(SystemExit):
            rmlib.guard_write("/HERMES/Other")


class RmapiWriteChokepointTests(unittest.TestCase):
    """rmapi_write: einziger Mutations-Chokepoint, guarded VOR jedem Subprozess.

    require() wird neutralisiert (kein echtes Binary nötig), subprocess.run wird
    gepatcht (kein echter Aufruf). Wir prüfen das tatsächlich gebaute argv.
    """

    def setUp(self):
        # require() prüft die rmapi-Binary — im Test irrelevant, ausschalten.
        self._req = mock.patch.object(rmlib, "require", lambda *a, **k: None)
        self._req.start()
        # subprocess.run abfangen; rc=0 zurückgeben.
        self._run = mock.patch.object(rmlib.subprocess, "run").start()
        self._run.return_value = mock.Mock(returncode=0)

    def tearDown(self):
        mock.patch.stopall()

    def _argv(self):
        """Das an subprocess.run übergebene argv (erstes Positionsargument)."""
        self.assertTrue(self._run.called, "subprocess.run wurde nicht aufgerufen")
        return self._run.call_args.args[0]

    def test_put_guards_only_cloud_target_not_local(self):
        # Lokale Datei mit '..' im PFAD muss UNGEGUARDED durchgehen (es ist eine
        # echte lokale Quelldatei), nur das Cloud-Ziel '/HERMES/x' wird geprüft.
        with _env(RM_ALLOWED_PREFIX="/HERMES"):
            rc = rmlib.rmapi_write("put",
                                   local_first="../local/datei.pdf",
                                   cloud_paths=["/HERMES/x"])
        self.assertEqual(rc, 0)
        argv = self._argv()
        self.assertEqual(argv[:2], [rmlib.RMAPI, "put"])
        # local_first roh & vor dem Cloud-Ziel, Cloud-Ziel geguarded/normalisiert.
        self.assertEqual(argv[2], "../local/datei.pdf")
        self.assertEqual(argv[3], "/HERMES/x")

    def test_put_cloud_target_out_of_prefix_raises_before_run(self):
        with _env(RM_ALLOWED_PREFIX="/HERMES"):
            with self.assertRaises(SystemExit):
                rmlib.rmapi_write("put",
                                  local_first="/tmp/datei.pdf",
                                  cloud_paths=["/Secret"])
        self.assertFalse(self._run.called,
                         "Subprozess darf bei Guard-Verstoß NICHT starten")

    def test_put_local_with_dotdot_does_not_trigger_guard_on_local(self):
        # Beweist explizit: der '..'-Schutz greift NUR für Cloud-Pfade. Eine
        # lokale '..'-Quelle bei sauberem Cloud-Ziel führt NICHT zu SystemExit.
        with _env(RM_ALLOWED_PREFIX="/HERMES"):
            rc = rmlib.rmapi_write("put",
                                   local_first="../../weit/weg.pdf",
                                   cloud_paths=["/HERMES/ok"])
        self.assertEqual(rc, 0)
        self.assertEqual(self._argv()[2], "../../weit/weg.pdf")

    def test_mv_guards_both_cloud_paths(self):
        with _env(RM_ALLOWED_PREFIX="/HERMES"):
            rc = rmlib.rmapi_write("mv",
                                   cloud_paths=["/HERMES/a", "/HERMES/b"])
        self.assertEqual(rc, 0)
        argv = self._argv()
        self.assertEqual(argv, [rmlib.RMAPI, "mv", "/HERMES/a", "/HERMES/b"])

    def test_mv_denies_if_either_path_out_of_prefix(self):
        # Quelle ok, Ziel außerhalb → raise, kein Subprozess.
        with _env(RM_ALLOWED_PREFIX="/HERMES"):
            with self.assertRaises(SystemExit):
                rmlib.rmapi_write("mv",
                                  cloud_paths=["/HERMES/a", "/Secret/b"])
        self.assertFalse(self._run.called)
        # Und andersherum: Quelle außerhalb, Ziel ok.
        with _env(RM_ALLOWED_PREFIX="/HERMES"):
            with self.assertRaises(SystemExit):
                rmlib.rmapi_write("mv",
                                  cloud_paths=["/Secret/a", "/HERMES/b"])
        self.assertFalse(self._run.called)

    def test_mkdir_guarded(self):
        with _env(RM_ALLOWED_PREFIX="/HERMES"):
            rc = rmlib.rmapi_write("mkdir", cloud_paths=["/HERMES/Neu"])
        self.assertEqual(rc, 0)
        self.assertEqual(self._argv(), [rmlib.RMAPI, "mkdir", "/HERMES/Neu"])

    def test_rm_guarded_denies_out_of_prefix(self):
        with _env(RM_ALLOWED_PREFIX="/HERMES"):
            with self.assertRaises(SystemExit):
                rmlib.rmapi_write("rm", cloud_paths=["/Secret"])
        self.assertFalse(self._run.called)

    def test_unset_prefix_blocks_all_mutations(self):
        # Fail-closed end-to-end: ohne Prefix raised schon das erste Cloud-Arg.
        with _env():
            with self.assertRaises(SystemExit):
                rmlib.rmapi_write("put", local_first="/tmp/x.pdf",
                                  cloud_paths=["/HERMES/x"])
        self.assertFalse(self._run.called)

    def test_all_sentinel_allows_mutation_anywhere(self):
        with _env(RM_ALLOWED_PREFIX="ALL"):
            rc = rmlib.rmapi_write("put", local_first="/tmp/x.pdf",
                                   cloud_paths=["/Beliebig/Ort"])
        self.assertEqual(rc, 0)
        self.assertEqual(self._argv(),
                         [rmlib.RMAPI, "put", "/tmp/x.pdf", "/Beliebig/Ort"])

    def test_put_root_upload_under_all_has_no_remote_arg_path(self):
        # Bei ALL + Cloud-Ziel '/' soll guard_write('/') == '/' sein; rmapi_write
        # selbst hängt es als Argument an (Wurzel-Sonderfall liegt im upload()).
        with _env(RM_ALLOWED_PREFIX="ALL"):
            self.assertEqual(rmlib.guard_write("/"), "/")


class EnsureDestAncestorSkipTests(unittest.TestCase):
    """send.ensure_dest: erzeugt nur AT-OR-BELOW base, überspringt echte Vorfahren.

    Wir patchen rmlib.rmapi_write (an es delegiert ensure_dest) und prüfen, WELCHE
    mkdir-Pfade angefordert werden — ohne echten rmapi-Aufruf.
    """

    def setUp(self):
        import send  # erst hier, damit PYTHONPATH=scripts gesetzt ist
        self.send = send
        self._calls = []

        def _fake_write(verb, cloud_paths=None, local_first=None, extra=None):
            # Den Guard real durchlaufen lassen (das ist Teil des Vertrags),
            # nur den Subprozess unterdrücken.
            guarded = [rmlib.guard_write(p) for p in (cloud_paths or [])]
            self._calls.append((verb, guarded))
            return 0

        self._patch = mock.patch.object(rmlib, "rmapi_write", _fake_write)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def test_single_segment_base_creates_base_and_child(self):
        with _env(RM_ALLOWED_PREFIX="/HERMES"):
            self.send.ensure_dest("/HERMES/Reports")
        mkdirs = [paths for verb, paths in self._calls if verb == "mkdir"]
        self.assertEqual(mkdirs, [["/HERMES"], ["/HERMES/Reports"]])

    def test_multi_segment_base_skips_true_ancestor(self):
        # base '/HERMES/Sub': der Vorfahr '/HERMES' wird ÜBERSPRUNGEN (nicht
        # erzeugt, kein raise), erst ab '/HERMES/Sub' wird angelegt.
        with _env(RM_ALLOWED_PREFIX="/HERMES/Sub"):
            self.send.ensure_dest("/HERMES/Sub/Reports")
        mkdirs = [paths for verb, paths in self._calls if verb == "mkdir"]
        self.assertEqual(mkdirs, [["/HERMES/Sub"], ["/HERMES/Sub/Reports"]])

    def test_dest_outside_base_raises_via_guard(self):
        # ensure_dest auf einen Pfad außerhalb der Basis muss am Guard scheitern.
        with _env(RM_ALLOWED_PREFIX="/HERMES"):
            with self.assertRaises(SystemExit):
                self.send.ensure_dest("/Secret/x")

    def test_all_sentinel_creates_every_segment(self):
        with _env(RM_ALLOWED_PREFIX="ALL"):
            self.send.ensure_dest("/A/B/C")
        mkdirs = [paths for verb, paths in self._calls if verb == "mkdir"]
        self.assertEqual(mkdirs, [["/A"], ["/A/B"], ["/A/B/C"]])


class UploadGuardTests(unittest.TestCase):
    """send.upload: Cloud-Ziel IMMER geguarded (auch '/'); lokale Datei roh."""

    def setUp(self):
        import send
        self.send = send
        self._calls = []

        def _fake_write(verb, cloud_paths=None, local_first=None, extra=None):
            guarded = [rmlib.guard_write(p) for p in (cloud_paths or [])]
            self._calls.append({"verb": verb, "local": local_first,
                                "cloud": guarded})
            return 0

        self._patch = mock.patch.object(rmlib, "rmapi_write", _fake_write)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def test_root_upload_denied_under_confinement(self):
        # dest '/' unter Confinement → guard_write('/') raised, kein put.
        with _env(RM_ALLOWED_PREFIX="/HERMES"):
            with self.assertRaises(SystemExit):
                self.send.upload("/tmp/x.pdf", "/")
        self.assertEqual(self._calls, [])

    def test_root_upload_allowed_under_all(self):
        with _env(RM_ALLOWED_PREFIX="ALL"):
            rc = self.send.upload("/tmp/x.pdf", "/")
        self.assertEqual(rc, 0)
        put = [c for c in self._calls if c["verb"] == "put"]
        self.assertEqual(len(put), 1)
        self.assertEqual(put[0]["local"], "/tmp/x.pdf")
        self.assertEqual(put[0]["cloud"], [])  # Wurzel: kein remote-Argument

    def test_folder_upload_guards_target(self):
        with _env(RM_ALLOWED_PREFIX="/HERMES"):
            rc = self.send.upload("/tmp/x.pdf", "/HERMES/Reports")
        self.assertEqual(rc, 0)
        put = [c for c in self._calls if c["verb"] == "put"]
        self.assertEqual(put[0]["local"], "/tmp/x.pdf")
        self.assertEqual(put[0]["cloud"], ["/HERMES/Reports"])

    def test_folder_upload_outside_prefix_denied(self):
        with _env(RM_ALLOWED_PREFIX="/HERMES"):
            with self.assertRaises(SystemExit):
                self.send.upload("/tmp/x.pdf", "/Secret")
        self.assertEqual([c for c in self._calls if c["verb"] == "put"], [])


class SendMainDenialTests(unittest.TestCase):
    """End-to-End über send.main(): ein out-of-prefix-Ziel wird abgelehnt, OHNE
    dass je ein rmapi-Subprozess startet — und im JSON-Modus kommt ein sauberes
    Fehlerobjekt (kein Traceback). Beweist die volle Kette CLI→guard, ohne
    Cloud-Mutation (subprocess.run gepatcht)."""

    def setUp(self):
        import send
        self.send = send
        # Realer Mini-PDF lokal, damit send den Passthrough-Pfad nimmt.
        import tempfile
        self._tmp = tempfile.mkdtemp(prefix="rm-test-send-")
        self._pdf = os.path.join(self._tmp, "x.pdf")
        with open(self._pdf, "wb") as f:
            f.write(b"%PDF-1.4\n%%EOF\n")
        self._run = mock.patch.object(rmlib.subprocess, "run").start()
        self._run.return_value = mock.Mock(returncode=0)
        mock.patch.object(rmlib, "require", lambda *a, **k: None).start()

    def tearDown(self):
        mock.patch.stopall()
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_main_denies_out_of_prefix_json_clean_error(self):
        with _env(RM_ALLOWED_PREFIX="/HERMES"):
            rc = self.send.main([self._pdf, "--dest", "/Secret", "--json"])
        self.assertNotEqual(rc, 0)
        self.assertFalse(self._run.called,
                         "rmapi darf bei Guard-Verstoß nicht laufen")

    def test_main_denies_default_root_under_confinement(self):
        # Ohne --dest landet der Default '/' → unter Confinement abgelehnt.
        with _env(RM_ALLOWED_PREFIX="/HERMES"):
            rc = self.send.main([self._pdf, "--json"])
        self.assertNotEqual(rc, 0)
        self.assertFalse(self._run.called)

    def test_main_denies_when_prefix_unset(self):
        # Fail-closed über die ganze CLI: ohne RM_ALLOWED_PREFIX kein Upload.
        with _env():
            rc = self.send.main([self._pdf, "--dest", "/HERMES/x", "--json"])
        self.assertNotEqual(rc, 0)
        self.assertFalse(self._run.called)

    def test_main_allows_in_prefix(self):
        # Positivfall: in-prefix-Ziel → put läuft (gepatcht), rc 0.
        with _env(RM_ALLOWED_PREFIX="/HERMES"):
            rc = self.send.main([self._pdf, "--dest", "/HERMES/Reports", "--json"])
        self.assertEqual(rc, 0)
        self.assertTrue(self._run.called)
        # Letzter Aufruf = put mit geguardetem Cloud-Ziel als letztem Argument.
        put_argv = self._run.call_args.args[0]
        self.assertEqual(put_argv[0:2], [rmlib.RMAPI, "put"])
        self.assertEqual(put_argv[-1], "/HERMES/Reports")


class RmEnvTokenOverrideTests(unittest.TestCase):
    """rm_env: externes RMAPI_CONFIG wird respektiert (Per-Agent-Token)."""

    def test_external_rmapi_config_respected(self):
        with _env(RMAPI_CONFIG="/agents/tom/.rmapi.conf"):
            self.assertEqual(rmlib.rm_env()["RMAPI_CONFIG"],
                             "/agents/tom/.rmapi.conf")

    def test_falls_back_to_repo_config_when_unset(self):
        with _env():  # kein RMAPI_CONFIG gesetzt
            self.assertEqual(rmlib.rm_env()["RMAPI_CONFIG"], rmlib.RMAPI_CONFIG)


if __name__ == "__main__":
    unittest.main(verbosity=2)
