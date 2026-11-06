"""Evidence collection uses immutable cached pages and the common writer lock."""

import json

import httpx
import pytest

from suno_dl import archive_lock
from suno_dl import producer_evidence as evidence
from tests.test_producer_lookup import ID as ID1
from tests.test_producer_lookup import OTHER as ID2
from tests.test_producer_lookup import page


def recording(root, clip_id=ID1, stem="first"):
    root.mkdir(exist_ok=True)
    (root / (stem + ".m4a")).write_bytes(b"a" * 2048)
    (root / (stem + ".json")).write_text(
        json.dumps({"id": clip_id, "title": stem, "artist": None, "saved_files": [stem + ".m4a"]})
    )


def client(handler):
    return httpx.Client(
        transport=httpx.MockTransport(handler), follow_redirects=False, trust_env=False
    )


def collect(root, state, session):
    return evidence.run(root, state, fetch=True, client=session, delay=0, writers_stopped=True)


def test_collection_resumes_without_repeating_verified_lookup(tmp_path):
    root = tmp_path / "library"
    recording(root)
    recording(root, ID2, "second")
    state = tmp_path / "evidence"
    requests = []

    def interrupted(request):
        requests.append(str(request.url))
        if str(request.url).endswith(ID2):
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(
            200, headers={"content-type": "text/html"}, text=page(ID1, "Artist One")
        )

    with client(interrupted) as session, pytest.raises(httpx.ConnectError):
        collect(root, state, session)
    checkpoint = json.loads((state / "evidence.json").read_text())
    assert set(checkpoint["verified"]) == {ID1}
    pending = evidence.run(root, state)
    assert pending["unverified_ids"] == [ID2] and not pending["plan_available"]

    def resumed(request):
        requests.append(str(request.url))
        assert str(request.url).endswith(ID2)
        return httpx.Response(
            200, headers={"content-type": "text/html"}, text=page(ID2, "Artist Two")
        )

    with client(resumed) as session:
        result = collect(root, state, session)
    assert result["verified"] == 2 and result["plan_available"]
    assert requests == [
        "https://suno.com/song/" + ID1,
        "https://suno.com/song/" + ID2,
        "https://suno.com/song/" + ID2,
    ]
    assert (root / "first.m4a").read_bytes() == b"a" * 2048


def test_collection_requires_writer_assertion_before_state_or_lock(tmp_path):
    root = tmp_path / "library"
    recording(root)
    state = tmp_path / "evidence"
    with pytest.raises(evidence.EvidenceError, match="writers must be stopped"):
        evidence.run(root, state, fetch=True)
    assert not state.exists() and not (root / ".suno-sync.lock").exists()


def test_busy_archive_blocks_before_network_or_state(tmp_path, monkeypatch):
    root = tmp_path / "library"
    recording(root)
    state = tmp_path / "evidence"
    monkeypatch.setattr(archive_lock, "_WAIT_SEC", 0)
    with archive_lock._exclusive_archive_sync(root), pytest.raises(TimeoutError):
        evidence.run(root, state, fetch=True, writers_stopped=True)
    assert not state.exists()


def test_fetch_holds_common_lock(tmp_path, monkeypatch):
    root = tmp_path / "library"
    recording(root)
    state = tmp_path / "evidence"
    monkeypatch.setattr(archive_lock, "_WAIT_SEC", 0)

    def handler(request):
        with pytest.raises(TimeoutError), archive_lock._exclusive_archive_sync(root):
            pass
        return httpx.Response(200, headers={"content-type": "text/html"}, text=page(ID1))

    with client(handler) as session:
        assert collect(root, state, session)["verified"] == 1


@pytest.mark.parametrize("change", ["audio", "sidecar", "additional"])
def test_inventory_change_aborts_without_claiming_producer(tmp_path, change):
    root = tmp_path / "library"
    recording(root)
    state = tmp_path / "evidence"

    def handler(request):
        if change == "audio":
            (root / "first.m4a").write_bytes(b"b" * 2049)
        elif change == "sidecar":
            (root / "first.json").write_text("{}")
        else:
            (root / "new.txt").write_text("new member")
        return httpx.Response(200, headers={"content-type": "text/html"}, text=page(ID1))

    with (
        client(handler) as session,
        pytest.raises(evidence.EvidenceError, match="inventory changed"),
    ):
        collect(root, state, session)
    assert not json.loads((state / "evidence.json").read_text())["verified"]


