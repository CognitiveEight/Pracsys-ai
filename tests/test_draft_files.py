"""SIM-211: draft Word files are filed by workspace, short-lived and purgeable.

No real storage: a fake GCS client records what would have happened.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
from pathlib import Path

import pytest

from app.storage import draft_files, gcs

WS = "6af5743ad8e1c12884c13492"


class _Blob:
    def __init__(self, store, name):
        self.store, self.name = store, name

    def upload_from_string(self, data, content_type=None):
        self.store.objects[self.name] = data

    def generate_signed_url(self, version, expiration, method):
        self.store.signed.append((self.name, expiration))
        return f"https://storage.example/{self.name}?sig=x"

    def delete(self):
        if self.store.fail_delete:
            raise RuntimeError("simulated storage outage")
        self.store.objects.pop(self.name, None)


class _Bucket:
    def __init__(self, store):
        self.store = store

    def blob(self, name):
        return _Blob(self.store, name)


class _Client:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.signed: list = []
        self.fail_delete = False

    def bucket(self, _name):
        return _Bucket(self)

    def list_blobs(self, _bucket, prefix=""):
        return [_Blob(self, n) for n in sorted(self.objects) if n.startswith(prefix)]


@pytest.fixture
def client(monkeypatch):
    c = _Client()
    monkeypatch.setattr(gcs, "get_gcs_client", lambda: c)
    monkeypatch.setattr(draft_files.settings, "gcs_docx_bucket_name", "drafts-bucket")
    monkeypatch.setattr(draft_files.settings, "draft_docx_link_ttl_seconds", 900)
    return c


def test_upload_is_filed_by_workspace_without_title(client):
    url = draft_files.upload_draft_docx(WS, b"docx")
    (name,) = client.objects
    assert name.startswith(f"drafts/{WS}/")
    assert name.endswith(".docx")
    # uuid hex only: nothing from the draft title or the user
    assert len(name.split("/")[-1]) == 32 + len(".docx")
    assert url.startswith("https://")


def test_link_is_short_lived(client):
    before = dt.datetime.now(dt.timezone.utc)
    draft_files.upload_draft_docx(WS, b"docx")
    (_, expiry), = client.signed
    assert dt.timedelta(minutes=14) < expiry - before <= dt.timedelta(minutes=15, seconds=5)


def test_signed_url_has_no_default_expiry():
    with pytest.raises(TypeError):
        gcs.generate_signed_url("b", "n")  # type: ignore[call-arg]


def test_signed_url_refuses_explicit_none_expiry():
    with pytest.raises(ValueError):
        gcs.generate_signed_url("b", "n", expires_at=None)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", ["", "../other", "a/b", None])
def test_invalid_workspace_id_is_refused(client, bad):
    with pytest.raises(ValueError):
        draft_files.upload_draft_docx(bad, b"docx")
    assert client.objects == {}


def test_no_bucket_refuses_upload_and_purges_nothing(monkeypatch):
    monkeypatch.setattr(draft_files.settings, "gcs_docx_bucket_name", None)
    with pytest.raises(RuntimeError):
        draft_files.upload_draft_docx(WS, b"docx")
    assert draft_files.delete_workspace_draft_files(WS) == 0


def test_purge_deletes_only_that_workspace(client):
    draft_files.upload_draft_docx(WS, b"1")
    draft_files.upload_draft_docx(WS, b"2")
    draft_files.upload_draft_docx("otherworkspace", b"3")
    client.objects["draft/user1/" + "a" * 32 + "-Title.docx"] = b"legacy"

    assert draft_files.delete_workspace_draft_files(WS) == 2
    assert not any(n.startswith(f"drafts/{WS}/") for n in client.objects)
    assert any(n.startswith("drafts/otherworkspace/") for n in client.objects)
    assert any(n.startswith("draft/") for n in client.objects)


def test_purge_does_not_reach_a_workspace_whose_id_extends_it(client):
    # "<WS>x" shares WS as a string prefix; the trailing "/" must keep it out.
    draft_files.upload_draft_docx(WS, b"1")
    draft_files.upload_draft_docx(WS + "x", b"2")

    assert draft_files.delete_workspace_draft_files(WS) == 1
    assert any(n.startswith(f"drafts/{WS}x/") for n in client.objects)


def test_purge_raises_on_storage_error(client):
    draft_files.upload_draft_docx(WS, b"1")
    client.fail_delete = True
    with pytest.raises(RuntimeError):
        draft_files.delete_workspace_draft_files(WS)


def _cleanup_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "cleanup_legacy_draft_files.py"
    spec = importlib.util.spec_from_file_location("cleanup_legacy_draft_files", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cleanup_dry_run_deletes_nothing(client, capsys):
    mod = _cleanup_module()
    legacy = "draft/user1/" + "b" * 32 + "-Some_Title.docx"
    stray = "draft/notes.txt"
    client.objects.update({legacy: b"x", stray: b"y"})

    assert mod.main([]) == 0
    assert legacy in client.objects and stray in client.objects
    out = capsys.readouterr().out
    assert "would delete): 1" in out and "left alone): 1" in out
    assert "Some_Title" not in out  # names never printed


def test_cleanup_execute_deletes_only_legacy_pattern(client):
    mod = _cleanup_module()
    legacy = "draft/user1/" + "c" * 32 + "-T.docx"
    stray = "draft/user1/not-a-uuid.docx"
    new = f"drafts/{WS}/" + "d" * 32 + ".docx"
    client.objects.update({legacy: b"x", stray: b"y", new: b"z"})

    assert mod.main(["--execute"]) == 0
    assert legacy not in client.objects
    assert stray in client.objects and new in client.objects
