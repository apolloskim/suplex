"""Enrichment results must be readable everywhere leads are read."""

from __future__ import annotations

import csv
import json

from click.testing import CliRunner

from suplex import cli as cli_module
from suplex.models import LeadSource, VerificationStatus
from suplex.outreach import engine


def test_enrichment_data_round_trips_through_the_store(store, make_lead) -> None:
    lead = make_lead(name="Acme Dental", website="https://acme.example")
    assert lead.enriched_at is None
    assert lead.enrichment_data == {}

    store.update_enrichment(lead.id, {"site": {"emails": ["hi@acme.example"]}})

    stored = store.get_leads()[0]
    assert stored.enriched_at is not None
    assert stored.enrichment_data == {"site": {"emails": ["hi@acme.example"]}}
    # model_dump() is what `suplex leads` and the MCP get_leads tool return.
    assert stored.model_dump()["enrichment_data"]["site"]["emails"] == ["hi@acme.example"]


def test_export_enriched_only_keeps_enriched_leads(store, make_lead, tmp_path, monkeypatch) -> None:
    enriched = make_lead(name="Enriched Co", google_maps_url="https://maps.example/1")
    make_lead(name="Raw Co", google_maps_url="https://maps.example/2")
    store.update_enrichment(enriched.id, {"site": {}})
    monkeypatch.setattr(cli_module, "LeadStore", lambda: store)

    out = tmp_path / "leads.csv"
    result = CliRunner().invoke(cli_module.cli, ["export", "--enriched", "-o", str(out)])

    assert result.exit_code == 0, result.output
    with open(out, newline="") as f:
        rows = list(csv.DictReader(f))
    assert [row["name"] for row in rows] == ["Enriched Co"]


def _campaign(store, *, enriched_only: bool) -> str:
    store.add_mailbox("mb1", "me@sender.example", "Me", "/tmp/gws-mb1")
    filters = {"enriched_only": True} if enriched_only else {}
    store.create_campaign("camp1", "Test", filters, [{"step": 1, "delay_days": 0}], "mb1", "1 Main St")
    return "camp1"


def test_campaign_enriched_only_filters_on_enrichment_not_raw_data(store, make_lead) -> None:
    enriched = make_lead(
        name="Enriched Co", email="a@enriched.example", google_maps_url="https://maps.example/1",
        verification_status=VerificationStatus.VALID, raw_data={"rating": 4.8},
    )
    make_lead(
        name="Raw Co", email="b@raw.example", google_maps_url="https://maps.example/2",
        verification_status=VerificationStatus.VALID, raw_data={"rating": 4.1},
    )
    store.update_enrichment(enriched.id, {"site": {"title": "Enriched Co"}})

    campaign_id = _campaign(store, enriched_only=True)

    assert [row["name"] for row in store.get_eligible_leads(campaign_id)] == ["Enriched Co"]


def test_preview_context_decodes_enrichment_data(store, make_lead, monkeypatch) -> None:
    lead = make_lead(
        name="Enriched Co", email="a@enriched.example", source=LeadSource.LINKEDIN,
        verification_status=VerificationStatus.VALID, raw_data={"headline": "Owner"},
    )
    store.update_enrichment(lead.id, {"linkedin_posts": [{"text": "We are hiring"}]})
    campaign_id = _campaign(store, enriched_only=True)
    monkeypatch.setattr(engine, "get_playbook", lambda: "# Playbook\n\n## Tone\n- Direct\n")
    monkeypatch.setattr(engine, "get_playbook_summary", lambda: "Direct")

    context = engine.preview_context(campaign_id, store=store)

    preview = context["leads"][0]
    assert preview["raw_data"] == {"headline": "Owner"}
    assert preview["enrichment_data"] == {"linkedin_posts": [{"text": "We are hiring"}]}
    json.dumps(context, default=str)  # must stay serialisable for the CLI / MCP
