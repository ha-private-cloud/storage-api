from sqlalchemy import select

from app.models import Node, Vault
from tests.conftest import PASSWORD, SERVICE_HEADERS, as_user


async def test_every_route_needs_the_service_token(client):
    client.cookies.set("ck_sso", "alice-session")
    assert (await client.get("/api/v1/vault")).status_code == 401
    assert (await client.get("/api/v1/folders", headers={"Authorization": "Bearer wrong"})).status_code == 401


async def test_unknown_session_is_rejected(client):
    client.headers.update(SERVICE_HEADERS)
    client.cookies.set("ck_sso", "made-up")
    assert (await client.get("/api/v1/vault")).status_code == 401


async def test_storage_is_not_set_up_until_the_first_unlock(client):
    as_user(client)
    assert (await client.get("/api/v1/vault")).json() == {"exists": False, "unlocked": False}
    response = await client.get("/api/v1/folders")
    assert response.status_code == 423
    assert response.json()["detail"] == "not_set_up"


async def test_first_unlock_creates_storage_a_recovery_code_and_default_folders(alice):
    code = alice.recovery_code
    assert len(code.replace("-", "")) == 30
    names = [i["name"] for i in (await alice.get("/api/v1/folders")).json()["items"]]
    assert names == ["Documents", "Music", "Photos", "Videos"]
    assert (await alice.get("/api/v1/vault")).json() == {"exists": True, "unlocked": True}


async def test_names_and_keys_are_never_stored_in_plain_form(alice, app):
    await alice.post("/api/v1/folders", json={"name": "Tax returns"})
    async with app.state.sessionmaker() as db:
        stored = [n.encrypted_name for n in await db.scalars(select(Node))]
        vault = await db.scalar(select(Vault))
    assert not any("Tax returns" in s or "Photos" in s for s in stored)
    assert alice.recovery_code.encode() not in vault.recovery_wrapped_key


async def test_a_new_session_must_unlock_before_using_storage(alice):
    as_user(alice, "alice-second-session")
    response = await alice.get("/api/v1/folders")
    assert response.status_code == 423
    assert response.json()["detail"] == "locked"
    assert (await alice.post("/api/v1/vault/unlock", json={"password": PASSWORD})).json() == {"status": "unlocked"}
    assert (await alice.get("/api/v1/folders")).status_code == 200


async def test_a_reset_password_needs_the_recovery_code(alice):
    as_user(alice, "alice-second-session")
    response = await alice.post("/api/v1/vault/unlock", json={"password": "a-password-set-by-an-admin"})
    assert response.status_code == 409
    assert response.json()["detail"] == "recovery_required"
    assert (await alice.get("/api/v1/folders")).status_code == 423


async def test_recovery_relocks_the_key_under_the_new_password(alice):
    code = alice.recovery_code
    await alice.post("/api/v1/folders", json={"name": "Kept"})
    as_user(alice, "alice-second-session")
    typed = code.lower().replace("-", " ")
    response = await alice.post("/api/v1/vault/recover", json={"recovery_code": typed, "password": "the-new-password-123"})
    assert response.json() == {"status": "unlocked"}
    assert "Kept" in [i["name"] for i in (await alice.get("/api/v1/folders")).json()["items"]]

    await alice.post("/api/v1/vault/lock")
    assert (await alice.post("/api/v1/vault/unlock", json={"password": "the-new-password-123"})).status_code == 200
    assert (await alice.post("/api/v1/vault/unlock", json={"password": PASSWORD})).status_code == 409


async def test_a_wrong_recovery_code_is_refused(alice):
    response = await alice.post(
        "/api/v1/vault/recover", json={"recovery_code": "AAAAA-BBBBB-CCCCC-DDDDD-EEEEE-FFFFF", "password": "whatever-123"}
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "invalid_recovery_code"


async def test_locking_drops_the_unlocked_key(alice):
    assert (await alice.post("/api/v1/vault/lock")).status_code == 204
    assert (await alice.get("/api/v1/folders")).status_code == 423


async def test_users_cannot_see_each_others_files(alice):
    folder = (await alice.post("/api/v1/folders", json={"name": "Private"})).json()
    as_user(alice, "bob-session")
    await alice.post("/api/v1/vault/unlock", json={"password": "bobs-password-123"})
    assert (await alice.get("/api/v1/folders", params={"parent_id": folder["id"]})).status_code == 404
    assert (await alice.patch(f"/api/v1/nodes/{folder['id']}", json={"name": "Mine"})).status_code == 404
    assert (await alice.delete(f"/api/v1/nodes/{folder['id']}")).status_code == 404
    assert "Private" not in [i["name"] for i in (await alice.get("/api/v1/search", params={"q": "Priv"})).json()["items"]]
