"""The Hearline stack, built offline with placeholder values (no AWS account, no Docker build).
Run: hearlineCDK/cdk/.venv/bin/python -m pytest -q hearlineCDK/cdk     (pip install -r requirements-dev.txt)
"""

import json
import subprocess
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from hearline_stack import ROOT, HearlineStack


@pytest.fixture(scope="module")
def keypair(tmp_path_factory: pytest.TempPathFactory) -> str:
    d = tmp_path_factory.mktemp("keys")
    subprocess.run(["openssl", "genrsa", "-out", d / "k.pem", "2048"], check=True, capture_output=True)
    return subprocess.run(["openssl", "rsa", "-pubout", "-in", d / "k.pem"], check=True,
                          capture_output=True, text=True).stdout


def build(keypair: str, **extra: str) -> Template:
    (ROOT / "hearline" / "frontend" / "dist").mkdir(exist_ok=True)       # the site upload needs the folder
    ctx = {"region": "us-east-1", "domain": "example.click", "hostedZoneId": "Z0000",
           "cloudfrontPublicKey": keypair, "originSecret": "s3cret", "clerkIssuer": "https://clerk.example.click",
           "clerkDns": json.dumps({"clerk": "frontend-api.clerk.services"}), **extra}
    app = cdk.App(context=ctx)
    stack = HearlineStack(app, "Hearline", env=cdk.Environment(account="123456789012", region="us-east-1"))
    return Template.from_stack(stack)


@pytest.fixture(scope="module")
def t(keypair: str) -> Template:
    return build(keypair)


def test_one_small_server_and_no_nat_gateway(t: Template) -> None:
    t.resource_count_is("AWS::EC2::Instance", 1)
    t.has_resource_properties("AWS::EC2::Instance", {"InstanceType": "t4g.medium"})
    t.resource_count_is("AWS::EC2::NatGateway", 0)                 # ~$32/month saved
    t.resource_count_is("AWS::ElasticLoadBalancingV2::LoadBalancer", 0)
    t.resource_count_is("AWS::RDS::DBInstance", 0)


def test_site_and_admin_on_one_distribution_with_a_protected_api(t: Template) -> None:
    t.has_resource_properties("AWS::CloudFront::Distribution", {"DistributionConfig": Match.object_like({
        "Aliases": ["example.click", "admin.example.click"],
        "Origins": Match.array_with([Match.object_like({
            "DomainName": "origin.example.click",
            "OriginCustomHeaders": [{"HeaderName": "X-Origin-Verify", "HeaderValue": "s3cret"}]})]),
        "CacheBehaviors": Match.array_with([
            Match.object_like({"PathPattern": "/api/*"}),
            Match.object_like({"PathPattern": "/audio/*", "TrustedKeyGroups": Match.any_value()})]),
    })})
    t.has_resource_properties("AWS::CertificateManager::Certificate",
                              {"DomainName": "example.click", "SubjectAlternativeNames": ["admin.example.click"]})


def test_server_settings_are_production(t: Template) -> None:
    params = t.find_resources("AWS::SSM::Parameter", {"Properties": {"Name": "/hearline/config"}})
    value = json.dumps(next(iter(params.values()))["Properties"]["Value"])
    for setting in ("APP_ENV=production", "AUTH_MODE=clerk", "AUDIO_STORAGE=s3",
                    "https://admin.example.click", "PUBLIC_BASE_URL=https://example.click"):
        assert setting in value
    assert "s3cret" not in value                                   # secrets never in the template config


def test_one_bucket_kept_and_the_site_upload_never_touches_audio(t: Template) -> None:
    t.resource_count_is("AWS::S3::Bucket", 1)
    t.has_resource("AWS::S3::Bucket", {"DeletionPolicy": "Retain"})
    t.has_resource_properties("Custom::CDKBucketDeployment", {"DestinationBucketKeyPrefix": "site/", "Prune": True})
    t.has_resource_properties("AWS::DLM::LifecyclePolicy", {"PolicyDetails": Match.object_like({
        "Schedules": [Match.object_like({"RetainRule": {"Count": 7}})]})})


def test_alerts_only_when_an_email_is_set(t: Template, keypair: str) -> None:
    t.resource_count_is("AWS::SNS::Topic", 0)
    t.resource_count_is("AWS::Budgets::Budget", 0)
    a = build(keypair, alertEmail="ops@example.com", monthlyBudgetUsd="40")
    a.has_resource_properties("AWS::SNS::Subscription", {"Protocol": "email", "Endpoint": "ops@example.com"})
    a.has_resource_properties("AWS::Logs::MetricFilter", {"FilterPattern": '"ALERT "'})
    a.has_resource_properties("AWS::Budgets::Budget", {"Budget": Match.object_like(
        {"BudgetLimit": {"Amount": 40, "Unit": "USD"}})})