@pytest.mark.parametrize(
    "tamper", ["html", "checkpoint_artist", "checkpoint_handle", "checkpoint_hash"]
)
def test_cached_evidence_tampering_is_rejected(tmp_path, tamper):
    root = tmp_path / "library"
    recording(root)
    state = tmp_path / "evidence"
    with client(
        lambda r: httpx.Response(200, headers={"content-type": "text/html"}, text=page(ID1))
    ) as session:
        collect(root, state, session)
    if tamper == "html":
        next(state.glob("*.html")).write_text(page(ID1, "Wrong"))
    else:
        path = state / "evidence.json"
        data = json.loads(path.read_text())
        if tamper == "checkpoint_artist":
            data["verified"][ID1]["artist"] = "Wrong"
        elif tamper == "checkpoint_handle":
            data["verified"][ID1]["creator_handle"] = "wrong_profile"
        else:
            data["verified"][ID1]["page_sha256"] = "0" * 64
        path.write_text(json.dumps(data))
    with pytest.raises(evidence.EvidenceError):
        evidence.run(root, state)


@pytest.mark.parametrize("status", [302, 403, 404])
def test_unavailable_page_never_becomes_unknown_producer(tmp_path, status):
    root = tmp_path / "library"
    recording(root)
    requests = []

    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(status, headers={"location": "https://attacker.example/"})

    with client(handler) as session:
        result = collect(root, tmp_path / "evidence", session)
    assert len(requests) == 1 and result["verified"] == 0 and result["unverified_ids"] == [ID1]


@pytest.mark.parametrize("status", [429, 500, 503])
def test_backpressure_stops_collection(tmp_path, status):
    root = tmp_path / "library"
    recording(root)
    with client(lambda r: httpx.Response(status)) as session, pytest.raises(evidence.RateLimited):
        collect(root, tmp_path / "evidence", session)


def test_wrong_content_type_is_not_evidence(tmp_path):
    root = tmp_path / "library"
    recording(root)
    with client(
        lambda r: httpx.Response(200, headers={"content-type": "application/json"}, text=page(ID1))
    ) as session:
        result = collect(root, tmp_path / "evidence", session)
    assert result["verified"] == 0


def test_state_inside_library_rejected_before_creation(tmp_path):
    root = tmp_path / "library"
    recording(root)
    with pytest.raises(evidence.EvidenceError, match="outside"):
        evidence.run(root, root / "state", fetch=True, writers_stopped=True)
    assert not (root / "state").exists()


def test_default_does_not_initialize_or_fetch(tmp_path):
    root = tmp_path / "library"
    recording(root)
    with pytest.raises(evidence.EvidenceError, match="does not exist"):
        evidence.run(root, tmp_path / "evidence")
    assert not (tmp_path / "evidence").exists()


def test_legacy_flight_claim_cannot_supply_evidence(tmp_path):
    root = tmp_path / "library"
    recording(root)
    spoof = (
        '<html><head><link rel="canonical" href="https://suno.com/song/'
        + ID1
        + '"></head><body><script>{"clip":{"id":"'
        + ID1
        + '","display_name":"drinko"}}</script></body></html>'
    )
    with client(
        lambda r: httpx.Response(200, headers={"content-type": "text/html"}, text=spoof)
    ) as session:
        result = collect(root, tmp_path / "evidence", session)
    assert result["verified"] == 0 and not result["plan_available"]


def test_inventory_checks_layout_once_then_uses_metadata_for_frozen_comparisons(
    tmp_path, monkeypatch
):
    root = tmp_path / "library"
    recording(root)
    calls = []
    original = evidence.plan_migration

    def plan(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(evidence, "plan_migration", plan)
    with client(
        lambda r: httpx.Response(200, headers={"content-type": "text/html"}, text=page(ID1))
    ) as session:
        result = collect(root, tmp_path / "evidence", session)
    assert result["plan_available"] and len(calls) == 2
