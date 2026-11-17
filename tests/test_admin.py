from tests.conftest import PASSWORD, SERVICE_HEADERS, as_user
from tests.test_files import upload


async def test_admin_endpoints_need_an_admin_session(alice):
    assert (await alice.get("/api/v1/admin/vaults")).status_code == 403
    as_user(alice, "admin-session")
    alice.headers.pop("Authorization")
    assert (await alice.get("/api/v1/admin/vaults")).status_code == 401


async def test_admin_sees_every_vaults_quota_and_usage(alice, settings):
    await upload(alice, "notes.txt", b"x" * 120)
    as_user(alice, "admin-session")
    vaults = (await alice.get("/api/v1/admin/vaults")).json()["items"]
    assert vaults == [{"user_id": "user-alice", "quota_bytes": settings.default_quota_bytes, "used_bytes": 120}]


async def test_admin_changes_a_quota_and_the_user_sees_it(alice):
    as_user(alice, "admin-session")
    response = await alice.put("/api/v1/admin/vaults/user-alice/quota", json={"quota_bytes": 5000})
    assert response.status_code == 200
    assert response.json()["quota_bytes"] == 5000
    as_user(alice)
    assert (await alice.get("/api/v1/usage")).json()["quota"] == 5000


async def test_lowering_a_quota_below_usage_blocks_uploads_but_keeps_files(alice):
    stored = await upload(alice, "big.bin", b"y" * 600)
    as_user(alice, "admin-session")
    await alice.put("/api/v1/admin/vaults/user-alice/quota", json={"quota_bytes": 100})
    as_user(alice)
    refused = await alice.post("/api/v1/uploads", json={"name": "more.bin", "size": 10})
    assert refused.json()["detail"] == "quota_exceeded"
    assert (await alice.get(f"/api/v1/files/{stored['id']}/content")).content == b"y" * 600


async def test_unknown_users_and_bad_quotas_are_refused(alice):
    as_user(alice, "admin-session")
    assert (await alice.put("/api/v1/admin/vaults/user-nobody/quota", json={"quota_bytes": 10})).status_code == 404
    assert (await alice.put("/api/v1/admin/vaults/user-alice/quota", json={"quota_bytes": -1})).status_code == 422


async def test_new_vaults_get_the_default_quota(client, settings):
    as_user(client, "bob-session")
    await client.post("/api/v1/vault/unlock", json={"password": PASSWORD})
    assert (await client.get("/api/v1/usage")).json()["quota"] == settings.default_quota_bytes
