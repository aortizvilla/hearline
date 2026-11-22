"""CDK app: one stack, `Hearline`, in the account/region of the current AWS credentials.

Everything is passed by deploy.sh as context (-c key=value): see hearline_stack.py.
"""

import os

import aws_cdk as cdk

from hearline_stack import HearlineStack

app = cdk.App()
HearlineStack(app, "Hearline", env=cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=app.node.try_get_context("region") or "us-east-1"))
app.synth()
