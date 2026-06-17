#!/usr/bin/env python3
"""Adversariale Verifikation P2 Schreib-Confinement — LINSE 'traversal'.

Ziel: guard_write / _norm_cloud_path BRECHEN. Ein String, der ZURUECKKEHRT
(accepted) UND ausserhalb von base normalisiert, ist ein LEAK. Ein raise
SystemExit ist KORREKTES Verhalten (Denial), kein Leak.

Lauf:
  PYTHONPATH=scripts .venv/bin/python -m unittest test_guard_traversal_adv -v
"""
from __future__ import annotations

import os
import unittest

import rmlib

PREFIX = rmlib.RM_ALLOWED_PREFIX_ENV  # "RM_ALLOWED_PREFIX"


class _Base(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get(PREFIX)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop(PREFIX, None)
        else:
            os.environ[PREFIX] = self._saved

    def set_prefix(self, val):
        if val is None:
            os.environ.pop(PREFIX, None)
        else:
            os.environ[PREFIX] = val


class NormIdempotent(_Base):
    """C1-Kern: norm(norm(x)) == norm(x). Nur wenn das gilt, ist es harmlos,
    dass upload() das bereits genormte norm_dest (statt guard_write-Rueckgabe)
    herumreicht — guard_write(norm_dest) == norm_dest."""

    SAFE = ["/", "/A", "/A/B", "//A//B//", "/A/B/", "A/B", "/HERMES",
            "/HERMES/x/y", "/HERMES/./x", "///A///", "/A/ /B", "/A.B/C-D (1)"]

    def test_idempotent(self):
        for s in self.SAFE:
            n1 = rmlib._norm_cloud_path(s)
            n2 = rmlib._norm_cloud_path(n1)
            self.assertEqual(n1, n2, f"norm nicht idempotent fuer {s!r}: {n1!r} -> {n2!r}")

    def test_guard_returns_exactly_norm(self):
        """Unter ALL muss guard_write den genormten Pfad UNVERAENDERT liefern —
        d. h. der Rueckgabewert == _norm_cloud_path(dest). Das beweist, dass
        upload()'s norm_dest deckungsgleich mit dem geguardeten Wert ist."""
        self.set_prefix(rmlib._GUARD_SENTINEL_ALL)
        for s in self.SAFE:
            self.assertEqual(rmlib.guard_write(s), rmlib._norm_cloud_path(s),
                             f"guard_write != norm fuer {s!r}")


class TraversalDenied(_Base):
    """Jeder '..'-Tragende String MUSS raisen — niemals zurueckkehren."""

    ATTACKS = [
        "..", "/..", "../", "/../", "../../etc",
        "/HERMES/../etc", "/HERMES/../../x", "/HERMES/a/../../x",
        "HERMES/../../x", "/HERMES/a/b/../../../x",
        "/HERMES/sub/..", "/HERMES/..%2f", "..\\..",   # %2f literal, \\ literal -> '..' segment bleibt
        "/HERMES//../x", "/HERMES/./../x", "/a/b/c/../../../../x",
        "/HERMES/ .. ", "/HERMES/...",  # ' .. ' (mit spaces) und '...' sind KEINE '..'-Segmente
    ]

    def test_dotdot_raises_under_confinement(self):
        self.set_prefix("/HERMES")
        for s in self.ATTACKS:
            segs = [x for x in s.split("/") if x != ""]
            if ".." in segs:
                with self.assertRaises(SystemExit, msg=f"'..'-Pfad {s!r} nicht abgelehnt"):
                    rmlib.guard_write(s)
            else:
                # Kein echtes '..'-Segment -> Guard entscheidet per Prefix.
                # Wir verlangen nur: falls es ZURUECKKEHRT, dann in-base.
                try:
                    r = rmlib.guard_write(s)
                except SystemExit:
                    continue
                self.assertTrue(r == "/HERMES" or r.startswith("/HERMES/"),
                                f"{s!r} kehrte zurueck OHNE '..', aber out-of-base: {r!r}")

    def test_dotdot_raises_even_under_ALL(self):
        """_norm_cloud_path laeuft VOR der Base-Pruefung — also muss '..' auch
        bei Sentinel ALL raisen (sonst koennte ALL '..' an rmapi durchreichen)."""
        self.set_prefix(rmlib._GUARD_SENTINEL_ALL)
        for s in ["/HERMES/../x", "/a/../../x", "/.."]:
            with self.assertRaises(SystemExit, msg=f"'..' unter ALL nicht abgelehnt: {s!r}"):
                rmlib.guard_write(s)


class SubstringSibling(_Base):
    """Klassischer Prefix-Substring-Bypass: base '/HERMES' darf '/HERMESX',
    '/HERMES_evil', '/HERMES-2' NICHT akzeptieren (fehlende '/'-Grenze)."""

    def test_sibling_denied(self):
        self.set_prefix("/HERMES")
        for s in ["/HERMESX", "/HERMES_evil", "/HERMES-2", "/HERMES.bak",
                  "/HERMESX/sub", "/HERMESsub/x"]:
            with self.assertRaises(SystemExit, msg=f"Sibling {s!r} faelschlich akzeptiert"):
                rmlib.guard_write(s)

    def test_exact_and_below_allowed(self):
        self.set_prefix("/HERMES")
        for s, exp in [("/HERMES", "/HERMES"), ("/HERMES/", "/HERMES"),
                       ("/HERMES/x", "/HERMES/x"), ("//HERMES//x//", "/HERMES/x"),
                       ("/HERMES/./x", "/HERMES/./x"), ("/HERMES/a/b/c", "/HERMES/a/b/c")]:
            self.assertEqual(rmlib.guard_write(s), exp, f"in-base {s!r} falsch/abgelehnt")


class SlashEmptyNorm(_Base):
    """Mehrfache/fuehrende/abschliessende Slashes + leere Segmente: muessen
    sauber kollabieren, niemals out-of-base landen."""

    def test_collapse(self):
        cases = [("", "/"), ("/", "/"), ("//", "/"), ("///", "/"),
                 ("/A//B", "/A/B"), ("//A//B//", "/A/B"), ("/A/B/", "/A/B"),
                 ("A//B", "/A/B"), ("////A", "/A")]
        for s, exp in cases:
            self.assertEqual(rmlib._norm_cloud_path(s), exp, f"norm({s!r}) != {exp!r}")

    def test_root_denied_under_confinement(self):
        """Alles, was zu '/' normalisiert, ist unter Confinement out-of-base
        (base != '/') und MUSS raisen — kein Root-Schlupf."""
        self.set_prefix("/HERMES")
        for s in ["", "/", "//", "///", "////"]:
            with self.assertRaises(SystemExit, msg=f"Root {s!r} unter Confinement akzeptiert"):
                rmlib.guard_write(s)


class ControlChars(_Base):
    """Steuerzeichen/Newlines/NUL/Tab: in JEDEM Pfad-Argument raise."""

    def test_control_raises(self):
        self.set_prefix("/HERMES")
        attacks = ["/HERMES/x\x00", "/HERMES/\n/x", "/HERMES/a\tb",
                   "/HERMES/x\r", "/HERMES/\x1f", "/HERMES/\x7f",
                   "/HERMES/x\x00/../../etc", "\n/HERMES", "/HERMES\nfoo"]
        for s in attacks:
            with self.assertRaises(SystemExit, msg=f"Control-Char {s!r} nicht abgelehnt"):
                rmlib.guard_write(s)

    def test_control_raises_under_ALL(self):
        self.set_prefix(rmlib._GUARD_SENTINEL_ALL)
        for s in ["/x\x00", "/\n", "/a\tb"]:
            with self.assertRaises(SystemExit, msg=f"Control unter ALL nicht abgelehnt: {s!r}"):
                rmlib.guard_write(s)


class SingleDotSegment(_Base):
    """Einzelner '.' ist ein LITERAL-Segment (kein normpath). './x' -> Segment
    '.' bleibt erhalten. Verhalten dokumentieren: in-base akzeptiert, sonst
    fail-closed (Denial, kein Leak)."""

    def test_dot_in_base_kept_literal(self):
        self.set_prefix("/HERMES")
        # '/HERMES/./x' -> '.' bleibt Segment -> '/HERMES/./x' (in-base, akzeptiert)
        self.assertEqual(rmlib.guard_write("/HERMES/./x"), "/HERMES/./x")

    def test_leading_dot_outside_base_denied(self):
        self.set_prefix("/HERMES")
        # '/./HERMES' -> '/./HERMES' (Segment '.' vor HERMES) -> NICHT unter /HERMES -> Denial
        with self.assertRaises(SystemExit):
            rmlib.guard_write("/./HERMES")


class SentinelAndConfig(_Base):
    """Fail-closed-Konfiguration: unset/""/'/' -> DENY; nur exakt 'ALL' hebt auf."""

    def test_unset_denies(self):
        self.set_prefix(None)
        with self.assertRaises(SystemExit):
            rmlib.guard_write("/HERMES/x")

    def test_empty_denies(self):
        self.set_prefix("")
        with self.assertRaises(SystemExit):
            rmlib.guard_write("/HERMES/x")

    def test_prefix_root_denies(self):
        for p in ["/", "//", "///", ""]:
            self.set_prefix(p if p else "")  # "" handled by test_empty too
            if p == "":
                continue
            with self.assertRaises(SystemExit, msg=f"Prefix {p!r} (= root) nicht abgelehnt"):
                rmlib.guard_write("/HERMES/x")

    def test_prefix_with_dotdot_denies(self):
        """Prefix selbst mit '..' -> _norm_cloud_path im _guard_base raised."""
        self.set_prefix("/HERMES/../etc")
        with self.assertRaises(SystemExit):
            rmlib.guard_write("/HERMES/../etc/x")

    def test_all_sentinel_case_sensitive(self):
        """Nur exakt 'ALL' hebt auf; 'all'/'All'/' ALL ' sind normale Prefixe."""
        self.set_prefix("all")  # -> base '/all' (case-sensitive Sentinel matcht nicht)
        # '/HERMES/x' liegt nicht unter '/all' -> Denial (kein Vollzugriff)
        with self.assertRaises(SystemExit):
            rmlib.guard_write("/HERMES/x")
        # aber '/all/x' waere erlaubt (es ist ein normaler Prefix)
        self.assertEqual(rmlib.guard_write("/all/x"), "/all/x")

    def test_all_grants_full(self):
        self.set_prefix(rmlib._GUARD_SENTINEL_ALL)
        self.assertEqual(rmlib.guard_write("/anywhere/deep/x"), "/anywhere/deep/x")


class NestedPrefixAncestor(_Base):
    """Basis '/HERMES/Sub': ein ECHTER VORFAHR '/HERMES' als Ziel ist out-of-base
    und MUSS raisen (man darf nicht oberhalb des eigenen Prefix schreiben)."""

    def test_ancestor_denied(self):
        self.set_prefix("/HERMES/Sub")
        for s in ["/HERMES", "/HERMES/", "/", "/HERMES/Other"]:
            with self.assertRaises(SystemExit, msg=f"Vorfahr/Sibling {s!r} akzeptiert"):
                rmlib.guard_write(s)

    def test_self_and_below_allowed(self):
        self.set_prefix("/HERMES/Sub")
        self.assertEqual(rmlib.guard_write("/HERMES/Sub"), "/HERMES/Sub")
        self.assertEqual(rmlib.guard_write("/HERMES/Sub/x"), "/HERMES/Sub/x")


if __name__ == "__main__":
    unittest.main(verbosity=2)
