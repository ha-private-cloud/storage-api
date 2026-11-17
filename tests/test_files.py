import io
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import update

from app import library
from app.models import Node

CONTENT = bytes(range(256)) * 2 + b"tail-bytes"


async def root_items(client) -> dict:
    return {i["name"]: i for i in (await client.get("/api/v1/folders")).json()["items"]}


async def upload(client, name, data, parent_id=None, mime="application/octet-stream"):
    started = await client.post(
        "/api/v1/uploads", json={"parent_id": parent_id, "name": name, "size": len(data), "mime_type": mime}
    )
    assert started.status_code == 201, started.text
    info = started.json()
    for index in range(info["chunk_count"]):
        piece = data[index * info["chunk_size"] : (index + 1) * info["chunk_size"]]
        assert (await client.put(f"/api/v1/uploads/{info['id']}/chunks/{index}", content=piece)).status_code == 204
    done = await client.post(f"/api/v1/uploads/{info['id']}/complete")
    assert done.status_code == 201, done.text
    return done.json()


async def test_folders_can_be_created_renamed_and_moved(alice):
    work = (await alice.post("/api/v1/folders", json={"name": "Work"})).json()
    reports = (await alice.post("/api/v1/folders", json={"name": "Reports", "parent_id": work["id"]})).json()
    listing = (await alice.get("/api/v1/folders", params={"parent_id": work["id"]})).json()
    assert [i["name"] for i in listing["items"]] == ["Reports"]
    assert [p["name"] for p in listing["folder"]["path"]] == ["Work"]

    renamed = await alice.patch(f"/api/v1/nodes/{reports['id']}", json={"name": "Old reports"})
    assert renamed.json()["name"] == "Old reports"
    moved = await alice.patch(f"/api/v1/nodes/{reports['id']}", json={"parent_id": None})
    assert moved.json()["parent_id"] is None
    assert "Old reports" in await root_items(alice)


@pytest.mark.parametrize("name", ["", "a/b", "..", "x" * 256, "bad\x00name"])
async def test_invalid_names_are_refused(alice, name):
    response = await alice.post("/api/v1/folders", json={"name": name})
    assert response.status_code in (400, 422)


async def test_duplicate_names_in_a_folder_are_refused_case_insensitively(alice):
    await alice.post("/api/v1/folders", json={"name": "Trips"})
    response = await alice.post("/api/v1/folders", json={"name": "trips"})
    assert response.status_code == 409
    assert response.json()["detail"] == "name_taken"


async def test_a_folder_cannot_be_moved_into_itself(alice):
    outer = (await alice.post("/api/v1/folders", json={"name": "Outer"})).json()
    inner = (await alice.post("/api/v1/folders", json={"name": "Inner", "parent_id": outer["id"]})).json()
    response = await alice.patch(f"/api/v1/nodes/{outer['id']}", json={"parent_id": inner["id"]})
    assert response.json()["detail"] == "cannot_move_into_itself"


async def test_default_folders_cannot_be_renamed_moved_or_binned(alice):
    photos = (await root_items(alice))["Photos"]
    assert (await alice.patch(f"/api/v1/nodes/{photos['id']}", json={"name": "Pics"})).status_code == 403
    assert (await alice.delete(f"/api/v1/nodes/{photos['id']}")).status_code == 403


async def test_upload_in_chunks_and_download_it_back(alice, settings):
    node = await upload(alice, "data.bin", CONTENT)
    assert node["size"] == len(CONTENT)
    response = await alice.get(f"/api/v1/files/{node['id']}/content")
    assert response.status_code == 200
    assert response.content == CONTENT
    assert response.headers["accept-ranges"] == "bytes"
    assert "data.bin" in response.headers["content-disposition"]


@pytest.mark.parametrize(
    ("header", "start", "end"),
    [("bytes=0-0", 0, 0), ("bytes=15-17", 15, 17), ("bytes=60-130", 60, 130), ("bytes=500-", 500, None), ("bytes=-5", -5, None)],
)
async def test_byte_ranges_cross_segment_and_chunk_boundaries(alice, header, start, end):
    node = await upload(alice, "ranged.bin", CONTENT)
    response = await alice.get(f"/api/v1/files/{node['id']}/content", headers={"Range": header})
    assert response.status_code == 206
    expected = CONTENT[start:] if end is None else CONTENT[start : end + 1]
    assert response.content == expected
    assert response.headers["content-range"].endswith(f"/{len(CONTENT)}")


