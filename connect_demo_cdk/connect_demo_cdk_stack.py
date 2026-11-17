from aws_cdk import CfnOutput, Stack
from constructs import Construct

from connect_demo_cdk.amazon_connect import ConnectDemoResources
from connect_demo_cdk.restaurant_backend import RestaurantBackend


class ConnectDemoCdkStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        suffix: str = "demo",
        alias_suffix: str = "0000",
        countries=None,
        claim_phone_numbers: bool = False,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        backend = RestaurantBackend(self, "Backend")
        resources = ConnectDemoResources(
            self,
            "Demo",
            suffix=suffix,
            alias_suffix=alias_suffix,
            countries=countries or ["US"],
            claim_phone_numbers=claim_phone_numbers,
            pedidos_fn=backend.pedidos_fn,
        )
        backend.allow_connect(resources.instance.attr_arn)

        CfnOutput(self, "ConnectInstanceArn", value=resources.instance.attr_arn)
        CfnOutput(self, "DocumentsBucketName", value=resources.bucket.bucket_name)
        CfnOutput(self, "WisdomKbArn", value=resources.kb.attr_knowledge_base_arn)
        CfnOutput(self, "BotAliasArn", value=resources.bot_alias.attr_arn)
        CfnOutput(self, "PedidosTableName", value=backend.pedidos.table_name)
        CfnOutput(self, "ToolCallsTableName", value=backend.tool_calls.table_name)
        CfnOutput(
            self,
            "MainContactFlowArn",
            value=resources.main_flow.attr_contact_flow_arn,
            description="Associate manually claimed phone numbers with this contact flow",
        )

        if claim_phone_numbers:
            for country_code, phone in resources.phone_numbers.items():
                CfnOutput(
                    self,
                    f"PhoneNumber{country_code}",
                    value=phone.attr_address,
                    description=f"{country_code} DID phone number (E.164)",
                )
        else:
            CfnOutput(
                self,
                "PhoneNumberClaimNote",
                value=(
                    "Phone number auto-claim is DISABLED. Claim a DID in the Connect console "
                    "(Channels > Phone numbers) and set its contact flow to MainContactFlowArn, "
                    "or set claim_phone_numbers=true in config.json."
                ),
                description="How to add a phone number to this demo",
            )
