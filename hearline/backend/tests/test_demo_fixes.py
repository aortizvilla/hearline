"""Dev email login, English phrasing in other languages, and compound source names."""

from fastapi.testclient import TestClient

from app.database.models import Story
from app.pipeline.step2_research import RawStory
from app.pipeline.step3_scripts import local_issues


def test_dev_login_by_email(client: TestClient) -> None:
    made = client.post("/api/dev/login", json={"email": "Mateo@Example.com", "name": "Mateo",
                                               "create": True}).json()
    assert made["email"] == "mateo@example.com" and made["name"] == "Mateo"
    again = client.post("/api/dev/login", json={"email": "mateo@example.com"}).json()
    assert again["user_id"] == made["user_id"]  # same person, any capitalisation
    profile = client.get("/api/profile", headers={"X-User-Id": str(made["user_id"])}).json()
    assert profile["name"] == "Mateo"
    assert client.post("/api/dev/login", json={"email": "nobody@example.com"}).status_code == 404


def test_dev_login_does_not_exist_with_clerk(client: TestClient, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "auth_mode", "clerk")
    assert client.post("/api/dev/login", json={"email": "a@example.com"}).status_code == 404


def story(source: str) -> Story:
    return Story(topic_id=1, canonical_url="u", headline="h", summary="s", source_name=source,
                 published_at=__import__("datetime").datetime(2026, 11, 22))


def test_english_phrasing_in_a_spanish_script_is_caught() -> None:
    words = " ".join(["palabra"] * 60)
    mixed = f"According to Reuters, un senador criticó la exportación. {words}"
    assert any("English phrasing" in i for i in local_issues(mixed, story("Reuters"), "es"))
    clean = f"Según Reuters, un senador criticó la exportación. {words}"
    assert not any("English phrasing" in i for i in local_issues(clean, story("Reuters"), "es"))


def test_compound_source_names_are_cleaned_and_accepted() -> None:
    raw = RawStory(headline="Headline here", summary="A summary long enough to be valid.",
                   source_name="Gulf Times (Reuters report)", url="https://gulf-times.com/a-b-c",
                   published_date="2026-11-22")
    assert raw.source_name == "Gulf Times"
    words = " ".join(["word"] * 60)
    script = f"According to Reuters, the summit stalled. {words}"
    assert not any("attribute" in i for i in local_issues(script, story("Gulf Times / Reuters"),
                                                          "en"))