async def test_an_unsatisfiable_range_is_refused(alice):
    node = await upload(alice, "small.bin", b"hello")
    response = await alice.get(f"/api/v1/files/{node['id']}/content", headers={"Range": "bytes=99-"})
    assert response.status_code == 416


async def test_nothing_on_disk_is_readable(alice, settings):
    await upload(alice, "secret-name.txt", b"the plaintext marker " * 10)
    for path in Path(settings.data_dir).rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            assert b"plaintext marker" not in data
            assert "secret-name" not in path.name


async def test_an_interrupted_upload_reports_what_it_already_has(alice):
    info = (await alice.post("/api/v1/uploads", json={"name": "big.bin", "size": len(CONTENT)})).json()
    await alice.put(f"/api/v1/uploads/{info['id']}/chunks/0", content=CONTENT[:64])
    await alice.put(f"/api/v1/uploads/{info['id']}/chunks/2", content=CONTENT[128:192])
    assert (await alice.get(f"/api/v1/uploads/{info['id']}")).json()["received"] == [0, 2]
    assert (await alice.post(f"/api/v1/uploads/{info['id']}/complete")).json()["detail"] == "missing_chunks"


async def test_chunks_of_the_wrong_size_are_refused(alice):
    info = (await alice.post("/api/v1/uploads", json={"name": "x.bin", "size": 100})).json()
    response = await alice.put(f"/api/v1/uploads/{info['id']}/chunks/0", content=b"short")
    assert response.status_code == 400
    assert response.json()["expected"] == 64


async def test_uploads_beyond_the_quota_are_refused(alice):
    await upload(alice, "first.bin", b"a" * 600)
    response = await alice.post("/api/v1/uploads", json={"name": "second.bin", "size": 500})
    assert response.status_code == 413
    assert response.json()["detail"] == "quota_exceeded"
    assert (await alice.get("/api/v1/usage")).json() == {"used": 600, "quota": 1000}


async def test_binned_items_disappear_and_can_be_restored(alice):
    folder = (await alice.post("/api/v1/folders", json={"name": "Old"})).json()
    file = await upload(alice, "inside.txt", b"hello", parent_id=folder["id"])
    assert (await alice.delete(f"/api/v1/nodes/{folder['id']}")).status_code == 204

    assert "Old" not in await root_items(alice)
    assert (await alice.get(f"/api/v1/files/{file['id']}/content")).status_code == 404
    assert (await alice.get("/api/v1/search", params={"q": "inside"})).json()["items"] == []
    assert [i["name"] for i in (await alice.get("/api/v1/trash")).json()["items"]] == ["Old"]

    assert (await alice.post(f"/api/v1/trash/{folder['id']}/restore")).status_code == 200
    assert (await alice.get(f"/api/v1/files/{file['id']}/content")).content == b"hello"


async def test_deleting_forever_frees_the_space_and_removes_the_data(alice, settings):
    file = await upload(alice, "gone.bin", b"z" * 300)
    await alice.delete(f"/api/v1/nodes/{file['id']}")
    assert (await alice.delete(f"/api/v1/trash/{file['id']}")).status_code == 204
    assert (await alice.get("/api/v1/usage")).json()["used"] == 0
    assert not list((Path(settings.data_dir) / "user-alice" / "files").glob("*"))


async def test_trash_older_than_the_retention_period_is_purged(alice, app, settings):
    file = await upload(alice, "expired.bin", b"q" * 100)
    await alice.delete(f"/api/v1/nodes/{file['id']}")
    async with app.state.sessionmaker() as db:
        await db.execute(
            update(Node).where(Node.id == file["id"]).values(trashed_at=datetime.now(timezone.utc) - timedelta(days=31))
        )
        await db.commit()
        assert await library.purge_expired(db, app.state.blobs, settings.trash_retention_days) == 1
    assert (await alice.get("/api/v1/trash")).json()["items"] == []
    assert (await alice.get("/api/v1/usage")).json()["used"] == 0


async def test_sections_list_their_default_folder(alice):
    photos = (await root_items(alice))["Photos"]
    await upload(alice, "beach.jpg", b"not really a jpeg", parent_id=photos["id"], mime="image/jpeg")
    listing = (await alice.get("/api/v1/sections/photos")).json()
    assert listing["folder"]["name"] == "Photos"
    assert [i["name"] for i in listing["items"]] == ["beach.jpg"]
    assert (await alice.get("/api/v1/sections/nope")).status_code == 404


