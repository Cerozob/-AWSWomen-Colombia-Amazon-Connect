#!/usr/bin/env python3
import json
import os
import secrets
import sys

import aws_cdk as cdk
from aws_cdk import Tags
from cdk_nag import (
    AwsSolutionsChecks,
    HIPAASecurityChecks,
    NIST80053R4Checks,
    NIST80053R5Checks,
    PCIDSS321Checks,
    ServerlessChecks,
)

from connect_demo_cdk.connect_demo_cdk_stack import ConnectDemoCdkStack

BASE_DIR = os.path.dirname(__file__)

# config.json is gitignored; copy config.json.example and edit it.
config_path = os.path.join(BASE_DIR, "config.json")
if not os.path.exists(config_path):
    sys.exit("Falta config.json: copia config.json.example a config.json y ajústalo.")
with open(config_path, "r", encoding="utf-8") as f:
    config = json.load(f)

tags = config.get("tags")
if not isinstance(tags, dict) or not tags:
    sys.exit("config.json debe incluir un objeto 'tags' no vacío (ver config.json.example).")

# The Connect instance alias lives in the GLOBAL `<alias>.my.connect.aws`
# namespace, so a random suffix is generated once and persisted (gitignored)
# to keep the alias stable across redeploys.
alias_suffix_path = os.path.join(BASE_DIR, ".connect-alias-suffix")
if os.path.exists(alias_suffix_path):
    with open(alias_suffix_path, "r", encoding="utf-8") as f:
        alias_suffix = f.read().strip()
else:
    alias_suffix = secrets.token_hex(4)
    with open(alias_suffix_path, "w", encoding="utf-8") as f:
        f.write(alias_suffix)

app = cdk.App()
ConnectDemoCdkStack(
    app,
    "ConnectDemoCdkStack",
    suffix=config.get("suffix", "restaurant"),
    alias_suffix=alias_suffix,
    countries=config.get("countries", ["US"]),
    claim_phone_numbers=config.get("claim_phone_numbers", False),
    env=cdk.Environment(
        account=os.getenv("CDK_DEFAULT_ACCOUNT"),
        region=os.getenv("CDK_DEFAULT_REGION"),
    ),
)

for key, value in tags.items():
    Tags.of(app).add(key, value)

# cdk-nag rule packs: all off by default (enable per config.json "nag" block).
nag = config.get("nag", {})
packs = {
    "aws_solutions": AwsSolutionsChecks,
    "hipaa_security": HIPAASecurityChecks,
    "nist_800_53_r4": NIST80053R4Checks,
    "nist_800_53_r5": NIST80053R5Checks,
    "pci_dss_321": PCIDSS321Checks,
    "serverless": ServerlessChecks,
}
from aws_cdk import Validations  # noqa: E402 (cdk-nag v3 uses the native Validations API)

for flag, pack in packs.items():
    if nag.get(flag, False):
        Validations.of(app).add_plugins(pack(app))

app.synth()
