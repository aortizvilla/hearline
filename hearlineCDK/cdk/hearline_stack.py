"""Hearline on AWS, cheapest shape: one EC2 server behind CloudFront.

    https://<domain>/          CloudFront -> S3 site/ (frontend build)
    https://<domain>/api/*     CloudFront -> origin.<domain> (Caddy on the server -> API)
    https://<domain>/audio/*   CloudFront -> S3 audio/ (signed links only); one bucket for both
    https://admin.<domain>/    the admin dashboard (admin.html), same API

The server runs API + worker + Postgres + Caddy with docker compose (hearlineCDK/server/). Its
disk is snapshotted daily (7 kept). Secrets live in SSM Parameter Store (/hearline/secrets,
written by hearlineCDK/scripts/put_secrets.py), never in this stack.

Context (from deploy.sh): domain, hostedZoneId, instanceType, clerkIssuer, adminUserIds,
dailyBudgetUsd, cloudfrontPublicKey (PEM), originSecret, clerkDns (JSON: name -> target);
optional alertEmail and monthlyBudgetUsd (emails on app alerts and on AWS spend).
"""

from __future__ import annotations

import json
from pathlib import Path

from aws_cdk import CfnOutput, CfnTag, Duration, RemovalPolicy, Stack
from aws_cdk import aws_budgets as budgets
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_cloudfront as cf
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_dlm as dlm
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_route53 as r53
from aws_cdk import aws_route53_targets as r53_targets
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_assets as s3_assets
from aws_cdk import aws_s3_deployment as s3_deploy
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subs
from aws_cdk import aws_ssm as ssm
from constructs import Construct

ROOT = Path(__file__).resolve().parents[2]  # the repo root
LOG_GROUP = "/hearline"


class HearlineStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(scope, construct_id, **kwargs)
        ctx = self.node.try_get_context
        domain: str = ctx("domain")
        admin_domain = f"admin.{domain}"  # the admin dashboard: a separate app (admin.html)
        origin_domain = f"origin.{domain}"
        site, admin_site = f"https://{domain}", f"https://{admin_domain}"

        zone = r53.HostedZone.from_hosted_zone_attributes(
            self, "Zone", hosted_zone_id=ctx("hostedZoneId"), zone_name=domain)

        # -- storage ------------------------------------------------------------------------
        # One private bucket: site/ (the built frontend, replaced on each deploy) and audio/
        # (listeners' clips and episodes). Kept even if the stack is deleted.
        bucket = s3.Bucket(
            self, "Storage", block_public_access=s3.BlockPublicAccess.BLOCK_ALL, enforce_ssl=True,
            encryption=s3.BucketEncryption.S3_MANAGED, removal_policy=RemovalPolicy.RETAIN)

        # -- the server ---------------------------------------------------------------------
        vpc = ec2.Vpc(  # public subnet only: no NAT gateway (~$32/month saved)
            self, "Vpc", max_azs=1, nat_gateways=0,
            subnet_configuration=[ec2.SubnetConfiguration(
                name="public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24)])
        sg = ec2.SecurityGroup(self, "ServerSg", vpc=vpc, allow_all_outbound=True,
                               description="Caddy: 443 for CloudFront, 80 for Let's Encrypt")
        # Caddy answers only requests carrying CloudFront's secret header (see Caddyfile)
        sg.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(443), "HTTPS (CloudFront)")
        sg.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(80), "Let's Encrypt challenge")

        role = iam.Role(self, "ServerRole", assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
                        managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name(
                            "AmazonSSMManagedInstanceCore")])  # shell via SSM, no SSH keys
        bucket.grant_read_write(role, "audio/*")   # the server only touches audio/
        role.add_to_policy(iam.PolicyStatement(
            actions=["ssm:GetParameter", "ssm:GetParameters"],
            resources=[self.format_arn(service="ssm", resource="parameter",
                                       resource_name="hearline/*")]))

        image = ecr_assets.DockerImageAsset(
            self, "BackendImage", directory=str(ROOT / "hearline" / "backend"),
            platform=ecr_assets.Platform.LINUX_ARM64)
        image.repository.grant_pull(role)
        server_files = s3_assets.Asset(self, "ServerFiles", path=str(ROOT / "hearlineCDK" / "server"))
        server_files.grant_read(role)

        log_group = logs.LogGroup(self, "Logs", log_group_name=LOG_GROUP,
                                  retention=logs.RetentionDays.ONE_MONTH,
                                  removal_policy=RemovalPolicy.DESTROY)
        log_group.grant_write(role)

        user_data = ec2.UserData.for_linux()
        user_data.add_commands((ROOT / "hearlineCDK" / "server" / "bootstrap.sh").read_text())
        server = ec2.Instance(
            self, "Server", vpc=vpc, security_group=sg, role=role, user_data=user_data,
            instance_type=ec2.InstanceType(ctx("instanceType") or "t4g.medium"),
            machine_image=ec2.MachineImage.latest_amazon_linux2023(
                cpu_type=ec2.AmazonLinuxCpuType.ARM_64),
            require_imdsv2=True,
            block_devices=[ec2.BlockDevice(device_name="/dev/xvda", volume=ec2.BlockDeviceVolume.ebs(
                30, volume_type=ec2.EbsDeviceVolumeType.GP3, encrypted=True,
                delete_on_termination=False))])  # the database lives on this disk
        server.instance.add_property_override("Tags", [{"Key": "hearline-backup", "Value": "daily"}])
        eip = ec2.CfnEIP(self, "ServerIp", instance_id=server.instance_id)
        r53.ARecord(self, "OriginRecord", zone=zone, record_name="origin",
                    target=r53.RecordTarget.from_ip_addresses(eip.attr_public_ip),
                    ttl=Duration.minutes(5))

        # Daily snapshots of the server's disk, 7 kept (the database backup)
        dlm_role = iam.Role(self, "SnapshotRole",
                            assumed_by=iam.ServicePrincipal("dlm.amazonaws.com"),
                            managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name(
                                "service-role/AWSDataLifecycleManagerServiceRole")])
        dlm.CfnLifecyclePolicy(
            self, "DailySnapshots", description="Hearline server disk, daily, 7 kept",
            state="ENABLED", execution_role_arn=dlm_role.role_arn,
            policy_details=dlm.CfnLifecyclePolicy.PolicyDetailsProperty(
                resource_types=["INSTANCE"],
                target_tags=[CfnTag(key="hearline-backup", value="daily")],
                schedules=[dlm.CfnLifecyclePolicy.ScheduleProperty(
                    name="daily", copy_tags=True,
                    create_rule=dlm.CfnLifecyclePolicy.CreateRuleProperty(
                        interval=24, interval_unit="HOURS", times=["04:00"]),
                    retain_rule=dlm.CfnLifecyclePolicy.RetainRuleProperty(count=7))]))

        # If AWS hardware fails, move the server to healthy hardware (same disk and IP)
        cw.Alarm(self, "AutoRecover", evaluation_periods=2, threshold=1,
                 metric=cw.Metric(namespace="AWS/EC2", metric_name="StatusCheckFailed_System",
                                  dimensions_map={"InstanceId": server.instance_id},
                                  statistic="Maximum", period=Duration.minutes(1)),
                 comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD
                 ).add_alarm_action(cw_actions.Ec2Action(cw_actions.Ec2InstanceAction.RECOVER))

        # -- CloudFront: one address for the site, the API and the audio --------------------
        cert = acm.Certificate(self, "Cert", domain_name=domain,
                               subject_alternative_names=[admin_domain],
                               validation=acm.CertificateValidation.from_dns(zone))
        public_key = cf.PublicKey(self, "AudioKey", encoded_key=ctx("cloudfrontPublicKey"))
        key_group = cf.KeyGroup(self, "AudioKeys", items=[public_key])
        # Pages (no file extension) get the app of their address: admin.<domain> the
        # dashboard, <domain> the product. The product address never serves admin.html.
        spa = cf.Function(self, "SpaRewrite", code=cf.FunctionCode.from_inline(
            "function handler(e){var r=e.request;"
            "var admin=r.headers.host&&r.headers.host.value.indexOf('admin.')===0;"
            "if(r.uri.indexOf('.')===-1||(!admin&&r.uri==='/admin.html')){"
            "r.uri=admin?'/admin.html':'/index.html';}return r;}"))

        dist = cf.Distribution(
            self, "Site", domain_names=[domain, admin_domain], certificate=cert,
            default_root_object="index.html", price_class=cf.PriceClass.PRICE_CLASS_100,
            default_behavior=cf.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(bucket, origin_path="/site"),
                viewer_protocol_policy=cf.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cf.CachePolicy.CACHING_OPTIMIZED,
                function_associations=[cf.FunctionAssociation(
                    function=spa, event_type=cf.FunctionEventType.VIEWER_REQUEST)]),
            additional_behaviors={
                "/api/*": cf.BehaviorOptions(
                    origin=origins.HttpOrigin(
                        origin_domain, protocol_policy=cf.OriginProtocolPolicy.HTTPS_ONLY,
                        custom_headers={"X-Origin-Verify": ctx("originSecret")},
                        read_timeout=Duration.seconds(60)),
                    viewer_protocol_policy=cf.ViewerProtocolPolicy.HTTPS_ONLY,
                    allowed_methods=cf.AllowedMethods.ALLOW_ALL,
                    cache_policy=cf.CachePolicy.CACHING_DISABLED,
                    origin_request_policy=cf.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER),
                "/audio/*": cf.BehaviorOptions(
                    origin=origins.S3BucketOrigin.with_origin_access_control(bucket),
                    viewer_protocol_policy=cf.ViewerProtocolPolicy.HTTPS_ONLY,
                    cache_policy=cf.CachePolicy.CACHING_OPTIMIZED,
                    trusted_key_groups=[key_group]),  # signed links only (the API signs)
            })
        r53.ARecord(self, "SiteRecord", zone=zone,
                    target=r53.RecordTarget.from_alias(r53_targets.CloudFrontTarget(dist)))
        r53.ARecord(self, "AdminRecord", zone=zone, record_name="admin",
                    target=r53.RecordTarget.from_alias(r53_targets.CloudFrontTarget(dist)))

        s3_deploy.BucketDeployment(
            self, "WebDeploy", destination_bucket=bucket, destination_key_prefix="site/",
            distribution=dist,   # replaces (and prunes) only site/: audio/ is never touched
            sources=[s3_deploy.Source.asset(str(ROOT / "hearline" / "frontend" / "dist"))])

        # Clerk production instance: its CNAME records (hearlineCDK/clerk-dns.json)
        for i, (name, target) in enumerate(json.loads(ctx("clerkDns") or "{}").items()):
            r53.CnameRecord(self, f"Clerk{i}", zone=zone, record_name=name, domain_name=target,
                            ttl=Duration.hours(1))

        # -- what the server reads at each update (hearlineCDK/server/update.sh) -------------------
        config = {
            "APP_ENV": "production",
            "AUTH_MODE": "clerk",
            "CLERK_ISSUER": ctx("clerkIssuer"),
            "CLERK_AUTHORIZED_PARTIES": json.dumps([site, admin_site]),
            "CORS_ORIGINS": json.dumps([site, admin_site]),
            "ADMIN_USER_IDS": ctx("adminUserIds") or "[]",
            "PUBLIC_BASE_URL": site,
            "AUDIO_STORAGE": "s3",
            "AUDIO_BUCKET": bucket.bucket_name,
            "AUDIO_PREFIX": "audio",
            "CLOUDFRONT_DOMAIN": domain,
            "CLOUDFRONT_KEY_ID": public_key.public_key_id,
            "AWS_REGION": self.region,
            "DAILY_BUDGET_USD": str(ctx("dailyBudgetUsd") or 25),
            "ORIGIN_DOMAIN": origin_domain,
            "LOG_GROUP": LOG_GROUP,
        }
        params = [
            ssm.StringParameter(self, "ConfigParam", parameter_name="/hearline/config",
                                string_value="\n".join(f"{k}={v}" for k, v in config.items())),
            ssm.StringParameter(self, "ImageParam", parameter_name="/hearline/image-uri",
                                string_value=image.image_uri),
            ssm.StringParameter(self, "FilesParam", parameter_name="/hearline/server-files",
                                string_value=server_files.s3_object_url),
        ]
        for param in params:  # the first boot reads them
            server.node.add_dependency(param)

        # -- alerts by email (optional: set ALERT_EMAIL in config.env) -------------------------
        email = ctx("alertEmail") or ""
        if email:
            topic = sns.Topic(self, "Alerts", display_name="Hearline alerts")
            topic.add_subscription(subs.EmailSubscription(email))   # confirm the email AWS sends
            # the app logs "ALERT <kind>: ..." when a job fails for good, the daily cap is hit
            # or the daily plan fails (app/tracking/alerts.py): one metric filter covers them all
            alerts = logs.MetricFilter(self, "AppAlerts", log_group=log_group,
                                       filter_pattern=logs.FilterPattern.literal('"ALERT "'),
                                       metric_namespace="Hearline", metric_name="Alerts",
                                       metric_value="1")
            cw.Alarm(self, "AppAlertAlarm", metric=alerts.metric(statistic="Sum",
                                                                 period=Duration.minutes(5)),
                     threshold=1, evaluation_periods=1,
                     comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                     treat_missing_data=cw.TreatMissingData.NOT_BREACHING
                     ).add_alarm_action(cw_actions.SnsAction(topic))
            # AWS spend for the month: 80 % actually spent, or 100 % forecast
            limit = float(ctx("monthlyBudgetUsd") or 60)
            budgets.CfnBudget(self, "MonthlyBudget", budget=budgets.CfnBudget.BudgetDataProperty(
                budget_type="COST", time_unit="MONTHLY",
                budget_limit=budgets.CfnBudget.SpendProperty(amount=limit, unit="USD")),
                notifications_with_subscribers=[
                    budgets.CfnBudget.NotificationWithSubscribersProperty(
                        notification=budgets.CfnBudget.NotificationProperty(
                            notification_type=kind, comparison_operator="GREATER_THAN",
                            threshold=pct, threshold_type="PERCENTAGE"),
                        subscribers=[budgets.CfnBudget.SubscriberProperty(
                            subscription_type="EMAIL", address=email)])
                    for kind, pct in (("ACTUAL", 80), ("FORECASTED", 100))])

        CfnOutput(self, "SiteUrl", value=site)
        CfnOutput(self, "AdminUrl", value=admin_site)
        CfnOutput(self, "InstanceId", value=server.instance_id)
        CfnOutput(self, "ServerPublicIp", value=eip.attr_public_ip)