async def test_sections_list_matching_files_from_every_folder(alice):
    photos = (await root_items(alice))["Photos"]
    trips = (await alice.post("/api/v1/folders", json={"name": "Trips", "parent_id": photos["id"]})).json()
    work = (await alice.post("/api/v1/folders", json={"name": "Work"})).json()
    await upload(alice, "beach.jpg", b"jpeg", parent_id=photos["id"], mime="image/jpeg")
    await upload(alice, "desk.png", b"png", parent_id=work["id"], mime="image/png")
    await upload(alice, "notes.txt", b"text", parent_id=work["id"], mime="text/plain")
    await upload(alice, "root.gif", b"gif", mime="image/gif")

    items = (await alice.get("/api/v1/sections/photos")).json()["items"]

    assert [i["name"] for i in items] == ["Trips", "beach.jpg", "desk.png", "root.gif"]
    locations = {i["name"]: [c["name"] for c in i["location"]] for i in items if i["kind"] == "file"}
    assert locations == {"beach.jpg": ["Photos"], "desk.png": ["Work"], "root.gif": []}
    assert next(i for i in items if i["name"] == "Trips")["id"] == trips["id"]


async def test_search_results_say_which_folder_they_are_in(alice):
    work = (await alice.post("/api/v1/folders", json={"name": "Work"})).json()
    year = (await alice.post("/api/v1/folders", json={"name": "2026", "parent_id": work["id"]})).json()
    await upload(alice, "plan.txt", b"a", parent_id=year["id"])
    await upload(alice, "plan-old.txt", b"b")
    found = (await alice.get("/api/v1/search", params={"q": "plan"})).json()["items"]
    assert {i["name"]: i["location"] for i in found} == {
        "plan.txt": [{"id": work["id"], "name": "Work"}, {"id": year["id"], "name": "2026"}],
        "plan-old.txt": [],
    }


async def test_search_finds_files_in_any_folder(alice):
    work = (await alice.post("/api/v1/folders", json={"name": "Work"})).json()
    await upload(alice, "Quarterly Report.pdf", b"pdf", parent_id=work["id"])
    found = (await alice.get("/api/v1/search", params={"q": "report"})).json()["items"]
    assert [i["name"] for i in found] == ["Quarterly Report.pdf"]


async def test_images_get_a_thumbnail(alice):
    buffer = io.BytesIO()
    Image.new("RGB", (40, 30), (200, 80, 20)).save(buffer, format="PNG")
    node = await upload(alice, "pic.png", buffer.getvalue(), mime="image/png")
    listed = next(i for i in (await alice.get("/api/v1/folders")).json()["items"] if i["id"] == node["id"])
    assert listed["has_thumbnail"] is True
    thumb = await alice.get(f"/api/v1/files/{node['id']}/thumbnail")
    assert thumb.status_code == 200
    assert thumb.headers["content-type"] == "image/jpeg"
    assert Image.open(io.BytesIO(thumb.content)).format == "JPEG"


@pytest.mark.parametrize(
    ("name", "mime", "inline"),
    [("a.png", "image/png", True), ("b.mp4", "video/mp4", True), ("c.mp3", "audio/mpeg", True),
     ("d.html", "text/html", False), ("e.svg", "image/svg+xml", False), ("f.js", "text/javascript", False)],
)
async def test_only_media_is_shown_inline_and_everything_is_sandboxed(alice, name, mime, inline):
    node = await upload(alice, name, b"<script>alert(1)</script>", mime=mime)
    response = await alice.get(f"/api/v1/files/{node['id']}/content")
    assert response.headers["content-disposition"].startswith("inline" if inline else "attachment")
    assert response.headers["content-type"].startswith(mime if inline else "application/octet-stream")
    assert response.headers["content-security-policy"].startswith("sandbox")
    assert response.headers["x-content-type-options"] == "nosniff"


async def test_pdfs_are_shown_inline_in_a_same_origin_frame_only(alice):
    node = await upload(alice, "report.pdf", b"%PDF-1.4", mime="application/pdf")
    response = await alice.get(f"/api/v1/files/{node['id']}/content")
    assert response.headers["content-disposition"].startswith("inline")
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-security-policy"] == "frame-ancestors 'self'"
    assert response.headers["x-frame-options"] == "SAMEORIGIN"
    assert response.headers["x-content-type-options"] == "nosniff"

    downloaded = await alice.get(f"/api/v1/files/{node['id']}/content", params={"download": "true"})
    assert downloaded.headers["content-disposition"].startswith("attachment")
    assert downloaded.headers["content-security-policy"].startswith("sandbox")


async def test_folder_tree_lists_every_visible_folder_by_path(alice):
    work = (await alice.post("/api/v1/folders", json={"name": "Work"})).json()
    await alice.post("/api/v1/folders", json={"name": "2026", "parent_id": work["id"]})
    binned = (await alice.post("/api/v1/folders", json={"name": "Binned"})).json()
    await alice.delete(f"/api/v1/nodes/{binned['id']}")
    paths = [i["path"] for i in (await alice.get("/api/v1/folder-tree")).json()["items"]]
    assert paths == ["Documents", "Music", "Photos", "Videos", "Work", "Work/2026"]


async def section_id(client, section):
    return (await client.get(f"/api/v1/sections/{section}")).json()["folder"]["id"]


@pytest.mark.parametrize(
    ("section", "name", "mime", "allowed"),
    [
        ("photos", "beach.jpg", "image/jpeg", True),
        ("photos", "scan.HEIC", "image/heic", True),
        ("photos", "notes.txt", "text/plain", False),
        ("photos", "fake.jpg", "application/x-msdownload", False),
        ("videos", "clip.mp4", "video/mp4", True),
        ("videos", "song.mp3", "audio/mpeg", False),
        ("music", "song.flac", "audio/flac", True),
        ("music", "clip.mov", "video/quicktime", False),
        ("documents", "report.pdf", "application/pdf", True),
        ("documents", "sheet.xlsx", "application/octet-stream", True),
        ("documents", "beach.png", "image/png", False),
        ("documents", "tool.exe", "application/octet-stream", False),
    ],
)
async def test_default_folders_only_accept_their_kind_of_file(alice, section, name, mime, allowed):
    response = await alice.post(
        "/api/v1/uploads", json={"parent_id": await section_id(alice, section), "name": name, "size": 4, "mime_type": mime}
    )
    if allowed:
        assert response.status_code == 201
    else:
        assert response.status_code == 400
        assert response.json() == {"detail": "wrong_type", "section": section}


async def test_subfolders_of_a_default_folder_follow_its_rule(alice):
    trips = (await alice.post("/api/v1/folders", json={"name": "Trips", "parent_id": await section_id(alice, "photos")})).json()
    bad = await alice.post("/api/v1/uploads", json={"parent_id": trips["id"], "name": "itinerary.pdf", "size": 4})
    assert bad.json()["detail"] == "wrong_type"
    good = await alice.post("/api/v1/uploads", json={"parent_id": trips["id"], "name": "sunset.png", "size": 4, "mime_type": "image/png"})
    assert good.status_code == 201


async def test_files_cannot_be_moved_into_the_wrong_default_folder(alice):
    doc = await upload(alice, "letter.pdf", b"pdf", mime="application/pdf")
    response = await alice.patch(f"/api/v1/nodes/{doc['id']}", json={"parent_id": await section_id(alice, "photos")})
    assert response.status_code == 400
    assert response.json()["detail"] == "wrong_type"
    moved = await alice.patch(f"/api/v1/nodes/{doc['id']}", json={"parent_id": await section_id(alice, "documents")})
    assert moved.status_code == 200


async def test_a_folder_with_mismatched_contents_cannot_be_moved_in(alice):
    mixed = (await alice.post("/api/v1/folders", json={"name": "Mixed"})).json()
    await upload(alice, "pic.png", b"png", parent_id=mixed["id"], mime="image/png")
    await upload(alice, "notes.txt", b"txt", parent_id=mixed["id"], mime="text/plain")
    response = await alice.patch(f"/api/v1/nodes/{mixed['id']}", json={"parent_id": await section_id(alice, "photos")})
    assert response.json()["detail"] == "wrong_type"


async def test_renaming_cannot_sneak_the_wrong_type_into_a_default_folder(alice):
    pic = await upload(alice, "pic.png", b"png", parent_id=await section_id(alice, "photos"), mime="image/png")
    response = await alice.patch(f"/api/v1/nodes/{pic['id']}", json={"name": "pic.exe"})
    assert response.json()["detail"] == "wrong_type"
    assert (await alice.patch(f"/api/v1/nodes/{pic['id']}", json={"name": "holiday.png"})).status_code == 200


async def test_anything_goes_outside_the_default_folders(alice):
    response = await alice.post("/api/v1/uploads", json={"name": "tool.exe", "size": 4})
    assert response.status_code == 201
